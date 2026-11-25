"""Regression for an in-dialog Wildix offer while Assist owns RTP."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from tests.test_media_session_updates import MEDIA_SESSION_UPDATES
from .voip_phase1_support import _load_intercom_module


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "voip_stack" / "media_renegotiation.py"
PACKAGE = "voip_assist_reinvite_test"
SDP = _load_intercom_module("core.sdp")
SIP = _load_intercom_module("core.sip")
WIRE = _load_intercom_module("sip_server_state")
CLIENT = _load_intercom_module("sip_client")
AUDIO_SEND = SDP.RtpPcmFormat(111, "L16", 16000, 1, 20)
AUDIO_RECEIVE = AUDIO_SEND
OPUS_SEND = SDP.RtpPcmFormat(112, "OPUS", 48000, 2, 20)
OPUS_RECEIVE = OPUS_SEND
DEFAULT_SEND = SDP.RtpPcmFormat(113, "L16", 48000, 1, 10)
DEFAULT_RECEIVE = SDP.RtpPcmFormat(114, "L16", 16000, 1, 20)
ESTABLISHED_SEND = SDP.RtpPcmFormat(115, "L16", 48000, 1, 10)
ESTABLISHED_RECEIVE = SDP.RtpPcmFormat(116, "L16", 16000, 1, 20)


def _video(direction="sendrecv", encoding="JPEG"):
    return SDP.RtpVideoFormat(payload_type={"JPEG": 26, "VP8": 104, "H264": 105}[encoding],
                              encoding=encoding, direction=direction)


JPEG = _video()
H264_SEND = _video("sendonly", "H264")
H264_RECEIVE = _video("recvonly", "H264")


class _InvitePeer(WIRE.SipInvite):
    """Expose the fake media edge's view without copying the wire record schema."""

    @property
    def outbound_rtp_format(self):
        return self.send_format

    @property
    def outbound_rtp_formats(self):
        return self.send_formats

    @property
    def inbound_rtp_format(self):
        return self.recv_format

    @property
    def inbound_rtp_formats(self):
        return self.recv_formats


class _DialogPeer(CLIENT.SipDialog):
    """The fake bidirectional audio edge retains the real SIP dialog model."""

    can_send = True
    can_receive = True


def _invite(**values):
    data = dict(source_host="198.51.100.20", source_port=5060,
        request_uri=SIP.SipUri("HA", "192.0.2.10"), caller_uri=SIP.SipUri("Caller", "198.51.100.20"),
        target="HA", caller="Caller", call_id="call", cseq="1 INVITE",
        send_format=AUDIO_SEND, recv_format=AUDIO_RECEIVE,
        remote_rtp_host="198.51.100.20", remote_rtp_port=42000)
    data.update(values)
    if "remote_sdp" not in data:
        formats = (AUDIO_SEND, OPUS_SEND, DEFAULT_SEND, DEFAULT_RECEIVE,
                   ESTABLISHED_SEND, ESTABLISHED_RECEIVE)
        body = ("v=0\r\no=- 1 1 IN IP4 198.51.100.20\r\ns=Independent peer\r\n"
                "c=IN IP4 198.51.100.20\r\nt=0 0\r\nm=audio 42000 RTP/AVP "
                + " ".join(str(fmt.payload_type) for fmt in formats) + "\r\n")
        body += "".join(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}\r\n"
                        for fmt in formats)
        body += "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n"
        if (video := data.get("video_format")) is not None:
            body += (f"m=video 42002 RTP/AVP {video.payload_type}\r\n"
                     f"a=rtpmap:{video.payload_type} {video.encoding}/90000\r\na={video.direction}\r\n")
            data.setdefault("remote_video_rtp_host", "198.51.100.20")
            data.setdefault("remote_video_rtp_port", 42002)
        data["remote_sdp"] = body.encode()
    return _InvitePeer(**data)


