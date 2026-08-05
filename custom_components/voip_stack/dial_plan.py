"""Pure construction of PBX dial targets and ring policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .dial_fork import ForkStrategy
from .roster import RosterEntry, find_entry
from .core.sip import parse_sip_uri


@dataclass(frozen=True, slots=True)
class RingPolicy:
    """Bounded forking policy resolved before any signaling starts."""

    strategy: ForkStrategy = ForkStrategy.PARALLEL
    overall_timeout: float = 30.0
    step_timeout: float = 15.0
    member_tiers: Mapping[str, int] = field(default_factory=dict)
    tier_strategies: Mapping[int, ForkStrategy] = field(default_factory=dict)

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> "RingPolicy":
        raw = dict(metadata or {})
        nested = raw.get("ring_policy")
        if isinstance(nested, Mapping):
            raw = {**raw, **nested}
        strategy = ForkStrategy(str(raw.get("strategy") or raw.get("ring_strategy") or "parallel"))
        overall_timeout = float(raw.get("overall_timeout") or raw.get("ring_timeout") or 30.0)
        step_timeout = float(raw.get("step_timeout") or 15.0)
        if not 0.0 < overall_timeout <= 300.0:
            raise ValueError("ring overall_timeout must be between 0 and 300 seconds")
        if not 0.0 < step_timeout <= overall_timeout:
            raise ValueError("ring step_timeout must be positive and no longer than overall_timeout")
        member_tiers = {
            str(member).strip().lower(): int(tier)
            for member, tier in dict(raw.get("member_tiers") or {}).items()
        }
        if any(tier < 0 for tier in member_tiers.values()):
            raise ValueError("ring member tiers must not be negative")
        tier_strategies = {
            int(tier): ForkStrategy(value)
            for tier, value in dict(raw.get("tier_strategies") or {}).items()
        }
        if any(tier < 0 for tier in tier_strategies):
            raise ValueError("ring strategy tiers must not be negative")
        return cls(
            strategy=strategy,
            overall_timeout=overall_timeout,
            step_timeout=step_timeout,
            member_tiers=member_tiers,
            tier_strategies=tier_strategies,
        )


@dataclass(frozen=True, slots=True)
class SipContactTarget:
    """One physical Contact belonging to a logical dial destination."""

    candidate_id: str
    endpoint_id: str
    member: str
    uri: str
    transport: str
    q: float
    tier: int
    order: int
    user_agent: str = ""


def _contact_rows(entry: RosterEntry) -> list[tuple[str, str, float, str]]:
    rows: list[tuple[str, str, float, str]] = []
    raw_contacts = (entry.metadata or {}).get("sip_contacts")
    if raw_contacts is not None:
        if not isinstance(raw_contacts, list):
            raise ValueError(f"sip_contacts for {entry.id!r} must be a list")
        for raw in raw_contacts:
            if not isinstance(raw, Mapping):
                raise ValueError(f"invalid SIP Contact for {entry.id!r}")
            uri = str(raw.get("uri") or "").strip()
            transport = str(raw.get("transport") or "").strip().lower()
            q = float(raw.get("q", 1.0))
            if not uri or not 0.0 <= q <= 1.0:
                raise ValueError(f"invalid SIP Contact for {entry.id!r}")
            user_agent = str(raw.get("user_agent") or "").strip()
            parsed = parse_sip_uri(uri)
            if transport not in {"udp", "tcp"}:
                transport = next(
                    (
                        str(value).lower()
                        for key, value in parsed.params
                        if key.lower() == "transport"
                    ),
                    "udp",
                )
            if transport not in {"udp", "tcp"}:
                raise ValueError(f"unsupported SIP transport for {entry.id!r}")
            rows.append((str(parsed), transport, q, user_agent))
    elif entry.sip_uri:
        parsed = parse_sip_uri(entry.sip_uri)
        transport = next(
            (
                str(value).lower()
                for key, value in parsed.params
                if key.lower() == "transport"
            ),
            "udp",
        )
        rows.append(
            (
                str(parsed),
                transport,
                1.0,
                str((entry.metadata or {}).get("user_agent") or "").strip(),
            )
        )

    unique: list[tuple[str, str, float, str]] = []
    seen: set[str] = set()
    for row in rows:
        identity = row[0].lower()
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return sorted(unique, key=lambda row: (-row[2], row[0]))


def build_sip_contact_targets(
    members: Iterable[str],
    entries: list[RosterEntry],
    *,
    policy: RingPolicy,
    exclude_endpoint_id: str = "",
) -> tuple[SipContactTarget, ...]:
    """Expand logical members into q-ordered physical Contact branches."""

    targets: list[SipContactTarget] = []
    excluded = str(exclude_endpoint_id or "").strip()
    for member_order, member in enumerate(members):
        entry = find_entry(entries, str(member), include_number=False)
        if entry is None or not entry.enabled:
            continue
        endpoint_id = str((entry.metadata or {}).get("endpoint_id") or entry.id).strip()
        if excluded and endpoint_id == excluded:
            continue
        contacts = _contact_rows(entry)
        q_tiers = {
            q: index for index, q in enumerate(sorted({row[2] for row in contacts}, reverse=True))
        }
        base_tier = policy.member_tiers.get(str(member).strip().lower(), 0)
        for contact_order, (uri, transport, q, user_agent) in enumerate(contacts):
            targets.append(
                SipContactTarget(
                    candidate_id=f"{endpoint_id}:contact:{contact_order}",
                    endpoint_id=endpoint_id,
                    member=str(member),
                    uri=uri,
                    transport=transport,
                    q=q,
                    tier=base_tier + q_tiers[q],
                    order=member_order * 1000 + contact_order,
                    user_agent=user_agent,
                )
            )
    if len({target.candidate_id for target in targets}) != len(targets):
        raise ValueError("dial plan contains duplicate logical endpoint members")
    return tuple(sorted(targets, key=lambda target: (target.tier, target.order)))
