"""RFC 4235 full-state documents derived from the existing call registry."""

from dataclasses import dataclass
from hashlib import sha256
import re
from xml.etree import ElementTree

from .core import sip
from .endpoint_session import LegPhase


@dataclass(frozen=True, slots=True)
class DialogState:
    identity: str
    state: str
    direction: str
    call_id: str = ""
    local_tag: str = ""
    remote_tag: str = ""
    held: bool = False
    session_id: str = ""
    generation: int = 0
    leg_id: str = ""
    peer_endpoint_id: str = ""
    peer_name: str = ""
    peer_user: str = ""
    local_uri: str = ""
    remote_uri: str = ""
    local_target: str = ""
    remote_target: str = ""
    rendering: str = ""


def subscription_filter(event_header):
    """Interpret the RFC 4235 dialog identifiers, preserving their case."""
    _package, pairs = sip._split_semicolon_params(event_header)
    values = {}
    for name, value in pairs:
        if name not in {"call-id", "to-tag", "from-tag"}:
            continue
        if name in values or not value:
            raise ValueError("invalid dialog subscription filter")
        if value.startswith('"') and value.endswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        values[name] = value
    if values and (not values.get("call-id") or not values.get("to-tag")):
        raise ValueError("dialog filter requires call-id and to-tag")
    return tuple(values.get(key, "") for key in ("call-id", "to-tag", "from-tag"))


def snapshot_all(runtime):
    """Project all call legs once; the registry remains the sole state owner."""
    calls = runtime.sip
    if calls is None:
        return {}
    result = {}
    manager = calls.component("udp_listener")
    phases = {
        "new": "trying",
        "route_requested": "trying",
        "calling": "trying",
        "connecting": "proceeding",
        "ringing": "early",
        "remote_ringing": "early",
        "in_call": "confirmed",
        "answered": "confirmed",
        "held": "confirmed",
        "transferring": "confirmed",
    }
    for session in tuple(calls.sessions.values()):
        legs = {}
        for leg in session.legs.values():
            if leg.endpoint_id and leg.phase not in {LegPhase.CLOSING, LegPhase.CLOSED}:
                legs.setdefault(leg.endpoint_id, []).append(leg)
        retired = {
            resource.value.endpoint_id
            for resource in session.resources
            if resource.name.startswith("retired_destination:")
        }
        for endpoint_id in session.endpoint_claims.keys() | legs.keys():
            source = endpoint_id == session.metadata.get("source_endpoint_id")
            matching = legs.get(endpoint_id, ())
            endpoint = runtime.endpoints.get(endpoint_id)
            browser = endpoint is not None and endpoint.kind.is_softphone
            projected = []
            if not matching and endpoint_id in retired:
                continue
            peer_id = str(
                ((session.metadata.get("dest_endpoint_id")
                  or session.metadata.get("target_endpoint_id"))
                 if source
                 else session.metadata.get("source_endpoint_id"))
                or ""
            )
            peer = runtime.endpoints.get(peer_id) if peer_id else None
            peer_name = str(
                (session.metadata.get("connected_party") or session.callee)
                if source
                else session.caller
            )
            for leg in matching or (None,):
                wire_id = (
                    leg.sip_call_id
                    if leg is not None
                    else session.call_id
                    if source
                    else str(session.metadata.get("bridge_dest_call_id") or "")
                )
                client = calls.sip_client_for(wire_id)
                key = getattr(client, "dialog_ids", None)
                contract = getattr(client, "dialog", None)
                local_uri = remote_uri = local_target = remote_target = ""
                if contract is not None:
                    local_uri = str(getattr(contract, "local_uri", "") or "")
                    remote_uri = str(getattr(contract, "remote_uri", "") or "")
                    remote_target = str(
                        getattr(contract, "remote_target_uri", "") or remote_uri
                    )
                early = getattr(client, "early_dialogs", {})
                if (
                    key is not None
                    and not getattr(client, "dialog", None)
                    and len(early) == 1
                ):
                    from .core.sip_dialog import DialogKey

                    key = DialogKey(key.call_id, key.local_tag, next(iter(early)))
                if key is None and manager is not None:
                    key = manager.dialog_key(wire_id)
                    if key is not None:
                        contract = manager.invite_for_call(wire_id)
                        local_uri = manager.local_uri_for_call(wire_id)
                        remote_uri = manager.remote_uri_for_call(wire_id)
                        remote_target = manager.remote_target_for_call(wire_id)
                local_tag, remote_tag = (
                    str(getattr(key, "local_tag", "")),
                    str(getattr(key, "remote_tag", "")),
                )
                if not browser:
                    local_tag, remote_tag = remote_tag, local_tag
                    local_uri, remote_uri = remote_uri, local_uri
                    local_target, remote_target = remote_target, ""
                state = leg.state if leg is not None else session.state
                audio_direction = getattr(
                    contract,
                    "local_audio_direction" if browser else "remote_audio_direction",
                    "sendrecv",
                )
                held = state == "held" or (
                    contract is not None
                    and (
                        audio_direction in {"sendonly", "inactive"}
                        or getattr(contract, "remote_audio_connection_held", False)
                    )
                )
                video = getattr(contract, "video_format", None)
                video_direction = (
                    getattr(contract, "local_video_direction", "")
                    if browser
                    else getattr(video, "direction", "")
                )
                receives_video = (
                    video is not None
                    and video_direction in {"recvonly", "sendrecv"}
                    and not getattr(contract, "remote_video_connection_held", False)
                )
                identity = f"{session.call_id}:{session.generation}"
                observed_wire = str(getattr(key, "call_id", ""))
                if observed_wire or (not browser and wire_id):
                    identity += ":" + (observed_wire or wire_id)
                projected.append(
                    DialogState(
                        identity,
                        phases.get(state, "terminated"),
                        "initiator" if source else "recipient",
                        observed_wire,
                        local_tag,
                        remote_tag,
                        held=held,
                        session_id=session.call_id,
                        generation=session.generation,
                        leg_id=leg.leg_id if leg is not None else "",
                        peer_endpoint_id=peer_id,
                        peer_name=peer_name,
                        peer_user=str(peer.sip_uri_user if peer else ""),
                        local_uri=local_uri,
                        remote_uri=remote_uri,
                        local_target=local_target,
                        remote_target=remote_target,
                        rendering="no" if held and not receives_video else "",
                    )
                )
            # Internal media/projection legs are not additional SIP dialogs.
            wired = [item for item in projected if item.call_id]
            if wired:
                unique = {
                    (item.call_id, item.local_tag, item.remote_tag): item
                    for item in wired
                }
                projected = list(unique.values())
            elif projected:
                rank = {
                    "terminated": 0,
                    "trying": 1,
                    "proceeding": 2,
                    "early": 3,
                    "confirmed": 4,
                }
                projected = [max(projected, key=lambda item: rank[item.state])]
            result.setdefault(endpoint_id, []).extend(projected)
    lot = calls.component("parking")
    if lot is not None:
        for slot, parked in lot.calls.items():
            if parked.session.live:
                session = parked.session
                result["parking:" + slot] = [
                    DialogState(
                        f"{session.call_id}:{session.generation}",
                        "confirmed" if session.answer_committed else "early",
                        "recipient",
                        held=True,
                        session_id=session.call_id,
                        generation=session.generation,
                        peer_name=session.caller,
                    )
                ]
    return {endpoint: tuple(states) for endpoint, states in result.items()}