def _dialog(video=None, **values):
    data = dict(target="Peer", remote_host="192.0.2.20", remote_sip_port=5060,
        remote_rtp_host="192.0.2.20", remote_rtp_port=42000, local_rtp_port=41002,
        call_id="destination", local_uri="sip:HA@192.0.2.10", remote_uri="sip:Peer@192.0.2.20",
        send_format=AUDIO_SEND, recv_format=AUDIO_RECEIVE, video_format=video,
        local_video_direction=video.direction if video else "inactive")
    data.update(values)
    return _DialogPeer(**data)


def _module(name: str, **values):
    if "." in name:
        parent = name.rsplit(".", 1)[0]
        parent_name = f"{PACKAGE}.{parent}"
        if parent_name not in sys.modules:
            package = types.ModuleType(parent_name)
            package.__path__ = []
            sys.modules[parent_name] = package
    module = types.ModuleType(f"{PACKAGE}.{name}")
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[module.__name__] = module
    return module


def _load_module(registry, answer_calls: list[dict], *, generic=False):
    registry.resource_for = lambda call_id, kind: (
        getattr(registry, f"{kind}s", {}).get(call_id)
        if kind == "relay"
        else getattr(registry, kind, {}).get(call_id)
    )
    registry.sip_client_for = lambda call_id: registry.sip_clients.get(call_id)
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE.parent)]
    sys.modules[PACKAGE] = package

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.core", core)

    class LocalCallMedia:
        def __init__(self) -> None:
            self.local_rtp_port = 41000
            self.committed = False

        def prepare_media_update(self, _updated):
            def commit() -> None:
                self.committed = True

            return commit

    class AssistMediaSession(LocalCallMedia):
        pass

    _module("assist_runtime", AssistMediaSession=AssistMediaSession)
    _module("local_call_media", LocalCallMedia=LocalCallMedia)
    _module("const", DOMAIN="voip_stack", HA_SOFTPHONE_DEVICE_ID="device")
    _module("endpoint_lifecycle", call_registry=lambda _hass: registry)

    decision = types.SimpleNamespace(accepted=True, reason="")
    _module(
        "media_offer_answer",
        validate_bridged_video_reoffer=lambda *_args, **_kwargs: decision,
        validate_direct_video_reoffer=lambda *_args, **_kwargs: decision,
    )
    _module(
        "media_ports",
        release_video_media_reservation=lambda _item: None,
        reserve_sip_video_media=lambda _hass: (),
    )
    _module(
        "media_session_updates",
        commit_audio_session_update=lambda *_args, **_kwargs: None,
        commit_video_session_update=lambda *_args, **_kwargs: None,
        committed_video_format_state=MEDIA_SESSION_UPDATES.committed_video_format_state,
    )
    _module("phone_endpoint", DEFAULT_ENDPOINT_ID="default")
    _module(
        "runtime_data",
        endpoint_directory=lambda _hass: types.SimpleNamespace(
            get=lambda _endpoint_id: None,
        ),
        require_runtime_data=lambda hass: hass.runtime,
    )

    def build_answer_directional(*args, **kwargs):
        answer_calls.append(dict(kwargs))
        return SDP.build_answer_directional(*args, **kwargs)

    from .voip_phase1_support import _load_intercom_module
    _module(
        "core.sdp",
        RtpVideoFormat=_load_intercom_module("core.sdp").RtpVideoFormat,
        build_answer_directional=build_answer_directional,
        constrained_media_direction=lambda *_args, **_kwargs: "sendrecv",
        constrained_video_direction=SDP.constrained_video_direction,
        first_offered_dtmf_format=lambda _sdp: None,
        offered_video_formats=SDP.offered_video_formats,
        video_fmtp_parameters=SDP.video_fmtp_parameters,
        remote_can_receive=SDP.remote_can_receive,
        remote_can_send=SDP.remote_can_send,
    )
    def invite_rtp_peer(invite, *, established=None):
        assert isinstance(invite, WIRE.SipInvite)
        return invite

    _module(
        "sip_bridge",
        dialog_video_rtp_peer=lambda dialog: dialog,
        invite_rtp_peer=invite_rtp_peer,
        invite_video_rtp_peer=lambda invite: invite,
    )

    identity = _load_intercom_module("core.sip_dialog").PreparedDialogIdentity
    _module("core.sip_dialog", PreparedDialogIdentity=identity)
    _module("sip_listener", SipInvite=WIRE.SipInvite, SipInviteResult=WIRE.SipInviteResult)
    # Keep cleanup behavior real without importing a second RTP stack beneath
    # this deliberately partial package's fake core.sdp module.
    _module(
        "sip_rtp_bridge",
        discard_peer_reconfiguration=_load_intercom_module("sip_rtp_bridge").discard_peer_reconfiguration,
    )
    _module("sip_video_relay")
    _module("call_events", fire_call_event=lambda *_args, **_kwargs: None)
    _module("browser_phone_data", _ha_softphone_store=lambda *_args, **_kwargs: {})

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.media_renegotiation", SOURCE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, LocalCallMedia if generic else AssistMediaSession


