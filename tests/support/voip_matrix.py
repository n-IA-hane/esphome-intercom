"""Reusable in-memory PBX model for fast group-call behavior tests.

The model intentionally stays at SIP/PBX semantics: ring groups fork one
dialog per member and pick one winner; conference groups are rooms where
participants join and optional ring members are invited.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import importlib.util
import json
from pathlib import Path
import sys


DEFAULT_AUDIO_FORMATS = ("16000:s16le:1:20", "48000:s16le:1:10")
SPOTPEAR_AUDIO_FORMATS = ("16000:s16le:1:20",)
MEDIA_INCOMPATIBLE = "media_incompatible"

IDLE = "idle"
RINGING = "ringing"
IN_CALL = "in_call"
CANCELLED = "cancelled"
ENDED = "ended"
BUSY = "busy"
UNAVAILABLE = "unavailable"


def _ha_audio_formats() -> tuple[str, ...]:
    """Load HA SIP audio formats without importing the Home Assistant package."""
    module_path = (
        Path(__file__).resolve().parents[2]
        / "custom_components"
        / "voip_stack"
        / "core"
        / "audio_format.py"
    )
    spec = importlib.util.spec_from_file_location("_voip_stack_audio_format", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return tuple(fmt.wire_token() for fmt in module.HA_SIP_PCM_FORMATS[:8])


HA_AUDIO_FORMATS = _ha_audio_formats()


def _frame_ms(fmt: str) -> int:
    try:
        return int(fmt.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def _group_names(value: object) -> list[str]:
    names: list[str] = []
    for raw in str(value or "").split(","):
        name = raw.strip()
        if name and name not in names:
            names.append(name)
    return names


@dataclass(slots=True)
class Endpoint:
    name: str
    ring_group: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    auto_answer: bool = False
    dnd: bool = False
    online: bool = True
    tx_formats: tuple[str, ...] = DEFAULT_AUDIO_FORMATS
    rx_formats: tuple[str, ...] = DEFAULT_AUDIO_FORMATS
    state: str = IDLE
    peer: str = ""
    selected_index: int = 0

    def endpoint_sensor(self) -> str:
        """Return a voip_stack endpoint sensor payload with group fields."""
        base = (
            f"{self.name}|192.0.2.{abs(hash(self.name)) % 200 + 1}|5060|40000|full_duplex|"
            f"{';'.join(self.tx_formats)}|{';'.join(self.rx_formats)}|sip_udp|"
            f"{self.name}|{self.conference_group}|{self.ring_group}"
        )
        return f"{base}|{'true' if self.conference_ring else 'false'}"

    @property
    def callable(self) -> bool:
        return self.online and not self.dnd and self.state == IDLE


@dataclass(slots=True)
class CallResult:
    kind: str
    caller: str
    target: str
    state: str
    winner: str = ""
    participants: list[str] = field(default_factory=list)
    ringing: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    audio: dict[str, str] = field(default_factory=dict)


class MiniPbx:
    """Small deterministic PBX used by tests to cover call-state combinations."""

    def __init__(self, endpoints: list[Endpoint]) -> None:
        self.endpoints = {endpoint.name: endpoint for endpoint in endpoints}
        self.manual_contacts: dict[str, dict] = {}
        self.sip_accounts: dict[str, dict] = {}
        self.trunks: dict[str, str] = {}
        self.ring_call: CallResult | None = None
        self.direct_calls: dict[str, CallResult] = {}
        self.conferences: dict[str, CallResult] = {}
        self.phonebook: dict[str, dict] = {}
        self.pushes: list[dict[str, dict]] = []

    def endpoint(self, name: str) -> Endpoint:
        return self.endpoints[name]

    def visible_contacts(self, endpoint: str) -> list[str]:
        if not self.phonebook:
            self.rebuild_phonebook()
        return sorted(name for name in self.phonebook if name != endpoint)

    def select_contact(self, endpoint: str, step: int = 1) -> str:
        contacts = self.visible_contacts(endpoint)
        if not contacts:
            return ""
        target = self.endpoint(endpoint)
        target.selected_index = (target.selected_index + step) % len(contacts)
        return contacts[target.selected_index]

    def current_contact(self, endpoint: str) -> str:
        contacts = self.visible_contacts(endpoint)
        if not contacts:
            return ""
        target = self.endpoint(endpoint)
        target.selected_index %= len(contacts)
        return contacts[target.selected_index]

    def rebuild_phonebook(self) -> dict[str, dict]:
        """Build the central phonebook from virtual endpoint sensors."""
        active = [endpoint for endpoint in self.endpoints.values() if endpoint.online]
        phonebook: dict[str, dict] = {
            endpoint.name: {
                "type": "endpoint",
                "name": endpoint.name,
                "ring_group": endpoint.ring_group,
                "conference_group": endpoint.conference_group,
                "conference_ring": endpoint.conference_ring,
            }
            for endpoint in active
        }
        for name, contact in self.manual_contacts.items():
            phonebook[name] = {"type": "contact", "name": name, **contact}
        for name, account in self.sip_accounts.items():
            if account.get("registered", True):
                phonebook[name] = {"type": "sip_account", "name": name, **account}
        ring_groups = sorted({group for endpoint in active for group in _group_names(endpoint.ring_group)})
        ring_groups.extend(
            group
            for contact in [*self.manual_contacts.values(), *self.sip_accounts.values()]
            for group in _group_names(contact.get("ring_group"))
        )
        conference_groups = sorted({group for endpoint in active for group in _group_names(endpoint.conference_group)})
        conference_groups.extend(
            group
            for contact in [*self.manual_contacts.values(), *self.sip_accounts.values()]
            for group in _group_names(contact.get("conference_group"))
        )
        ring_groups = sorted(set(ring_groups))
        conference_groups = sorted(set(conference_groups))
        for group in ring_groups:
            members = [endpoint.name for endpoint in active if group in _group_names(endpoint.ring_group)]
            members.extend(
                name
                for name, contact in [*self.manual_contacts.items(), *self.sip_accounts.items()]
                if group in _group_names(contact.get("ring_group")) and contact.get("registered", True)
            )
            if members:
                phonebook[group] = {"type": "ring", "name": group, "members": members}
        for group in conference_groups:
            members = [endpoint.name for endpoint in active if group in _group_names(endpoint.conference_group)]
            members.extend(
                name
                for name, contact in [*self.manual_contacts.items(), *self.sip_accounts.items()]
                if group in _group_names(contact.get("conference_group")) and contact.get("registered", True)
            )
            ring_members = [
                endpoint.name
                for endpoint in active
                if group in _group_names(endpoint.conference_group) and endpoint.conference_ring
            ]
            ring_members.extend(
                name
                for name, contact in [*self.manual_contacts.items(), *self.sip_accounts.items()]
                if group in _group_names(contact.get("conference_group"))
                and contact.get("conference_ring")
                and contact.get("registered", True)
            )
            if members:
                phonebook[group] = {
                    "type": "conference",
                    "name": group,
                    "members": members,
                    "ring_members": ring_members,
                }
        self.phonebook = phonebook
        self.pushes.append({key: dict(value) for key, value in phonebook.items()})
        return phonebook

    def add_contact(self, name: str, **metadata) -> dict[str, dict]:
        self.manual_contacts[name] = dict(metadata)
        return self.rebuild_phonebook()

    def remove_contact(self, name: str) -> dict[str, dict]:
        self.manual_contacts.pop(name, None)
        return self.rebuild_phonebook()

    def create_sip_account(self, name: str, **metadata) -> dict[str, dict]:
        self.sip_accounts[name] = {"registered": True, **metadata}
        return self.rebuild_phonebook()

    def remove_sip_account(self, name: str) -> dict[str, dict]:
        self.sip_accounts.pop(name, None)
        return self.rebuild_phonebook()

    def add_trunk(self, name: str, prefix: str = "+") -> None:
        self.trunks[name] = prefix

    def call_trunk(self, caller: str, number: str) -> CallResult:
        caller_ep = self.endpoint(caller)
        if not caller_ep.callable:
            return CallResult("trunk", caller, number, BUSY, events=["caller_busy"])
        if not self.trunks:
            return CallResult("trunk", caller, number, UNAVAILABLE, events=["trunk_unavailable"])
        audio = self.negotiate_audio(caller, "trunk")
        if audio is None:
            return CallResult("trunk", caller, number, MEDIA_INCOMPATIBLE, events=["488"])
        caller_ep.state = IN_CALL
        caller_ep.peer = number
        return CallResult("trunk", caller, number, IN_CALL, winner="trunk", events=["INVITE_TRUNK", "200:trunk"], audio=audio)

    def set_group_membership(
        self,
        endpoint: str,
        *,
        ring_group: str | None = None,
        conference_group: str | None = None,
        conference_ring: bool | None = None,
    ) -> dict[str, dict]:
        target = self.endpoint(endpoint)
        if ring_group is not None:
            target.ring_group = ring_group
        if conference_group is not None:
            target.conference_group = conference_group
        if conference_ring is not None:
            target.conference_ring = conference_ring
        return self.rebuild_phonebook()

    def set_online(self, endpoint: str, online: bool) -> dict[str, dict]:
        target = self.endpoint(endpoint)
        target.online = online
        if not online:
            target.state = IDLE
            target.peer = ""
        return self.rebuild_phonebook()

    def set_dnd(self, endpoint: str, dnd: bool) -> None:
        target = self.endpoint(endpoint)
        target.dnd = dnd
        if dnd and target.state == RINGING:
            target.state = IDLE
            target.peer = ""

    def call_selected(self, caller: str) -> CallResult:
        return self.call(caller, self.current_contact(caller))

    def call(self, caller: str, target: str) -> CallResult:
        if not self.phonebook:
            self.rebuild_phonebook()
        entry = self.phonebook.get(target)
        if entry is None:
            return CallResult("unknown", caller, target, UNAVAILABLE, events=["not_found"])
        if entry["type"] == "ring":
            return self.call_ring_group(caller, target)
        if entry["type"] == "conference":
            return self.call_conference_group(caller, target)
        return self.call_endpoint(caller, target)

    def call_endpoint(self, caller: str, callee: str) -> CallResult:
        caller_ep = self.endpoint(caller)
        callee_ep = self.endpoint(callee)
        if not caller_ep.callable:
            return CallResult("direct", caller, callee, BUSY, events=["caller_busy"])
        if not callee_ep.online:
            return CallResult("direct", caller, callee, UNAVAILABLE, events=["callee_offline"])
        if callee_ep.dnd or callee_ep.state != IDLE:
            return CallResult("direct", caller, callee, BUSY, events=["486"])
        audio = self.negotiate_audio(caller, callee)
        if audio is None:
            return CallResult("direct", caller, callee, MEDIA_INCOMPATIBLE, events=["488"])
        call_id = f"{caller}->{callee}"
        result = CallResult("direct", caller, callee, RINGING, ringing=[callee], events=["180"], audio=audio)
        self.direct_calls[call_id] = result
        caller_ep.state = RINGING
        caller_ep.peer = callee
        callee_ep.state = RINGING
        callee_ep.peer = caller
        if callee_ep.auto_answer:
            return self.answer_endpoint(callee, caller)
        return result

    def answer_endpoint(self, callee: str, caller: str) -> CallResult:
        call_id = f"{caller}->{callee}"
        result = self.direct_calls.get(call_id)
        if result is None or result.state != RINGING:
            return CallResult("direct", caller, callee, UNAVAILABLE)
        result.state = IN_CALL
        result.winner = callee
        result.ringing = []
        result.events.append(f"200:{callee}")
        self.endpoint(caller).state = IN_CALL
        self.endpoint(caller).peer = callee
        self.endpoint(callee).state = IN_CALL
        self.endpoint(callee).peer = caller
        return result

    def cancel_endpoint(self, caller: str, callee: str) -> CallResult:
        call_id = f"{caller}->{callee}"
        result = self.direct_calls.get(call_id)
        if result is None:
            return CallResult("direct", caller, callee, UNAVAILABLE)
        if result.state == RINGING:
            result.state = CANCELLED
            result.cancelled = [callee]
            result.ringing = []
            result.events.append(f"CANCEL:{callee}")
            for name in (caller, callee):
                endpoint = self.endpoint(name)
                endpoint.state = IDLE
                endpoint.peer = ""
        return result

    def hangup_endpoint(self, who: str) -> CallResult:
        endpoint = self.endpoint(who)
        other = endpoint.peer
        if not other:
            return CallResult("direct", who, "", UNAVAILABLE)
        caller, callee = (who, other) if f"{who}->{other}" in self.direct_calls else (other, who)
        result = self.direct_calls.get(f"{caller}->{callee}", CallResult("direct", caller, callee, IN_CALL))
        if result.state == RINGING:
            return self.cancel_endpoint(caller, callee)
        result.state = ENDED
        result.events.extend([f"BYE:{who}", f"BYE:{other}"])
        for name in (who, other):
            target = self.endpoint(name)
            target.state = IDLE
            target.peer = ""
        return result

    def disconnect(self, endpoint: str) -> list[CallResult]:
        """Simulate abrupt endpoint disappearance and cleanup the opposite legs."""
        target = self.endpoint(endpoint)
        peer_name = target.peer
        target.online = False
        target.state = IDLE
        target.peer = ""
        affected: list[CallResult] = []
        if peer_name and peer_name in self.endpoints:
            peer_ep = self.endpoint(peer_name)
            peer_ep.state = IDLE
            peer_ep.peer = ""
            affected.append(CallResult("disconnect", endpoint, peer_name, ENDED, events=[f"DISCONNECT:{endpoint}", f"BYE:{peer_name}"]))
        for room_name, room in list(self.conferences.items()):
            if endpoint in room.participants:
                affected.append(self.leave_conference(endpoint, room_name))
        self.rebuild_phonebook()
        return affected

    def ring_group_members(self, group: str, *, exclude: str = "") -> list[str]:
        return [
            endpoint.name
            for endpoint in self.endpoints.values()
            if group in _group_names(endpoint.ring_group) and endpoint.name != exclude and endpoint.callable
        ]

    def negotiate_audio(self, caller: str, callee: str) -> dict[str, str] | None:
        caller_ep = self.endpoints.get(caller)
        callee_ep = self.endpoints.get(callee)
        if caller_ep is None:
            return None
        if callee == "trunk":
            trunk_formats = DEFAULT_AUDIO_FORMATS
            send_candidates = [fmt for fmt in caller_ep.tx_formats if fmt in trunk_formats]
            recv_candidates = [fmt for fmt in trunk_formats if fmt in caller_ep.rx_formats]
        elif callee_ep is not None:
            send_candidates = [fmt for fmt in caller_ep.tx_formats if fmt in callee_ep.rx_formats]
            recv_candidates = [fmt for fmt in callee_ep.tx_formats if fmt in caller_ep.rx_formats]
        else:
            entry = self.phonebook.get(callee, {})
            formats = tuple(entry.get("formats") or DEFAULT_AUDIO_FORMATS)
            send_candidates = [fmt for fmt in caller_ep.tx_formats if fmt in formats]
            recv_candidates = [fmt for fmt in formats if fmt in caller_ep.rx_formats]
        return self._choose_audio_pair(send_candidates, recv_candidates)

    @staticmethod
    def _choose_audio_pair(send_candidates: list[str], recv_candidates: list[str]) -> dict[str, str] | None:
        for send in send_candidates:
            send_frame = _frame_ms(send)
            if not send_frame:
                continue
            for recv in recv_candidates:
                if _frame_ms(recv) == send_frame:
                    return {"send": send, "recv": recv, "ptime": str(send_frame)}
        return None

    def conference_ring_members(self, group: str, *, exclude: str = "") -> list[str]:
        return [
            endpoint.name
            for endpoint in self.endpoints.values()
            if group in _group_names(endpoint.conference_group)
            and endpoint.conference_ring
            and endpoint.name != exclude
            and endpoint.callable
        ]

    def call_ring_group(self, caller: str, group: str) -> CallResult:
        caller_ep = self.endpoint(caller)
        if not caller_ep.callable:
            return CallResult("ring_group", caller, group, BUSY)
        members = self.ring_group_members(group, exclude=caller)
        if not members:
            return CallResult("ring_group", caller, group, UNAVAILABLE, events=["no_members"])
        caller_ep.state = RINGING
        result = CallResult("ring_group", caller, group, RINGING, ringing=list(members), events=["180"])
        self.ring_call = result
        for member in members:
            endpoint = self.endpoint(member)
            endpoint.state = RINGING
            endpoint.peer = caller
        for member in members:
            if self.endpoint(member).auto_answer:
                self.answer_ring_group(member)
                break
        return result

    def answer_ring_group(self, member: str) -> CallResult:
        result = self.ring_call
        if result is None or member not in result.ringing or result.state != RINGING:
            return CallResult("ring_group", member, "", UNAVAILABLE)
        audio = self.negotiate_audio(result.caller, member)
        if audio is None:
            result.events.append(f"488:{member}")
            result.ringing.remove(member)
            endpoint = self.endpoint(member)
            endpoint.state = IDLE
            endpoint.peer = ""
            return result
        result.state = IN_CALL
        result.winner = member
        result.audio = audio
        result.events.append(f"200:{member}")
        result.cancelled = [candidate for candidate in result.ringing if candidate != member]
        result.ringing = []
        caller_ep = self.endpoint(result.caller)
        winner_ep = self.endpoint(member)
        caller_ep.state = IN_CALL
        caller_ep.peer = member
        winner_ep.state = IN_CALL
        winner_ep.peer = result.caller
        for loser in result.cancelled:
            loser_ep = self.endpoint(loser)
            loser_ep.state = IDLE
            loser_ep.peer = ""
            result.events.append(f"CANCEL:{loser}")
        return result

    def caller_cancels_ring_group(self) -> CallResult:
        result = self.ring_call
        if result is None:
            return CallResult("ring_group", "", "", UNAVAILABLE)
        if result.state == RINGING:
            result.state = CANCELLED
            result.cancelled = list(result.ringing)
            result.events.extend(f"CANCEL:{member}" for member in result.cancelled)
            for name in [result.caller, *result.cancelled]:
                endpoint = self.endpoint(name)
                endpoint.state = IDLE
                endpoint.peer = ""
            result.ringing = []
        return result

    def hangup_ring_group(self, who: str) -> CallResult:
        result = self.ring_call
        if result is None:
            return CallResult("ring_group", who, "", UNAVAILABLE)
        if result.state == RINGING and who == result.caller:
            return self.caller_cancels_ring_group()
        if result.state == IN_CALL and who in {result.caller, result.winner}:
            other = result.winner if who == result.caller else result.caller
            result.state = ENDED
            result.events.extend([f"BYE:{who}", f"BYE:{other}"])
            for name in (result.caller, result.winner):
                endpoint = self.endpoint(name)
                endpoint.state = IDLE
                endpoint.peer = ""
        return result

    def call_conference_group(self, caller: str, group: str) -> CallResult:
        caller_ep = self.endpoint(caller)
        if not caller_ep.callable:
            return CallResult("conference_group", caller, group, BUSY)
        result = self.conferences.get(group)
        if result is None or result.state == ENDED:
            result = CallResult("conference_group", caller, group, IN_CALL, events=["room_started"])
            self.conferences[group] = result
        if caller not in result.participants:
            result.participants.append(caller)
            result.events.append(f"JOIN:{caller}")
        caller_ep.state = IN_CALL
        caller_ep.peer = group
        ring_members = self.conference_ring_members(group, exclude=caller)
        result.ringing = [member for member in ring_members if member not in result.participants]
        for member in result.ringing:
            endpoint = self.endpoint(member)
            endpoint.state = RINGING
            endpoint.peer = group
            result.events.append(f"RING:{member}")
        for member in list(result.ringing):
            if self.endpoint(member).auto_answer:
                self.answer_conference_invite(member, group)
        return result

    def answer_conference_invite(self, member: str, group: str) -> CallResult:
        result = self.conferences[group]
        if member not in result.ringing:
            return result
        result.ringing.remove(member)
        if member not in result.participants:
            result.participants.append(member)
        endpoint = self.endpoint(member)
        endpoint.state = IN_CALL
        endpoint.peer = group
        result.events.append(f"JOIN:{member}")
        return result

    def leave_conference(self, member: str, group: str) -> CallResult:
        result = self.conferences[group]
        if member in result.participants:
            result.participants.remove(member)
            result.events.append(f"LEAVE:{member}")
        endpoint = self.endpoint(member)
        endpoint.state = IDLE
        endpoint.peer = ""
        if not result.participants:
            result.state = ENDED
            result.ringing = []
            result.events.append("room_ended")
            for candidate in self.endpoints.values():
                if candidate.peer == group:
                    candidate.state = IDLE
                    candidate.peer = ""
        return result

    def active_runtime_summary(self) -> dict[str, object]:
        """Return active runtime state that should be empty after cleanup."""
        return {
            "active_endpoints": {
                name: {"state": endpoint.state, "peer": endpoint.peer}
                for name, endpoint in self.endpoints.items()
                if endpoint.state != IDLE or endpoint.peer
            },
            "active_ring": asdict(self.ring_call) if self.ring_call is not None and self.ring_call.state in {RINGING, IN_CALL} else None,
            "active_conferences": {
                name: asdict(room)
                for name, room in self.conferences.items()
                if room.state != ENDED and (room.participants or room.ringing)
            },
        }


def build_default_pbx() -> MiniPbx:
    pbx = MiniPbx(
        [
            Endpoint(
                "Casa",
                ring_group="RG Casa",
                conference_group="CG Casa",
                conference_ring=True,
                tx_formats=HA_AUDIO_FORMATS,
                rx_formats=HA_AUDIO_FORMATS,
            ),
            Endpoint(
                "Spotpear",
                ring_group="RG Casa",
                conference_group="CG Casa",
                tx_formats=SPOTPEAR_AUDIO_FORMATS,
                rx_formats=SPOTPEAR_AUDIO_FORMATS,
            ),
            Endpoint("WS3", ring_group="RG Casa", conference_group="CG Casa"),
            Endpoint("Zoiper", ring_group="RG Casa", conference_group="CG Casa", conference_ring=True),
        ]
    )
    pbx.rebuild_phonebook()
    return pbx


def run_scenario(name: str) -> dict:
    pbx = build_default_pbx()
    if name == "phonebook":
        return {"scenario": name, "phonebook": pbx.phonebook, "pushes": len(pbx.pushes)}
    if name == "ring-manual":
        call = pbx.call_ring_group("Casa", "RG Casa")
        answer = pbx.answer_ring_group("Spotpear")
        return {"scenario": name, "ringing": call.ringing, "result": asdict(answer)}
    if name == "ring-cancel":
        pbx.call_ring_group("Casa", "RG Casa")
        return {"scenario": name, "result": asdict(pbx.caller_cancels_ring_group())}
    if name == "ring-auto":
        pbx.endpoint("Spotpear").auto_answer = True
        return {"scenario": name, "result": asdict(pbx.call_ring_group("Casa", "RG Casa"))}
    if name == "conference":
        start = pbx.call_conference_group("Spotpear", "CG Casa")
        pbx.answer_conference_invite("Casa", "CG Casa")
        join = pbx.call_conference_group("WS3", "CG Casa")
        return {"scenario": name, "start": asdict(start), "join": asdict(join)}
    if name == "direct-disconnect":
        pbx.endpoint("Spotpear").auto_answer = True
        pbx.call_endpoint("WS3", "Spotpear")
        return {"scenario": name, "affected": [asdict(item) for item in pbx.disconnect("Spotpear")], "phonebook": pbx.phonebook}
    if name == "direct-lifecycle":
        pbx.endpoint("Spotpear").auto_answer = True
        answered = pbx.call_endpoint("WS3", "Spotpear")
        answered_snapshot = asdict(answered)
        ended = pbx.hangup_endpoint("WS3")
        ended_snapshot = asdict(ended)
        pbx.endpoint("Spotpear").auto_answer = False
        ringing = pbx.call_endpoint("WS3", "Spotpear")
        ringing_snapshot = asdict(ringing)
        cancelled = pbx.cancel_endpoint("WS3", "Spotpear")
        cancelled_snapshot = asdict(cancelled)
        return {
            "scenario": name,
            "answered": answered_snapshot,
            "ended": ended_snapshot,
            "ringing": ringing_snapshot,
            "cancelled": cancelled_snapshot,
        }
    if name == "selection":
        contacts = pbx.visible_contacts("WS3")
        for _ in range(len(contacts)):
            if pbx.current_contact("WS3") == "Spotpear":
                break
            pbx.select_contact("WS3")
        selected = pbx.current_contact("WS3")
        call = pbx.call_selected("WS3")
        return {"scenario": name, "contacts": contacts, "selected": selected, "call": asdict(call)}
    if name == "services":
        pbx.add_contact("Desk SIP", sip_uri="sip:desk@192.168.1.60:5060", ring_group="RG Casa, RG Desk")
        pbx.create_sip_account("MobileOffice", conference_group="CG Casa, CG Mobile", conference_ring=True)
        pbx.add_trunk("Wildix")
        trunk = pbx.call_trunk("Casa", "+12025550100")
        return {"scenario": name, "phonebook": pbx.phonebook, "pushes": len(pbx.pushes), "trunk": asdict(trunk)}
    if name == "services-delete":
        pbx.add_contact("Desk SIP", sip_uri="sip:desk@192.168.1.60:5060", ring_group="RG Casa")
        pbx.create_sip_account("MobileOffice", conference_group="CG Casa", conference_ring=True)
        with_services = dict(pbx.phonebook)
        pbx.remove_contact("Desk SIP")
        pbx.remove_sip_account("MobileOffice")
        return {
            "scenario": name,
            "with_services": with_services,
            "after_delete": pbx.phonebook,
            "pushes": len(pbx.pushes),
        }
    if name == "dynamic-groups":
        initial = dict(pbx.phonebook)
        pbx.set_group_membership("Casa", ring_group="", conference_group="", conference_ring=False)
        without_ha = dict(pbx.phonebook)
        for endpoint in ("Spotpear", "WS3", "Zoiper"):
            pbx.set_online(endpoint, False)
        return {
            "scenario": name,
            "initial": initial,
            "without_ha": without_ha,
            "after_offline": pbx.phonebook,
            "pushes": len(pbx.pushes),
        }
    if name == "errors":
        unknown = pbx.call("Casa", "Missing")
        pbx.set_dnd("Spotpear", True)
        dnd = pbx.call_endpoint("WS3", "Spotpear")
        pbx.set_dnd("Spotpear", False)
        pbx.set_online("Spotpear", False)
        offline = pbx.call_endpoint("WS3", "Spotpear")
        no_trunk = pbx.call_trunk("Casa", "+12025550100")
        return {
            "scenario": name,
            "unknown": asdict(unknown),
            "dnd": asdict(dnd),
            "offline": asdict(offline),
            "no_trunk": asdict(no_trunk),
        }
    if name == "audio":
        ws3_to_ha = pbx.negotiate_audio("WS3", "Casa")
        ws3_to_spotpear = pbx.negotiate_audio("WS3", "Spotpear")
        pbx.endpoint("Casa").tx_formats = ("48000:s16le:1:10", "16000:s16le:1:20")
        pbx.endpoint("Casa").rx_formats = ("48000:s16le:1:10", "16000:s16le:1:20")
        ha_to_ws3_mixed_order = pbx.negotiate_audio("Casa", "WS3")
        pbx.endpoint("WS3").tx_formats = ("16000:s16le:1:20",)
        pbx.endpoint("WS3").rx_formats = ("48000:s16le:1:10",)
        asymmetric_no_common_ptime = pbx.call_endpoint("Casa", "WS3")
        pbx.endpoint("Spotpear").rx_formats = ("16000:s16le:1:32",)
        incompatible = pbx.call_endpoint("WS3", "Spotpear")
        return {
            "scenario": name,
            "ws3_to_ha": ws3_to_ha,
            "ws3_to_spotpear": ws3_to_spotpear,
            "ha_to_ws3_mixed_order": ha_to_ws3_mixed_order,
            "asymmetric_no_common_ptime": asdict(asymmetric_no_common_ptime),
            "incompatible": asdict(incompatible),
        }
    if name == "chaos":
        double_answer_rejected = 0
        owner_leave_kept_room = 0
        for idx in range(100):
            pbx.call_ring_group("Casa", "RG Casa")
            pbx.caller_cancels_ring_group()

            pbx.call_ring_group("Casa", "RG Casa")
            won = pbx.answer_ring_group("Spotpear")
            late = pbx.answer_ring_group("WS3")
            if won.state == IN_CALL and won.winner == "Spotpear" and late.state == UNAVAILABLE:
                double_answer_rejected += 1
            pbx.hangup_ring_group("Casa")

            pbx.endpoint("Spotpear").auto_answer = True
            pbx.call_endpoint("WS3", "Spotpear")
            pbx.disconnect("Spotpear")
            pbx.set_online("Spotpear", True)
            pbx.endpoint("Spotpear").auto_answer = False

            room = pbx.call_conference_group("Spotpear", "CG Casa")
            pbx.answer_conference_invite("Casa", "CG Casa")
            pbx.call_conference_group("WS3", "CG Casa")
            room = pbx.leave_conference("Spotpear", "CG Casa")
            if room.state == IN_CALL and set(room.participants) == {"Casa", "WS3"}:
                owner_leave_kept_room += 1
            pbx.leave_conference("Casa", "CG Casa")
            pbx.leave_conference("WS3", "CG Casa")

            pbx.set_group_membership(
                "Casa",
                ring_group=f"RG Temp {idx}",
                conference_group=f"CG Temp {idx}",
                conference_ring=bool(idx % 2),
            )
            pbx.set_group_membership("Casa", ring_group="RG Casa", conference_group="CG Casa", conference_ring=True)

        return {
            "scenario": name,
            "double_answer_rejected": double_answer_rejected,
            "owner_leave_kept_room": owner_leave_kept_room,
            "runtime": pbx.active_runtime_summary(),
            "pushes": len(pbx.pushes),
        }
    raise ValueError(f"unknown scenario: {name}")


SCENARIO_NAMES = (
    "phonebook",
    "ring-manual",
    "ring-cancel",
    "ring-auto",
    "conference",
    "direct-lifecycle",
    "selection",
    "direct-disconnect",
    "services",
    "services-delete",
    "dynamic-groups",
    "errors",
    "audio",
    "chaos",
)


def validate_result(result: dict) -> list[str]:
    scenario = result["scenario"]
    errors: list[str] = []
    if scenario == "phonebook":
        phonebook = result["phonebook"]
        if "RG Casa" not in phonebook or "CG Casa" not in phonebook:
            errors.append("phonebook did not expose both group entries")
        if "Casa" not in phonebook.get("CG Casa", {}).get("ring_members", []):
            errors.append("HA virtual endpoint is not a conference ring member")
    elif scenario == "ring-manual":
        call = result["result"]
        if call["state"] != IN_CALL or call["winner"] != "Spotpear":
            errors.append("manual ring group answer did not connect Spotpear")
        if "CANCEL:WS3" not in call["events"]:
            errors.append("ring group winner did not cancel remaining ESP leg")
        if not call["audio"]:
            errors.append("ring group winner did not negotiate audio")
    elif scenario == "ring-cancel":
        call = result["result"]
        if call["state"] != CANCELLED:
            errors.append("caller cancel did not cancel ring group")
        if not all(event.startswith("CANCEL:") or event == "180" for event in call["events"]):
            errors.append("ring cancel emitted unexpected event")
    elif scenario == "ring-auto":
        call = result["result"]
        if call["state"] != IN_CALL or "200:Spotpear" not in call["events"]:
            errors.append("auto-answer ring group did not connect first auto endpoint")
    elif scenario == "conference":
        join = result["join"]
        if join["state"] != IN_CALL or "WS3" not in join["participants"]:
            errors.append("conference did not accept later manual join")
        if "RING:Casa" not in join["events"]:
            errors.append("conference did not ring HA virtual endpoint")
    elif scenario == "direct-disconnect":
        affected = result["affected"]
        if not affected or "DISCONNECT:Spotpear" not in affected[0]["events"]:
            errors.append("disconnect did not clear the opposite leg")
        if "Spotpear" in result["phonebook"]:
            errors.append("offline endpoint remained in phonebook")
    elif scenario == "direct-lifecycle":
        if result["answered"]["state"] != IN_CALL or result["answered"]["winner"] != "Spotpear":
            errors.append("direct auto-answer did not connect")
        if result["ended"]["state"] != ENDED or "BYE:WS3" not in result["ended"]["events"]:
            errors.append("direct hangup did not propagate BYE")
        if result["ringing"]["state"] != RINGING or result["cancelled"]["state"] != CANCELLED:
            errors.append("direct manual ringing/cancel flow failed")
    elif scenario == "selection":
        if "Spotpear" not in result["contacts"] or "RG Casa" not in result["contacts"] or "CG Casa" not in result["contacts"]:
            errors.append("contact list did not expose endpoints and groups")
        if result["selected"] != "Spotpear" or result["call"]["target"] != "Spotpear":
            errors.append("selected contact did not drive the outbound call target")
    elif scenario == "services":
        phonebook = result["phonebook"]
        if "Desk SIP" not in phonebook.get("RG Casa", {}).get("members", []):
            errors.append("manual contact was not added to ring group")
        if "Desk SIP" not in phonebook.get("RG Desk", {}).get("members", []):
            errors.append("comma-separated manual contact ring group was not expanded")
        if "MobileOffice" not in phonebook.get("CG Casa", {}).get("ring_members", []):
            errors.append("registered SIP account was not added as conference ring member")
        if "MobileOffice" not in phonebook.get("CG Mobile", {}).get("ring_members", []):
            errors.append("comma-separated SIP account conference group was not expanded")
        if result["trunk"]["state"] != IN_CALL:
            errors.append("trunk simulation did not connect")
    elif scenario == "services-delete":
        if "Desk SIP" not in result["with_services"].get("RG Casa", {}).get("members", []):
            errors.append("manual contact setup did not enter ring group before deletion")
        if "Desk SIP" in result["after_delete"]:
            errors.append("manual contact persisted after deletion")
        if "MobileOffice" in result["after_delete"]:
            errors.append("SIP account persisted after deletion")
        if result["pushes"] < 5:
            errors.append("service create/delete did not trigger enough phonebook pushes")
    elif scenario == "dynamic-groups":
        if "Casa" not in result["initial"].get("RG Casa", {}).get("members", []):
            errors.append("HA endpoint was not initially in ring group")
        if "Casa" in result["without_ha"].get("RG Casa", {}).get("members", []):
            errors.append("HA endpoint group update did not rebuild phonebook")
        if "RG Casa" in result["after_offline"] or "CG Casa" in result["after_offline"]:
            errors.append("empty dynamic groups persisted after endpoints went offline")
    elif scenario == "errors":
        for key, expected in {
            "unknown": UNAVAILABLE,
            "dnd": BUSY,
            "offline": UNAVAILABLE,
            "no_trunk": UNAVAILABLE,
        }.items():
            if result[key]["state"] != expected:
                errors.append(f"{key} expected {expected}, got {result[key]['state']}")
    elif scenario == "audio":
        if result["ws3_to_ha"] != {"send": "16000:s16le:1:20", "recv": "16000:s16le:1:20", "ptime": "20"}:
            errors.append("WS3-HA audio negotiation did not select common 20 ms profile")
        if result["ws3_to_spotpear"] != {"send": "16000:s16le:1:20", "recv": "16000:s16le:1:20", "ptime": "20"}:
            errors.append("WS3-Spotpear audio negotiation did not select Spotpear-compatible profile")
        if result["ha_to_ws3_mixed_order"] != {"send": "48000:s16le:1:10", "recv": "48000:s16le:1:10", "ptime": "10"}:
            errors.append("HA-WS3 audio negotiation did not skip mismatched first receive candidate")
        if result["asymmetric_no_common_ptime"]["state"] != MEDIA_INCOMPATIBLE:
            errors.append("directional audio without common ptime did not fail with media_incompatible")
        if result["incompatible"]["state"] != MEDIA_INCOMPATIBLE:
            errors.append("incompatible audio profile did not fail with media_incompatible")
    elif scenario == "chaos":
        if result["double_answer_rejected"] != 100:
            errors.append("ring group did not reject every late second answer")
        if result["owner_leave_kept_room"] != 100:
            errors.append("conference owner leave did not keep every non-empty room alive")
        runtime = result["runtime"]
        if runtime["active_endpoints"] or runtime["active_ring"] or runtime["active_conferences"]:
            errors.append(f"chaos scenario left active runtime state: {runtime}")
        if result["pushes"] < 200:
            errors.append("group churn did not rebuild and push phonebook changes")
    else:
        errors.append(f"no validator for scenario {scenario}")
    return errors


def run_matrix(names: tuple[str, ...] = SCENARIO_NAMES) -> tuple[list[dict], list[str]]:
    results = [run_scenario(name) for name in names]
    errors = []
    for result in results:
        errors.extend(f"{result['scenario']}: {error}" for error in validate_result(result))
    return results, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIO_NAMES, "all"), default="all")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--validate", action="store_true", help="return non-zero on semantic matrix failures")
    args = parser.parse_args(argv)
    names = SCENARIO_NAMES if args.scenario == "all" else (args.scenario,)
    try:
        results, errors = run_matrix(names)
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for result in results:
            print(f"{result['scenario']}: ok")
    if errors:
        for error in errors:
            print(f"matrix error: {error}", file=sys.stderr)
        return 1 if args.validate else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
