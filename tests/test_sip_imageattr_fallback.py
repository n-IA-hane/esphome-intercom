"""RFC 6236 retry uses the existing SIP offer owner and preserves legacy media."""

from dataclasses import replace
import unittest

from . import test_sip_protocol as protocol
from .voip_phase1_support import asyncio, sdp, sip


def bounded_jpeg():
    parsed = sdp.parse_imageattr("26 send [x=320,y=240] recv [x=320,y=240]")
    assert parsed is not None
    return replace(sdp.DEFAULT_VIDEO_FORMATS[3], image_attributes=parsed[1])


async def next_invite(sent, after=0):
    for _ in range(200):
        for raw, _address in sent[after:]:
            request = sip.parse_message(raw)
            if request.method == "INVITE":
                return request
        await asyncio.sleep(0)
    raise AssertionError("Expected in-dialog INVITE was not sent")


def legacy_answer(request, audio):
    return sdp.build_answer_directional(
        "127.0.0.2", "127.0.0.2", 42000, audio, audio,
        remote_sdp=sdp.remove_image_attributes(request.body),
        video_port=44000,
        video_format=sdp.remove_image_attributes(sdp.DEFAULT_VIDEO_FORMATS[3]),
        video_direction="sendrecv",
    )


def answer(client, request, audio, *, incompatible=False, status=200):
    body = legacy_answer(request, audio) if status == 200 else ""
    if incompatible:
        body += "a=imageattr:26 send [x=640,y=480] recv [x=640,y=480]\r\n"
    response = protocol.SipProtocolBugFixAsyncTest._response_to_request(
        request, status, "OK" if status == 200 else "Not Acceptable Here", body,
    )
    client.queue.put_nowait((response, ("127.0.0.2", 5060)))


class ImageAttributeFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client, self.current, self.sent, self.audio = (
            protocol.SipProtocolBugFixAsyncTest._confirmed_audio_client()
        )

    async def asyncTearDown(self):
        self.client.dialog = None
        self.client.transport = None
        await self.client.close()

    async def test_legacy_answer_keeps_media_without_a_second_offer(self):
        task = asyncio.create_task(self.client.async_prepare_video_reinvite(
            local_video_rtp_port=43000, video_formats=(bounded_jpeg(),),
        ))
        request = await next_invite(self.sent)
        self.assertIn(b"a=imageattr:", request.body)
        answer(self.client, request, self.audio)
        candidate = await asyncio.wait_for(task, 1)
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate.imageattr_fallback_needed)
        self.assertIsNone(candidate.video_format.image_attributes)
        self.assertEqual(candidate.send_format, self.current.send_format)
        self.assertIs(self.client.dialog, self.current)
        self.assertEqual(sum(sip.parse_message(raw).method == "INVITE" for raw, _ in self.sent), 1)

    async def exercise_incompatible_answer(self, *, unsolicited=False, decline=False):
        task = asyncio.create_task(self.client.async_prepare_video_reinvite(
            local_video_rtp_port=43000, video_formats=(bounded_jpeg(),),
        ))
        first = await next_invite(self.sent)
        offset = len(self.sent)
        answer(self.client, first, self.audio, incompatible=True)
        second = await next_invite(self.sent, offset)
        self.assertNotIn(b"a=imageattr:", second.body)
        self.assertEqual(
            first.body.decode().split("m=audio", 1)[1].split("m=video", 1)[0],
            second.body.decode().split("m=audio", 1)[1].split("m=video", 1)[0],
        )
        first_version = int(next(line for line in first.body.decode().splitlines() if line.startswith("o=")).split()[2])
        second_version = int(next(line for line in second.body.decode().splitlines() if line.startswith("o=")).split()[2])
        self.assertGreater(second_version, first_version)
        self.assertIs(self.client.dialog, self.current)
        answer(self.client, second, self.audio, incompatible=unsolicited, status=488 if decline else 200)
        candidate = await asyncio.wait_for(task, 1)
        self.assertEqual(sum(sip.parse_message(raw).method == "INVITE" for raw, _ in self.sent), 2)
        self.assertFalse(any(sip.parse_message(raw).method == "BYE" for raw, _ in self.sent))
        self.assertIs(self.client.dialog, self.current)
        if decline:
            self.assertIsNone(candidate)
            self.assertIsNone(self.client._prepared_reinvite)
        else:
            self.assertIsNotNone(candidate)
            self.assertFalse(candidate.imageattr_fallback_needed)
            self.assertIsNone(candidate.video_format.image_attributes)
            self.assertEqual(candidate.send_format, self.current.send_format)
            self.assertTrue(self.client.commit_prepared_reinvite(self.current, candidate))
            self.assertIs(self.client.dialog, candidate)

    async def test_incompatible_answer_gets_exactly_one_legacy_reoffer(self):
        await self.exercise_incompatible_answer()

    async def test_unsolicited_attribute_on_retry_cannot_start_an_offer_loop(self):
        await self.exercise_incompatible_answer(unsolicited=True)

    async def test_rejected_optional_retry_leaves_the_audio_call_alive(self):
        await self.exercise_incompatible_answer(decline=True)

    async def test_initial_fallback_uses_the_same_staged_offer_owner(self):
        jpeg = bounded_jpeg()
        self.client.video_formats = (jpeg,)
        self.client.video_format = jpeg
        self.client.local_video_rtp_port = 43000
        self.client.video_direction = "sendrecv"
        offer = sdp.build_offer_directional(
            "127.0.0.1", "127.0.0.1", 41000,
            [self.audio.audio_format], [self.audio.audio_format],
            video_port=43000, video_formats=(jpeg,),
        )
        self.current.local_sdp_body = offer
        self.current.video_format = sdp.remove_image_attributes(jpeg)
        self.current.local_video_format = sdp.remove_image_attributes(jpeg)
        self.current.local_video_rtp_port = 43000
        self.current.local_video_direction = "sendrecv"
        self.current.imageattr_fallback_needed = True
        task = asyncio.create_task(self.client._complete_imageattr_fallback())
        request = await next_invite(self.sent)
        self.assertNotIn(b"a=imageattr:", request.body)
        answer(self.client, request, self.audio)
        await asyncio.wait_for(task, 1)
        self.assertIsNot(self.client.dialog, self.current)
        self.assertEqual(self.client.dialog.send_format, self.current.send_format)
        self.assertIsNone(self.client._prepared_reinvite)
        self.assertFalse(self.client.dialog.imageattr_fallback_needed)
        self.assertEqual(sum(sip.parse_message(raw).method == "INVITE" for raw, _ in self.sent), 1)
