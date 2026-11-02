"""Independent lifecycle witnesses for an accepted optional image-size update."""

from dataclasses import replace
import unittest

from . import test_sip_protocol as protocol
from .voip_phase1_support import asyncio, sdp, sip, sip_client


class ImageAttributeCompensationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client, self.previous, self.sent, self.audio = (
            protocol.SipProtocolBugFixAsyncTest._confirmed_audio_client()
        )
        parsed = sdp.parse_imageattr("26 send [x=320,y=240] recv [x=320,y=240]")
        self.assertIsNotNone(parsed)
        jpeg = replace(sdp.DEFAULT_VIDEO_FORMATS[3], image_attributes=parsed[1])
        legacy = sdp.remove_image_attributes(jpeg)
        self.client.video_formats = (jpeg,)
        self.client.video_format = jpeg
        self.client.local_video_rtp_port = 43000
        self.client.video_direction = "sendrecv"
        self.previous.local_sdp_body = sdp.build_offer_directional(
            "127.0.0.1", "127.0.0.1", 41000,
            [self.audio.audio_format], [self.audio.audio_format],
            video_port=43000, video_formats=(jpeg,),
        )
        self.previous.video_format = legacy
        self.previous.local_video_format = legacy
        self.previous.local_video_rtp_port = 43000
        self.previous.local_video_direction = "sendrecv"
        self.previous.remote_video_rtp_host = "127.0.0.2"
        self.previous.remote_video_rtp_port = 44002
        self.previous.remote_video_rtcp_host = "127.0.0.2"
        self.previous.remote_video_rtcp_port = 44003
        self.previous.imageattr_fallback_needed = True
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.client.dialog = None
        self.client.transport = None
        await self.client.close()

    async def next_invite(self, offset=0):
        for _ in range(200):
            for raw, _address in self.sent[offset:]:
                request = sip.parse_message(raw)
                if request.method == "INVITE":
                    return request
            await asyncio.sleep(0)
        self.fail("Accepted remote update was not compensated with the existing offer owner")

    def answer(self, request, *, video_port):
        body = sdp.build_answer_directional(
            "127.0.0.2", "127.0.0.2", 42000, self.audio, self.audio,
            remote_sdp=sdp.remove_image_attributes(request.body),
            video_port=video_port,
            video_format=sdp.remove_image_attributes(sdp.DEFAULT_VIDEO_FORMATS[3]),
            video_direction="sendrecv",
        )
        response = protocol.SipProtocolBugFixAsyncTest._response_to_request(
            request, 200, "OK", body,
        )
        self.client.queue.put_nowait((response, ("127.0.0.2", 5060)))

    async def begin(self):
        task = asyncio.create_task(self.client._complete_imageattr_fallback())
        self.tasks.append(task)
        request = await self.next_invite()
        offset = len(self.sent)
        self.answer(request, video_port=44000)
        return task, offset

    def assert_no_bye(self):
        self.assertFalse(any(sip.parse_message(raw).method == "BYE" for raw, _ in self.sent))

    async def finish_restoration(self, task, offset, *, cancelled=False):
        restored = await self.next_invite(offset)
        self.answer(restored, video_port=44002)
        if cancelled:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)
        self.assertIsNotNone(self.client.dialog)
        self.assertEqual(self.client.dialog.remote_video_rtp_port, 44002)
        self.assertEqual(self.client.dialog.send_format, self.previous.send_format)
        self.assertIsNone(self.client._prepared_reinvite)
        self.assert_no_bye()

    async def test_declined_media_preparation_compensates_accepted_remote_update(self):
        async def prepare(_previous, _candidate, _method):
            return None

        self.client.on_media_update = prepare
        task, offset = await self.begin()
        await self.finish_restoration(task, offset)

    async def test_commit_failure_rolls_back_once_then_compensates(self):
        operations = []

        async def commit():
            operations.append("commit")
            raise RuntimeError("injected media failure")

        async def rollback():
            operations.append("rollback")

        async def prepare(_previous, _candidate, _method):
            return sip_client.PreparedDialogMediaUpdate(commit=commit, rollback=rollback)

        self.client.on_media_update = prepare
        task, offset = await self.begin()
        await self.finish_restoration(task, offset)
        self.assertEqual(operations, ["commit", "rollback"])

    async def test_cancelled_preparation_compensates_before_propagating_cancel(self):
        entered = asyncio.Event()

        async def prepare(_previous, _candidate, _method):
            entered.set()
            await asyncio.Event().wait()

        self.client.on_media_update = prepare
        task, offset = await self.begin()
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await self.finish_restoration(task, offset, cancelled=True)

    async def exercise_remote_bye(self, phase):
        entered, release = asyncio.Event(), asyncio.Event()
        operations = []
        notifications = []

        async def commit():
            operations.append("commit")
            if phase == "commit":
                entered.set()
                await release.wait()

        async def rollback():
            operations.append("rollback")

        async def prepare(_previous, _candidate, _method):
            if phase == "prepare":
                entered.set()
                await release.wait()
            return sip_client.PreparedDialogMediaUpdate(
                commit=commit, rollback=rollback, on_committed=notifications.append,
            )

        self.client.on_media_update = prepare
        task, offset = await self.begin()
        await asyncio.wait_for(entered.wait(), 1)
        watcher = asyncio.create_task(self.client.wait_for_dialog_termination(timeout=1))
        self.tasks.append(watcher)
        self.client.queue.put_nowait((
            protocol.SipProtocolBugFixAsyncTest._remote_dialog_request(self.client, "BYE", 3),
            ("127.0.0.2", 5060),
        ))
        self.assertEqual(await asyncio.wait_for(watcher, 1), "remote_hangup")
        release.set()
        await asyncio.wait_for(task, 1)
        self.assertIsNone(self.client.dialog)
        self.assertEqual(
            {"reservation_released": self.client._prepared_reinvite is None,
             "operations": operations, "notifications": notifications},
            {"reservation_released": True,
             "operations": ["rollback"] if phase == "prepare" else ["commit", "rollback"],
             "notifications": []},
        )
        self.assertFalse(any(sip.parse_message(raw).method == "INVITE" for raw, _ in self.sent[offset:]))
        self.assert_no_bye()

    async def test_remote_bye_during_preparation_prevents_stale_media_commit(self):
        await self.exercise_remote_bye("prepare")

    async def test_remote_bye_during_commit_rolls_back_unpublished_media(self):
        await self.exercise_remote_bye("commit")

    async def test_cancel_at_publication_does_not_rollback_a_published_generation(self):
        operations = []
        task = None

        async def commit():
            operations.append("commit")
            # Schedule the caller's cancellation at the commit/finalize handoff.
            # The actual common finalizer still runs, protected by its owner.
            asyncio.get_running_loop().call_soon(task.cancel)

        async def rollback():
            operations.append("rollback")

        async def prepare(_previous, _candidate, _method):
            return sip_client.PreparedDialogMediaUpdate(commit=commit, rollback=rollback)

        self.client.on_media_update = prepare
        task, _offset = await self.begin()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        published = self.client.dialog is not None and self.client.dialog is not self.previous
        self.assertFalse(
            published and "rollback" in operations,
            f"Published signaling generation retained after media rollback: {operations}",
        )