def filter_dialogs(states, match=("", "", "")):
    return tuple(
        item
        for item in states
        if all(
            not expected or actual == expected
            for actual, expected in zip(
                (item.call_id, item.local_tag, item.remote_tag), match, strict=True
            )
        )
    )


def snapshot(runtime, endpoint_id, match=("", "", "")):
    return filter_dialogs(snapshot_all(runtime).get(endpoint_id, ()), match)


def document(entity, version, states):
    root = ElementTree.Element(
        "dialog-info",
        {
            "xmlns": "urn:ietf:params:xml:ns:dialog-info",
            "version": str(version),
            "state": "full",
            "entity": entity,
        },
    )
    for item in states:
        attributes = {"id": sha256(item.identity.encode("utf-8")).hexdigest(), "direction": item.direction}
        for key, value in (
            ("call-id", item.call_id),
            ("local-tag", item.local_tag),
            ("remote-tag", item.remote_tag),
        ):
            if value:
                attributes[key] = value
        dialog = ElementTree.SubElement(root, "dialog", attributes)
        ElementTree.SubElement(dialog, "state").text = item.state
        local = ElementTree.SubElement(dialog, "local")
        ElementTree.SubElement(local, "identity").text = item.local_uri or entity
        target_uri = item.local_target or (
            entity if item.held and not item.call_id else ""
        )
        if target_uri:
            target = ElementTree.SubElement(local, "target", {"uri": target_uri})
            if item.rendering == "no" or (item.held and not item.call_id):
                ElementTree.SubElement(
                    target, "param", {"pname": "+sip.rendering", "pval": "no"}
                )
        remote_uri = item.remote_uri
        if not remote_uri and item.peer_user:
            from dataclasses import replace

            remote_uri = str(replace(sip.parse_sip_uri(entity), user=item.peer_user))
        if remote_uri or item.remote_target:
            remote = ElementTree.SubElement(dialog, "remote")
            if remote_uri:
                ElementTree.SubElement(
                    remote, "identity", {"display": item.peer_name}
                ).text = remote_uri
            if item.remote_target:
                ElementTree.SubElement(remote, "target", {"uri": item.remote_target})
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def activity_snapshot(runtime, *, dialogs=None):
    """Project occupied slots and active phone dialogs without caller details."""
    if runtime is None or runtime.sip is None:
        return {"parked": [], "phones": []}
    lot = runtime.sip.component("parking")
    parked = (
        sorted(slot for slot, call in lot.calls.items() if call.session.live)
        if lot is not None
        else []
    )
    phones = []
    dialogs = snapshot_all(runtime) if dialogs is None else dialogs
    for endpoint in runtime.endpoints.endpoints:
        states = [
            item
            for item in dialogs.get(endpoint.endpoint_id, ())
            if item.state != "terminated"
        ]
        if not states:
            continue
        ringing = any(
            item.state == "early" and item.direction == "recipient" for item in states
        )
        connected = any(item.state == "confirmed" for item in states)
        phones.append(
            {
                "endpoint_id": endpoint.endpoint_id,
                "device_id": endpoint.device_id,
                "name": endpoint.name,
                "state": "ringing" if ringing else "busy" if connected else "calling",
                "call_ids": sorted({item.session_id for item in states if item.session_id}),
            }
        )
    return {"parked": parked, "phones": phones}
