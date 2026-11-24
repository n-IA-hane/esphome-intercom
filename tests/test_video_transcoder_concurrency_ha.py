"""Independent real FFmpeg owners coexist and release only their own resources."""

import asyncio
import socket

import pytest

from custom_components.voip_stack.core.sdp import RtpVideoFormat
from custom_components.voip_stack.video_transcoder import FfmpegVideoTranscoder
from tests.test_automation_originate_ha import lab as lab

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]


async def test_real_converters_remain_independent_after_one_call_closes(lab):
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    workers = []
    try:
        for index, target in enumerate(sockets):
            target.bind(("127.0.0.1", 0))
            workers.append(FfmpegVideoTranscoder(
                hass=lab.hass, call_id=f"concurrent-call-{index}",
                input_format=RtpVideoFormat(payload_type=96, encoding="H264", packetization_mode=1,
                                             profile_level_id="42e01f"),
                output_port=target.getsockname()[1],
                output_format=RtpVideoFormat(payload_type=104, encoding="VP8"),
            ))
        await asyncio.gather(*(worker.async_start() for worker in workers))
        processes = [worker.process for worker in workers]
        assert all(worker.ready for worker in workers)
        assert processes[0].pid != processes[1].pid
        assert len(lab.runtime.media.transcoders) == 2
        await workers[0].async_close()
        assert processes[0].returncode is not None
        assert workers[1].ready and processes[1].returncode is None
        assert len(lab.runtime.media.transcoders) == 1
        await workers[1].async_close()
        assert processes[1].returncode is not None
        assert not lab.runtime.media.transcoders
    finally:
        await asyncio.gather(*(worker.async_close() for worker in workers))
        for target in sockets:
            target.close()


async def test_entry_shutdown_joins_all_real_video_workers(lab):
    from custom_components.voip_stack.browser_phone_runtime import _async_shutdown_all

    targets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    workers = []
    try:
        for index, target in enumerate(targets):
            target.bind(("127.0.0.1", 0))
            workers.append(FfmpegVideoTranscoder(
                hass=lab.hass, call_id=f"shutdown-video-{index}",
                input_format=RtpVideoFormat(payload_type=96, encoding="H264", packetization_mode=1,
                                             profile_level_id="42e01f"),
                output_port=target.getsockname()[1],
            ))
        await asyncio.gather(*(worker.async_start() for worker in workers))
        processes = [worker.process for worker in workers]
        await _async_shutdown_all(lab.hass)
        assert not lab.runtime.media.transcoders
        assert all(process.returncode is not None for process in processes)
    finally:
        await asyncio.gather(*(worker.async_close() for worker in workers))
        for target in targets:
            target.close()
