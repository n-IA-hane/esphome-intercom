"""Correlate an internal SIP leg with the call and account that own it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .endpoint_session import LegPhase
from .trunk_policy import trunk_inbound_source_available

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .core.codec_policy import CodecPolicy
    from .core.sip import SipMessage
    from .endpoint_registry import EndpointRegistry
    from .pbx_runtime import SipEndpointRuntime
    from .phone_endpoint import PhoneEndpoint
    from .sip_listener import SipInitialInvite, SipInvite, SipRequestResult
    from .sip_registrar import SipRegistrar


def owned_local_call_source(
    invite: SipInitialInvite,
    calls: SipEndpointRuntime,
    endpoints: EndpointRegistry,
    *,
    listener_hosts: set[str] | frozenset[str],
    listener_port: int,
) -> PhoneEndpoint | None:
    """Return the current HA source only when its actual SIP client owns the leg."""
    if invite.received_via_trunk:
        return None
    client = calls.sip_client_for(invite.call_id)
    session = calls.get_session(invite.call_id)
    if (client is None or session is None or not session.live
            or session.metadata.get("inbound_trunk_id")):
        return None
    identity = str(session.metadata.get("source_endpoint_id") or "")
    leg = session.legs.get(invite.call_id)
    source = endpoints.get(identity) if identity else None
    if (source is not None and source.supports_ha_client and source.active_call_id == session.call_id
            and session.endpoint_claims.get(identity) == "source"
            and leg is not None and leg.dialog is client and leg.role == "ha_softphone"
            and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}
            and calls.get_session(session.call_id, generation=session.generation) is session
            and client.matches_local_invite_source(
                invite, listener_hosts=listener_hosts, listener_port=listener_port,
            )):
        return source
    return None


def classify_trunk_invite(invite, *, enabled: bool, trunk, trunks=None):
    """Apply the authoritative trunk source classification."""

    if trunks is not None:
        from .trunk_policy import identify_inbound_trunk

        identity = identify_inbound_trunk(
            trunks, invite.source_host, invite.source_port, invite.signaling_transport,
            flow_trunk_id=invite.trunk_id if invite.received_via_trunk else "",
            contact_line=str(dict(invite.request_uri.params).get("line") or ""),
        ) if enabled else ""
        return replace(invite, received_via_trunk=bool(identity), trunk_id=identity)
    trusted = False
    if enabled and trunk_inbound_source_available(trunk):
        if invite.received_via_trunk:
            trusted = True
        else:
            accepts_source = getattr(trunk, "accepts_inbound_source", None)
            trusted = bool(
                callable(accepts_source)
                and accepts_source(
                    invite.source_host,
                    invite.source_port,
                    invite.signaling_transport,
                )
            )
    if invite.received_via_trunk == trusted:
        return invite
    return replace(invite, received_via_trunk=trusted)


@dataclass(frozen=True, slots=True)
class SipIngressPolicy:
    """Use the existing registrar and trunk catalog for listener admission and media policy."""

    hass: HomeAssistant
    config: dict[str, Any]
    local_ip: str
    registrar: SipRegistrar

    def classify(self, invite):
        if "audio_rtcp_min_interval" in self.config:
            invite = replace(invite, audio_rtcp_min_interval=float(self.config["audio_rtcp_min_interval"]))
        from .config import trunk_config, trunk_enabled
        from .runtime_data import runtime_data, sip_trunk, sip_trunks

        if invite.identified_source_endpoint_id or invite.authenticated_username:
            return invite
        runtime = runtime_data(self.hass)
        if runtime is not None and runtime.trunk_configs:
            return classify_trunk_invite(invite, enabled=True, trunk=None, trunks=sip_trunks(self.hass))
        trunk_cfg = trunk_config(self.hass)
        trunk = sip_trunk(self.hass)
        return classify_trunk_invite(
            invite,
            enabled=trunk_enabled(trunk_cfg),
            trunk=trunk,
        )

    def is_trunk(self, invite: SipInvite) -> bool:
        return self.classify(invite).received_via_trunk

    def authorize_initial_invite(
        self, request: SipMessage, invite: SipInitialInvite, addr: tuple[str, int], transport: str,
    ) -> SipInitialInvite | SipRequestResult:
        """Bind admission to a configured trunk, owned local call or Digest account."""
        from .core.sip import make_tag
        from .core.sip_auth import parse_digest_challenge
        from .endpoint_lifecycle import call_registry
        from .runtime_data import endpoint_directory, runtime_data, sip_trunks
        from .sip_listener import SipRequestResult
        from .sip_registrar import REALM
        from .trunk_policy import authenticated_inbound_trunk

        if "audio_rtcp_min_interval" in self.config:
            invite = replace(invite, audio_rtcp_min_interval=float(self.config["audio_rtcp_min_interval"]))
        runtime = runtime_data(self.hass)
        configs = runtime.trunk_configs if runtime is not None else {}
        auth = parse_digest_challenge(request.header("Authorization"))
        local_digest = auth.get("realm") == REALM
        try:
            classified = self.classify(invite)
        except ValueError:
            classified = None
        # A bound peer proves its account before source-address or owned-flow
        # trust can admit its presented caller. A shared NAT address is not the
        # peer account, and an explicit local Digest attempt cannot be ignored.
        bound_source = any(
            configs.get(identity, {}).get("trunk_inbound_account")
            and (identity == invite.trunk_id if invite.received_via_trunk else
                 client.accepts_inbound_source(invite.source_host, invite.source_port, transport))
            for identity, client in sip_trunks(self.hass).items()
        )
        if (classified is not None and invite.received_via_trunk and classified.received_via_trunk
                and not local_digest and not bound_source):
            return classified

        source = owned_local_call_source(invite, call_registry(self.hass), endpoint_directory(self.hass),
                                         listener_hosts={self.local_ip, "127.0.0.1", "::1"},
                                         listener_port=int(self.config["sip_port"]))
        if source is not None:
            return replace(invite, identified_source_endpoint_id=source.endpoint_id,
                           caller_route=source.username or source.name)

        if classified is not None and classified.received_via_trunk:
            if not local_digest and not bound_source:
                return classified
        elif classified is None and not bound_source:
            # A shared signaling address cannot choose a provider. An account
            # on that address can still prove its own identity using Digest.
            claimed = self.registrar.accounts.get(str(auth.get("username") or
                (invite.caller_uri.user if invite.caller_uri else "")).casefold())
            if not self.registrar.enabled or claimed is None or not claimed.enabled:
                return SipRequestResult(403, "Ambiguous trunk source", to_tag=make_tag())
        remaining = self.registrar.source_block_remaining(addr[0])
        if remaining:
            return SipRequestResult(403, "Forbidden", headers=(("Retry-After", str(remaining)),), to_tag=make_tag())
        account, challenge, tag = self.registrar.authorize_invite(request, addr, transport)
        if account is None:
            remaining = self.registrar.source_block_remaining(addr[0])
            if remaining:
                return SipRequestResult(403, "Forbidden", headers=(("Retry-After", str(remaining)),), to_tag=tag)
            return SipRequestResult(401, "Unauthorized", headers=(("WWW-Authenticate", challenge),), to_tag=tag)
        try:
            peer_trunk = authenticated_inbound_trunk(configs, account.username)
        except ValueError:
            return SipRequestResult(403, "Invalid incoming trunk account", to_tag=make_tag())
        if peer_trunk:
            if invite.caller_uri is None:
                return SipRequestResult(403, "Forbidden", to_tag=make_tag())
            return replace(invite, received_via_trunk=True, trunk_id=peer_trunk,
                           identified_source_endpoint_id="", authenticated_username=account.username,
                           caller_route=invite.caller_uri.user)
        source = endpoint_directory(self.hass).by_username(account.username)
        if (invite.caller_uri is None or invite.caller_uri.user.casefold() != account.username.casefold()
                or (source is None and not self.registrar.registered_contacts(account.username))):
            return SipRequestResult(403, "Forbidden", to_tag=make_tag())
        return replace(invite, received_via_trunk=False, trunk_id="",
                       identified_source_endpoint_id=source.endpoint_id if source is not None else "",
                       authenticated_username=account.username, caller_route=account.username)

    def identity_for_invite(self, request: SipMessage, initial: SipInitialInvite) -> SipInitialInvite:
        from .config import trunk_config
        from .inbound_identity import apply_inbound_identity
        from .roster import normalize_roster_key
        from .runtime_data import runtime_data

        try:
            classified = self.classify(initial)
        except ValueError:
            return initial  # The routing boundary rejects ambiguous sources.
        if classified.received_via_trunk:
            config = (trunk_config(self.hass, trunk_id=classified.trunk_id)
                      if classified.trunk_id else trunk_config(self.hass))
            classified = apply_inbound_identity(classified, config, request)
        elif not (classified.identified_source_endpoint_id or classified.authenticated_username):
            return classified

        user = classified.caller_number
        display = classified.caller_name
        number_key = normalize_roster_key(user)
        if not number_key or (display and not (number_key.isdecimal() and normalize_roster_key(display) == number_key)):
            return classified
        if (user.casefold() in {"anonymous", "restricted", "unavailable", "unknown", "withheld", "private"}
                or (classified.caller_uri and classified.caller_uri.host.casefold() == "anonymous.invalid")
                or any(value.strip().casefold() not in {"", "none"} for value in request.header_values("Privacy"))):
            return classified
        runtime = runtime_data(self.hass)
        if runtime is None:
            return classified
        source = runtime.endpoints.get(classified.identified_source_endpoint_id)
        name = source.name if source is not None else ""
        if not name and runtime.directory is not None and not runtime.directory.closed:
            name = runtime.directory.caller_display_name(user)
        return replace(classified, caller=name) if name else classified

    def codec_policy_for_invite(self, initial: SipInitialInvite) -> CodecPolicy:
        from .codec_settings import effective_codec_policy
        from .core.codec_policy import CodecPolicy
        try:
            classified = self.classify(initial)
        except ValueError:
            return CodecPolicy()  # The routing boundary rejects ambiguous sources.
        if classified.received_via_trunk:
            return effective_codec_policy(self.hass, trunk_id=classified.trunk_id or "")
        endpoint_id = classified.identified_source_endpoint_id
        if not endpoint_id and classified.authenticated_username:
            from .runtime_data import endpoint_directory

            endpoint = endpoint_directory(self.hass).by_username(classified.authenticated_username)
            endpoint_id = endpoint.endpoint_id if endpoint is not None else ""
        return effective_codec_policy(self.hass, endpoint_id=endpoint_id)