@pytest.mark.parametrize("generic", [False, True])
def test_assist_reinvite_returns_audio_answer_and_declines_video(generic) -> None:
    answer_calls: list[dict] = []
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"call": types.SimpleNamespace(generation=7)},
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "call" and generation == 7
        ),
    )
    module, AssistMediaSession = _load_module(registry, answer_calls, generic=generic)
    owner = AssistMediaSession()
    registry.relays["call"] = owner
    video = _video("recvonly")
    invite = _invite(
        call_id="call",
        send_format=OPUS_SEND,
        recv_format=OPUS_RECEIVE,
        recv_formats=(),
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=42000,
        remote_audio_direction="sendrecv",
        local_audio_direction="sendrecv",
        remote_audio_connection_held=False,
        video_format=video,
        local_video_format=video,
        video_answer_format=video,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", invite, invite, "INVITE"
        )
    )

    assert result.status == 200
    assert isinstance(result, WIRE.SipInviteResult)
    assert isinstance(result.media_contract, WIRE.SipInvite)
    assert "m=video 0" in result.answer_sdp
    assert "telephone-event/" not in result.answer_sdp
    assert answer_calls == [
        {
            "recv_formats": (),
            "send_formats": (),
            "dtmf_direction": None,
            "remote_sdp": invite.remote_sdp,
            "audio_direction": "sendrecv",
            "video_port": 0,
            "video_format": video,
            "video_direction": "inactive",
        }
    ]
    assert owner.committed is False
    asyncio.run(result.commit())
    assert owner.committed is True


