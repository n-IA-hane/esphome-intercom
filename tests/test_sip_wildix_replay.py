"""Wire-level regression replay for the Wildix video activation sequence."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format = _load("audio_format")
sdp = _load("sdp")
sip = _load("sip")
sip_listener = _load("sip_listener")
media_offer_answer = _load("media_offer_answer")
validate_direct_video_reoffer = media_offer_answer.validate_direct_video_reoffer
validate_bridged_video_reoffer = media_offer_answer.validate_bridged_video_reoffer


def _video(
    payload_type: int = 104,
    *,
    direction: str = "sendrecv",
    encoding: str = "VP8",
):
    return sdp.RtpVideoFormat(
        payload_type=payload_type,
        encoding=encoding,
        direction=direction,
    )


class VideoOfferPolicyTest(unittest.TestCase):
    def test_rejected_direct_reoffer_does_not_mutate_committed_contract(self) -> None:
        committed_send = _video(direction="recvonly")
        committed_recv = _video(direction="sendonly")
        proposed_send = _video(encoding="H264", direction="sendrecv")
        proposed_recv = _video(encoding="H264", direction="sendrecv")

        decision = validate_direct_video_reoffer(
            committed_send,
            committed_recv,
            proposed_send,
            proposed_recv,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "incompatible_video_contract")
        self.assertEqual(committed_send.encoding, "VP8")
        self.assertEqual(committed_send.direction, "recvonly")

    def test_bridge_checks_only_media_paths_active_in_both_directions(self) -> None:
        caller = _video(104, direction="sendonly")
        peer = _video(110, direction="recvonly")
        decision = validate_bridged_video_reoffer(
            caller,
            caller,
            _video(104, direction="recvonly"),
            peer_send=peer,
            peer_recv=_video(110, direction="sendonly"),
            peer_direction=peer,
        )
        self.assertTrue(decision.accepted)

    def test_direct_call_accepts_video_added_mid_dialog(self) -> None:
        decision = validate_direct_video_reoffer(
            None,
            None,
            _video(),
            _video(),
        )
        self.assertTrue(decision.accepted)

    def test_bridge_accepts_cross_codec_paths_owned_by_transcoders(self) -> None:
        vp8 = _video(104, encoding="VP8")
        jpeg = _video(26, encoding="JPEG")

        rejected = validate_bridged_video_reoffer(
            vp8,
            vp8,
            vp8,
            peer_send=jpeg,
            peer_recv=jpeg,
            peer_direction=jpeg,
        )
        accepted = validate_bridged_video_reoffer(
            vp8,
            vp8,
            vp8,
            peer_send=jpeg,
            peer_recv=jpeg,
            peer_direction=jpeg,
            caller_to_peer_transcoding=True,
            peer_to_caller_transcoding=True,
        )

        self.assertFalse(rejected.accepted)
        self.assertTrue(accepted.accepted)


class WildixVideoReinviteReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_recvonly_sendrecv_reinvite_storm_and_bye(self) -> None:
        """A camera activation must not starve or invalidate the later BYE."""

        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        committed_directions: list[str] = []
        call_id = "wildix-video-activation"
        local_ip = "192.0.2.20"
        remote_ip = "192.0.2.10"
        addr = (remote_ip, 5060)
        formats = list(audio_format.HA_TRUNK_AUDIO_FORMATS)
        vp8 = sdp.RtpVideoFormat(
            payload_type=104,
            encoding="VP8",
            fmtp="max-fr=20;max-fs=3600",
        )

        def offer(direction: str) -> bytes:
            return sdp.build_offer_directional(
                remote_ip,
                remote_ip,
                53860,
                formats,
                formats,
                include_common_codecs=True,
                video_port=42994,
                video_formats=(vp8,),
                video_direction=direction,
            ).encode()

        async def on_terminated(ended_call_id: str, reason: str) -> None:
            terminated.append((ended_call_id, reason))

        async def on_media_update(previous, updated, _method):
            decision = validate_direct_video_reoffer(
                previous.video_format,
                previous.recv_video_format,
                updated.video_format,
                updated.recv_video_format,
            )
            if not decision.accepted:
                return sip_listener.SipInviteResult(488, "Not Acceptable Here")
            answer = sdp.build_answer_directional(
                local_ip,
                local_ip,
                40018,
                updated.send_format,
                updated.recv_format,
                remote_sdp=updated.remote_sdp,
                video_port=40024,
                video_format=updated.answer_video_format,
                video_direction="recvonly",
            )

            async def commit() -> None:
                committed_directions.append(updated.video_format.direction)

            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer,
                commit=commit,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip=local_ip,
            local_rtp_port=40018,
            supported_formats=formats,
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
            enable_video=True,
            trusted_trunk=True,
        )

        def request(method: str, cseq: int, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP {remote_ip};branch=z9hG4bK-{method}-{cseq}"),
                ("From", "<sip:418@wildix.example>;tag=remote"),
                ("To", f"<sip:427@{local_ip}>;tag=local"),
                ("Contact", f"<sip:418@{remote_ip}:5060>"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(method, f"sip:427@{local_ip}", headers, body)

        initial_message = sip.parse_message(request("INVITE", 1, offer("recvonly")))
        initial_invite = endpoint._parse_invite(initial_message, addr)
        self.assertIsNotNone(initial_invite)
        endpoint.active_dialogs[call_id] = sip_listener._ActiveDialog(
            initial_message,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
            invite=initial_invite,
            local_sdp_session_id=5151,
        )

        # Wildix first refreshes the existing recvonly session, then enables
        # its camera with sendrecv. Repeated higher-CSeq offers model the
        # observed B2BUA retry burst rather than UDP retransmissions.
        directions = ["recvonly", "sendrecv", *(["sendrecv"] * 32)]
        for cseq, direction in enumerate(directions, start=2):
            await endpoint._handle_datagram(
                request("INVITE", cseq, offer(direction)),
                addr,
            )
            self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
            await endpoint._handle_datagram(request("ACK", cseq), addr)

        bye_cseq = len(directions) + 2
        await endpoint._handle_datagram(request("BYE", bye_cseq), addr)

        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertNotIn(call_id, endpoint.active_dialogs)
        self.assertEqual(terminated, [(call_id, "remote_hangup")])
        self.assertEqual(committed_directions[0], "recvonly")
        self.assertTrue(all(item == "sendrecv" for item in committed_directions[1:]))


if __name__ == "__main__":
    unittest.main()