@pytest.mark.parametrize("replace_owner", [False, True])
def test_bridged_audio_refresh_preserves_owned_video_transcoding(replace_owner) -> None:
    """An unchanged transcoded video m-line must survive an audio refresh."""

    answer_calls: list[dict] = []
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sip_clients={},
        sessions={"call": types.SimpleNamespace(generation=7)},
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda *_args: True,
    )
    module, _assist = _load_module(registry, answer_calls)
    validation: dict[str, object] = {}

    def validate(*_args, **kwargs):
        validation.update(kwargs)
        return types.SimpleNamespace(accepted=True, reason="")

    module.validate_bridged_video_reoffer = validate
    module.remote_can_send = lambda *_args: True
    module.remote_can_receive = lambda *_args, **_kwargs: True
    module.constrained_video_direction = lambda *_args, **_kwargs: "recvonly"
    materialized_invites: list[object] = []
    answered_formats: list[tuple[object, object]] = []

    def materialized_peer(invite):
        materialized_invites.append(invite)
        return updated_audio_peer

    def answer(*args, **_kwargs):
        answered_formats.append((args[3], args[4]))
        return SDP.build_answer_directional(*args, **_kwargs)

    module.invite_rtp_peer = materialized_peer
    module.build_answer_directional = answer
    module.invite_video_rtp_peer = lambda invite: updated_video_peer

    class PeerOwner:
        def __init__(self, left, right) -> None:
            self.left = left
            self.right = right

        def prepare_peer_reconfiguration(self, side, peer):
            return lambda: setattr(self, side, peer)

    video = _video("recvonly")
    video_relay = PeerOwner(
        object(),
        types.SimpleNamespace(
            send_format=H264_SEND,
            recv_format=H264_RECEIVE,
            video_format=_video("sendonly"),
            connection_held=False,
        ),
    )
    video_relay.left_port = 43000
    video_relay.transcodes_from = lambda side: side in {"left", "right"}
    relay = PeerOwner(
        object(),
        types.SimpleNamespace(can_send=True, can_receive=True),
    )
    relay.left_port = 41000
    relay.video_relay = video_relay
    registry.relays["call"] = relay
    previous = _invite(
        call_id="call",
        video_format=video,
        remote_video_connection_held=False,
    )
    updated_audio_peer = types.SimpleNamespace(
        outbound_rtp_format=ESTABLISHED_SEND, inbound_rtp_format=ESTABLISHED_RECEIVE,
        inbound_rtp_formats=(), outbound_rtp_formats=())
    updated_video_peer = object()
    updated = _invite(
        call_id="call",
        send_format=ESTABLISHED_SEND,
        recv_format=ESTABLISHED_RECEIVE,
        recv_formats=(),
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
        video_format=video,
        local_video_format=video,
        video_answer_format=video,
        remote_video_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert validation["caller_to_peer_transcoding"] is True
    assert validation["peer_to_caller_transcoding"] is True
    old_audio, old_video = relay.left, video_relay.left
    # Negotiation supplied the retained audio contract before this consumer.
    # The update materializes it once while preserving the owned video edge.
    assert materialized_invites == [updated]
    assert answered_formats == [(ESTABLISHED_SEND, ESTABLISHED_RECEIVE)]
    if replace_owner:
        registry.relays["call"] = object()
        with pytest.raises(RuntimeError, match="owner changed"):
            asyncio.run(result.commit())
        assert relay.left is old_audio and video_relay.left is old_video
    else:
        asyncio.run(result.commit())
        assert relay.left is updated_audio_peer and video_relay.left is updated_video_peer


def test_bridge_video_addition_cancellation_releases_staged_media() -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=9)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda *_args: True,
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )

    class Reservation:
        ports = (43000, 43002)
        detached = False

        def detach(self) -> None:
            self.detached = True

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.start_entered = asyncio.Event()
            self.stopped = False

        async def start(self) -> None:
            self.start_entered.set()
            await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stopped = True

    reservation = Reservation()
    video_relay = VideoRelay()
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: (
        reservation,
        (None, None, None, None),
    )

    candidate = _dialog(JPEG)

    class Client:
        def __init__(self) -> None:
            self.dialog = _dialog()
            self.aborted = False

        def prepared_reinvite_identity(self, candidate):
            from custom_components.voip_stack.core.sip_dialog import PreparedDialogIdentity
            return PreparedDialogIdentity(self, self.dialog, candidate)

        async def async_prepare_video_reinvite(self, **_kwargs):
            return candidate

        async def restore_prepared_reinvite(self, previous, staged):
            assert previous is self.dialog
            assert staged is candidate
            self.aborted = True
            return None

        def abort_prepared_reinvite(self, previous, staged) -> None:
            self.aborted = True

    client = Client()
    registry.sip_clients["destination"] = client
    registry.relays["source"] = types.SimpleNamespace()
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: video_relay
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: (
        types.SimpleNamespace(
            video_format=JPEG,
            direction="sendrecv",
        )
    )
    sip_bridge.dialog_rtp_peer = lambda item: item
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: (JPEG,)

    video = _video("sendrecv")
    previous = _invite(call_id="source", video_format=None)
    updated = _invite(call_id="source", video_format=video)

    async def run() -> None:
        task = asyncio.create_task(
            module._prepare_bridge_video_contract_change(
                types.SimpleNamespace(),
                "192.0.2.10",
                previous,
                updated,
                registry.relays["source"],
            )
        )
        await asyncio.wait_for(video_relay.start_entered.wait(), 1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled media preparation returned normally")

    asyncio.run(run())

    assert reservation.detached is True
    assert client.aborted is True
    assert video_relay.stopped is True


@pytest.mark.parametrize("superseded", [False, True])
def test_bridge_activates_video_when_initial_offer_was_recvonly(superseded) -> None:
    """A dormant source m-line must not masquerade as an active bridge leg."""

    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=10)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 10
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )

    class Reservation:
        ports = (43000, 43002)

        def detach(self) -> None:
            return None

    old_audio_left = types.SimpleNamespace(name="old-audio-left")
    old_audio_right = types.SimpleNamespace(
        name="old-audio-right", can_send=True, can_receive=True
    )
    new_audio_left = types.SimpleNamespace(
        name="new-audio-left",
        outbound_rtp_format=AUDIO_SEND,
        outbound_rtp_formats=(),
        inbound_rtp_format=AUDIO_RECEIVE,
        inbound_rtp_formats=(),
    )
    new_audio_right = types.SimpleNamespace(
        name="new-audio-right", can_send=True, can_receive=True
    )
    new_video_left = types.SimpleNamespace(name="new-video-left")

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    staged_video_relay = VideoRelay()
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: (
        Reservation(),
        (None, None, None, None),
    )

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = old_audio_left
            self.right = old_audio_right
            self.video_relay = None

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

        def attach_video_relay(self, video_relay) -> None:
            assert self.video_relay is None
            self.video_relay = video_relay

    relay = Relay()
    registry.relays["source"] = relay
    destination_dialog = _dialog()
    candidate = _dialog(JPEG)

    class Client:
        def __init__(self) -> None:
            self.dialog = destination_dialog
            self.prepared = None
            self.committed = False

        def prepared_reinvite_identity(self, candidate):
            from custom_components.voip_stack.core.sip_dialog import PreparedDialogIdentity
            return PreparedDialogIdentity(self, self.dialog, candidate)

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is destination_dialog
            assert staged is candidate
            self.committed = True
            self.dialog = staged
            return True

        async def restore_prepared_reinvite(self, *_args):
            assert superseded and self.dialog is not candidate
            return None

        def abort_prepared_reinvite(self, *_args) -> None:
            raise AssertionError("committed video activation was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: (
        staged_video_relay
    )
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: (
        types.SimpleNamespace(
            video_format=JPEG,
            direction="sendrecv",
        )
    )
    sip_bridge.dialog_rtp_peer = lambda _dialog: new_audio_right
    sip_bridge.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: (JPEG,)
    module.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    module.invite_video_rtp_peer = lambda _invite: new_video_left

    previous_video = _video("recvonly")
    updated_video = _video("sendrecv")
    previous = _invite(
        call_id="source",
        video_format=previous_video,
        remote_video_connection_held=False,
    )
    updated = _invite(
        call_id="source",
        video_format=updated_video,
        local_video_format=updated_video,
        video_answer_format=updated_video,
        remote_video_connection_held=False,
        send_format=AUDIO_SEND,
        recv_format=AUDIO_RECEIVE,
        recv_formats=(),
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert client.prepared == {
        "local_video_rtp_port": 43002,
        "video_formats": (JPEG,),
        "video_direction": "sendrecv",
    }
    assert staged_video_relay.started is True
    assert relay.video_relay is None
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.video_relay is staged_video_relay
    assert answer_calls[-1]["video_port"] == 43000
    assert answer_calls[-1]["video_direction"] == "sendrecv"

    if superseded:
        client.dialog = _dialog(JPEG, remote_rtp_port=42002)
        asyncio.run(result.rollback())
        assert relay.video_relay is staged_video_relay
        assert not staged_video_relay.stopped


def test_bridge_video_removal_updates_both_dialogs_and_stops_media() -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=11)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 11
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: None

    class VideoRelay:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    video_relay = VideoRelay()

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = types.SimpleNamespace(name="old-left")
            self.right = types.SimpleNamespace(name="old-right")
            self.video_relay = video_relay

        def prepare_peer_reconfiguration(self, side, peer):
            previous = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous
                setattr(self, side, peer)

            return commit

    relay = Relay()
    registry.relays["source"] = relay
    candidate = _dialog()

    class Client:
        def __init__(self) -> None:
            self.dialog = _dialog(JPEG)
            self.video_formats = (JPEG,)
            self.prepared: dict = {}
            self.committed = False

        def prepared_reinvite_identity(self, candidate):
            from custom_components.voip_stack.core.sip_dialog import PreparedDialogIdentity
            return PreparedDialogIdentity(self, self.dialog, candidate)

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is self.dialog
            assert staged is candidate
            self.committed = True
            self.dialog = staged
            return True

        async def restore_prepared_reinvite(self, previous, staged):
            assert staged is candidate and not self.committed
            assert self.dialog is previous
            return previous

        def abort_prepared_reinvite(self, _previous, _staged) -> None:
            raise AssertionError("committed removal was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.dialog_rtp_peer = lambda item: item
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ()

    previous = _invite(
        call_id="source",
        video_format=_video("sendrecv"),
    )
    updated = _invite(
        call_id="source",
        video_format=None,
        video_answer_format=None,
        send_format=AUDIO_SEND,
        recv_format=AUDIO_RECEIVE,
        recv_formats=(),
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module._prepare_bridge_video_contract_change(
            types.SimpleNamespace(),
            "192.0.2.10",
            previous,
            updated,
            relay,
        )
    )

    assert result.status == 200
    assert client.prepared == {
        "local_video_rtp_port": 0,
        "video_formats": (JPEG,),
        "video_direction": "inactive",
    }
    assert relay.video_relay is video_relay
    assert video_relay.stopped is False
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.video_relay is None
    assert video_relay.stopped is False
    assert result.dialog_identity.current(committed=True)
    asyncio.run(result.finalize())
    assert video_relay.stopped is True
    assert relay.left is updated
    assert relay.right is candidate


@pytest.mark.parametrize("local_parameters", ["", "Z0IAH5WoFAFuQA==,aM48gA=="], ids=["", "local-encoder"])
def test_bridge_video_inactive_updates_both_video_legs_atomically(local_parameters) -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=13)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 13
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: None

    old_audio_left = types.SimpleNamespace(name="old-audio-left")
    old_audio_right = types.SimpleNamespace(
        name="old-audio-right", can_send=True, can_receive=True
    )
    new_audio_left = types.SimpleNamespace(
        name="new-audio-left",
        outbound_rtp_format=AUDIO_SEND,
        outbound_rtp_formats=(),
        inbound_rtp_format=AUDIO_RECEIVE,
        inbound_rtp_formats=(),
    )
    new_audio_right = types.SimpleNamespace(
        name="new-audio-right", can_send=True, can_receive=True
    )
    old_video_left = types.SimpleNamespace(name="old-video-left")
    old_video_right = types.SimpleNamespace(name="old-video-right")
    new_video_left = types.SimpleNamespace(name="new-video-left")
    new_video_right = types.SimpleNamespace(
        name="new-video-right",
        send_format=JPEG,
        recv_format=JPEG,
        video_format=_video("inactive"),
        connection_held=False,
    )

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.left = old_video_left
            self.right = old_video_right

        def transcodes_from(self, _side: str) -> bool:
            return True

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

    video_relay = VideoRelay()

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = old_audio_left
            self.right = old_audio_right
            self.video_relay = video_relay

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

    relay = Relay()
    registry.relays["source"] = relay
    remote_format = SDP.RtpVideoFormat(payload_type=105, encoding="H264", profile_level_id="42001f",
        sprop_parameter_sets="Z0IAH5WoFAFuQA==,aM4xUg==", fmtp="max-fs=3600")
    local_sdp = ("v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\ns=Local encoder\r\n"
        "c=IN IP4 192.0.2.10\r\nt=0 0\r\nm=video 43002 RTP/AVP 105\r\n"
        "a=rtpmap:105 H264/90000\r\na=sendrecv\r\n"
        "a=fmtp:105 profile-level-id=42001f;packetization-mode=1;level-asymmetry-allowed=1;max-fs=3600"
        + (f";sprop-parameter-sets={local_parameters}" if local_parameters else "") + "\r\n")
    local_format = SDP.offered_video_formats(local_sdp)[0]
    destination_dialog = _dialog(remote_format, local_video_format=remote_format,
        local_sdp_body=local_sdp)
    candidate = _dialog(_video("inactive"))

    class Client:
        def __init__(self) -> None:
            self.dialog = destination_dialog
            self.video_formats = (JPEG,)
            self.prepared = None
            self.committed = False

        def prepared_reinvite_identity(self, candidate):
            from custom_components.voip_stack.core.sip_dialog import PreparedDialogIdentity
            return PreparedDialogIdentity(self, self.dialog, candidate)

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is destination_dialog
            assert staged is candidate
            self.committed = True
            self.dialog = staged
            return True

        async def restore_prepared_reinvite(self, previous, staged):
            assert previous is destination_dialog and staged is candidate and not self.committed
            assert self.dialog is previous
            return previous

        def abort_prepared_reinvite(self, *_args) -> None:
            raise AssertionError("committed direction update was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.dialog_rtp_peer = lambda _dialog: new_audio_right
    sip_bridge.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    sip_bridge.dialog_video_rtp_peer = lambda _dialog: new_video_right
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ()
    module.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    module.invite_video_rtp_peer = lambda _invite: new_video_left
    module.remote_can_send = lambda fmt: fmt.direction in {"sendonly", "sendrecv"}
    module.remote_can_receive = lambda fmt, **_kwargs: (
        fmt.direction
        in {
            "recvonly",
            "sendrecv",
        }
    )
    module.constrained_video_direction = lambda *_args, **_kwargs: "inactive"

    previous_video = _video("sendrecv")
    updated_video = _video("inactive")
    previous = _invite(
        call_id="source",
        video_format=previous_video,
        remote_video_connection_held=False,
    )
    updated = _invite(
        call_id="source",
        video_format=updated_video,
        local_video_format=updated_video,
        video_answer_format=updated_video,
        remote_video_connection_held=False,
        send_format=AUDIO_SEND,
        recv_format=AUDIO_RECEIVE,
        recv_formats=(),
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert client.prepared["local_video_rtp_port"] == 43002
    assert client.prepared["video_direction"] == "inactive"
    advertised, = client.prepared["video_formats"]
    assert (advertised.payload_type, advertised.encoding, advertised.clock_rate,
        advertised.profile_level_id, advertised.packetization_mode, advertised.level_asymmetry_allowed) == (
            105, "H264", 90000, "42001f", 1, True)
    assert advertised.sprop_parameter_sets == local_parameters
    assert SDP.video_fmtp_parameters(advertised)["max-fs"] == "3600"
    assert SDP.serialized_video_fmtp(advertised) == SDP.serialized_video_fmtp(local_format)
    assert client.committed is False
    assert video_relay.left is old_video_left
    assert video_relay.right is old_video_right
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.left is new_audio_left
    assert relay.right is new_audio_right
    assert video_relay.left is new_video_left
    assert video_relay.right is new_video_right
    assert answer_calls[-1]["video_port"] == 43000
    assert answer_calls[-1]["video_direction"] == "inactive"
