#!/usr/bin/env python3
"""SIP client, listener and media socket runtime contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    Path,
    _load_audio_ws_runtime_module,
    _load_intercom_module,
    _load_video_ws_runtime_module,
    _reserved_udp_ports,
    asyncio,
    audio_format,
    contextlib,
    dtmf,
    patch,
    roster,
    router,
    rtp,
    sdp,
    sip,
    sip_client,
    sip_endpoint,
    sip_listener,
    sip_trunk,
    socket,
    tempfile,
    threading,
    time,
    types,
    unittest,
)


class SipClientSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_audio_websocket_relays_pcm_without_rtp_and_isolates_bad_frames(
        self,
    ) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        audio_contract = audio_format.HA_SIP_PCM_FORMATS[0]
        expected = int(audio_contract.nominal_frame_bytes)
        peer_pcm = bytes((index % 251 for index in range(expected)))
        browser_pcm = bytes((250 - (index % 251) for index in range(expected)))

        class Bridge:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.peer_delivered = False
                self.closed = asyncio.Event()

            async def receive_audio(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> bytes:
                if not self.peer_delivered:
                    self.peer_delivered = True
                    return peer_pcm
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            def send_audio(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                pcm: bytes,
            ) -> bool:
                self.sent.append(bytes(pcm))
                return False

            async def wait_closed(self, _call_id: str) -> None:
                await self.closed.wait()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages = [
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=audio_ws.encode_audio_frame(bytes(expected + 1)),
                    ),
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=audio_ws.encode_audio_frame(browser_pcm),
                    ),
                ]
                self.peer_frame_sent = asyncio.Event()
                self.forced_closed = False

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.peer_frame_sent.set()

            def force_close(self) -> None:
                self.forced_closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.messages:
                    return self.messages.pop(0)
                await self.peer_frame_sent.wait()
                raise StopAsyncIteration

        lease = types.SimpleNamespace(
            call_id="local-audio",
            endpoint_id="kitchen",
            token="lease-token",
        )
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {const.CONF_DEBUG_MODE: False}},
            store={"call_id": lease.call_id, "state": "in_call"},
        )
        bridge = Bridge()
        ws = WebSocket()

        await asyncio.wait_for(
            audio_ws_view._run_local_audio_session(hass, ws, bridge, lease),
            timeout=1,
        )

        self.assertEqual(bridge.sent, [browser_pcm])
        self.assertEqual(
            [audio_ws.decode_audio_frame(frame) for frame in ws.binary],
            [peer_pcm],
        )
        self.assertEqual(ws.json[0]["media_transport"], "local_websocket")
        self.assertEqual(ws.json[0]["audio_direction"], "sendrecv")
        self.assertEqual(hass.store["drop_payload_size"], 1)
        self.assertEqual(hass.store["ws_rx"], 1)
        self.assertEqual(hass.store["ws_tx"], 1)

    async def test_local_video_websocket_relays_access_units_and_keyframe_control(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        peer_frame = video_ws_view._VIDEO_HEADER.pack(
            video_ws_view._VIDEO_ACCESS_UNIT,
            0,
            9000,
        ) + b"peer-vp8"
        browser_frame = video_ws_view._VIDEO_HEADER.pack(
            video_ws_view._VIDEO_ACCESS_UNIT,
            0,
            18000,
        ) + b"browser-vp8"

        class Snapshot:
            @staticmethod
            def video_direction_for(_endpoint_id: str) -> str:
                return "sendrecv"

        class Bridge:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.controls: list[str] = []
                self.peer_delivered = False
                self.control_delivered = False
                self.closed = asyncio.Event()

            @staticmethod
            def require_call(_call_id: str) -> Snapshot:
                return Snapshot()

            async def receive_video(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> bytes:
                if not self.peer_delivered:
                    self.peer_delivered = True
                    return peer_frame
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def receive_video_control(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> str:
                if not self.control_delivered:
                    self.control_delivered = True
                    return "force_key_frame"
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            def send_video(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                frame: bytes,
            ) -> bool:
                self.sent.append(bytes(frame))
                return False

            def send_video_control(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                control: str,
            ) -> bool:
                self.controls.append(control)
                return False

            async def wait_closed(self, _call_id: str) -> None:
                await self.closed.wait()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages = [
                    types.SimpleNamespace(type=WSMsgType.BINARY, data=b"bad"),
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=browser_frame,
                    ),
                    types.SimpleNamespace(
                        type=WSMsgType.TEXT,
                        data='{"type":"request_key_frame"}',
                    ),
                ]
                self.peer_frame_sent = asyncio.Event()
                self.control_sent = asyncio.Event()
                self.forced_closed = False

            async def send_json(self, payload: dict) -> None:
                copied = dict(payload)
                self.json.append(copied)
                if copied.get("type") == "force_key_frame":
                    self.control_sent.set()

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.peer_frame_sent.set()

            def force_close(self) -> None:
                self.forced_closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.gather(
                    self.peer_frame_sent.wait(),
                    self.control_sent.wait(),
                )
                raise StopAsyncIteration

        lease = types.SimpleNamespace(
            call_id="local-video",
            endpoint_id="kitchen",
            token="lease-token",
        )
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {const.CONF_DEBUG_MODE: False}},
            store={"call_id": lease.call_id, "state": "in_call"},
        )
        bridge = Bridge()
        ws = WebSocket()

        await asyncio.wait_for(
            video_ws_view._run_local_video_session(hass, ws, bridge, lease),
            timeout=1,
        )

        self.assertEqual(bridge.sent, [browser_frame])
        self.assertEqual(bridge.controls, ["force_key_frame"])
        self.assertEqual(ws.binary, [peer_frame])
        self.assertEqual(ws.json[0]["media_transport"], "local_websocket")
        self.assertEqual(ws.json[0]["direction"], "sendrecv")
        self.assertTrue(
            any(item.get("type") == "force_key_frame" for item in ws.json)
        )
        self.assertEqual(hass.store["video_drop_error"], 1)
        self.assertEqual(hass.store["video_access_units_tx"], 1)
        self.assertEqual(hass.store["video_access_units_rx"], 1)

    def test_video_udp_ingress_is_bounded_by_source_size_count_and_bytes(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        queue = video_ws_view._ByteBudgetQueue(
            maxsize=2,
            max_bytes=6,
            item_size=lambda item: len(item[0]),
        )
        protocol = video_ws_view._RtpVideoProtocol(
            queue,
            source_allowed=lambda host: host == "192.0.2.10",
            min_datagram_bytes=2,
            max_datagram_bytes=4,
        )

        protocol.datagram_received(b"bad", ("192.0.2.11", 4000))
        protocol.datagram_received(b"12345", ("192.0.2.10", 4000))
        protocol.datagram_received(b"abc", ("192.0.2.10", 4000))
        protocol.datagram_received(b"def", ("192.0.2.10", 4000))
        protocol.datagram_received(b"gh", ("192.0.2.10", 4000))

        self.assertEqual(protocol.dropped_packets, 3)
        self.assertEqual(protocol.take_drop_counts(), (1, 1, 1))
        self.assertEqual(protocol.take_drop_counts(), (0, 0, 0))
        self.assertEqual(queue.queued_bytes, 5)
        self.assertEqual(queue.get_nowait()[0], b"def")
        self.assertEqual(queue.queued_bytes, 2)
        self.assertEqual(queue.get_nowait()[0], b"gh")
        self.assertEqual(queue.queued_bytes, 0)

    def test_video_access_unit_queue_tracks_a_byte_budget(self) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        queue = video_ws_view._ByteBudgetQueue(
            maxsize=3,
            max_bytes=5,
            item_size=lambda item: len(item.data),
        )
        first = types.SimpleNamespace(data=b"abc")
        second = types.SimpleNamespace(data=b"de")
        queue.put_nowait(first)
        queue.put_nowait(second)

        self.assertEqual(queue.queued_bytes, 5)
        self.assertFalse(queue.can_fit(types.SimpleNamespace(data=b"f")))
        self.assertIs(queue.get_nowait(), first)
        self.assertEqual(queue.queued_bytes, 2)
        self.assertTrue(queue.can_fit(types.SimpleNamespace(data=b"fgh")))
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(types.SimpleNamespace(data=b"toolarge"))
        self.assertEqual(queue.queued_bytes, 2)

    async def test_video_packetization_executor_preserves_call_control_deadline(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        call_control_ran = asyncio.Event()

        def slow_packetizer(*_args, **_kwargs):
            time.sleep(0.08)
            return [(b"rtp", 3)]

        async def call_control() -> None:
            await asyncio.sleep(0.005)
            call_control_ran.set()

        with patch.object(
            video_ws_view,
            "_packetize_browser_access_unit",
            side_effect=slow_packetizer,
        ):
            packetize_task = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"frame",
                    encoding="VP8",
                    payload_type=103,
                    sequence=1,
                    timestamp=9000,
                    ssrc=7,
                )
            )
            control_task = asyncio.create_task(call_control())
            try:
                await asyncio.wait_for(call_control_ran.wait(), timeout=0.04)
                self.assertFalse(packetize_task.done())
                self.assertEqual(await packetize_task, [(b"rtp", 3)])
            finally:
                await asyncio.gather(
                    packetize_task,
                    control_task,
                    return_exceptions=True,
                )

    async def test_video_packetization_has_one_global_cpu_slot_and_skips_stale_work(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocking_packetizer(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=1)
            return [(b"rtp", 3)]

        current = True
        with patch.object(
            video_ws_view,
            "_packetize_browser_access_unit",
            side_effect=blocking_packetizer,
        ):
            first = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"first",
                    encoding="VP8",
                    payload_type=103,
                    sequence=1,
                    timestamp=9000,
                    ssrc=7,
                )
            )
            await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"stale",
                    encoding="VP8",
                    payload_type=103,
                    sequence=2,
                    timestamp=12000,
                    ssrc=7,
                    should_continue=lambda: current,
                )
            )
            current = False
            release.set()
            self.assertEqual(await first, [(b"rtp", 3)])
            self.assertIsNone(await second)
        self.assertEqual(calls, 1)

    async def test_video_rtp_send_burst_yields_to_call_end(self) -> None:
        video_ws_view = _load_video_ws_runtime_module()

        class Transport:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def sendto(self, raw: bytes, _target: tuple[str, int]) -> None:
                self.sent.append(raw)

        transport = Transport()
        call_ended = asyncio.Event()
        asyncio.get_running_loop().call_soon(call_ended.set)
        sent: list[tuple[int, int]] = []
        datagrams = [(bytes(12), 4)] * (
            video_ws_view._VIDEO_RTP_SEND_BURST_PACKETS + 3
        )

        complete = await video_ws_view._send_packetized_datagrams(
            transport,
            datagrams,
            ("192.0.2.10", 4000),
            should_continue=lambda: not call_ended.is_set(),
            on_sent=lambda raw_bytes, payload_bytes: sent.append(
                (raw_bytes, payload_bytes)
            ),
        )

        self.assertFalse(complete)
        self.assertEqual(
            len(transport.sent),
            video_ws_view._VIDEO_RTP_SEND_BURST_PACKETS,
        )
        self.assertEqual(len(sent), len(transport.sent))

    async def test_debug_capture_tracks_home_assistant_executor_future(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        written = asyncio.Event()

        class Capture:
            capture_name = "future-contract"
            call_id = "debug-call"

            @staticmethod
            def write(counters: dict[str, int]) -> None:
                self.assertEqual(counters, {"rtp_rx": 7})

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}

            def async_add_executor_job(self, target, *args):
                async def run():
                    target(*args)
                    written.set()

                # HA exposes executor jobs as an asyncio Future. A resolved
                # Task has the same Future contract without needing threads in
                # this deterministic regression test.
                return asyncio.create_task(run())

        hass = Hass()
        audio_ws_view._schedule_debug_capture_write(
            hass,
            Capture(),
            {"rtp_rx": 7},
        )
        tasks = hass.data[const.DOMAIN]["debug_capture_tasks"]
        self.assertEqual(len(tasks), 1)
        await asyncio.wait_for(written.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(tasks)

    async def test_debug_capture_executor_queue_is_bounded(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        capture_limits = _load_intercom_module("debug_capture")

        class Capture:
            capture_name = "bounded-contract"

            def __init__(self, call_id: str) -> None:
                self.call_id = call_id

            def write(self, _counters: dict[str, int]) -> None:
                raise AssertionError("pending executor job must not run")

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}
                self.scheduled = 0

            def async_add_executor_job(self, _target, *_args):
                self.scheduled += 1
                return asyncio.get_running_loop().create_future()

        hass = Hass()
        for index in range(capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES + 3):
            audio_ws_view._schedule_debug_capture_write(
                hass,
                Capture(f"debug-{index}"),
                {},
            )

        tasks = hass.data[const.DOMAIN]["debug_capture_tasks"]
        self.assertEqual(
            len(tasks),
            capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES,
        )
        self.assertEqual(
            hass.scheduled,
            capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES,
        )
        for task in tasks:
            task.cancel()

    def test_debug_capture_write_slots_are_globally_bounded(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0
        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            self.assertFalse(capture_limits.try_reserve_debug_capture_write())
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

    def test_debug_capture_write_slots_report_global_occupancy(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        self.assertEqual(capture_limits.debug_capture_pending_writes(), 0)
        reserved = 0
        try:
            for expected in (1, 2):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
                self.assertEqual(
                    capture_limits.debug_capture_pending_writes(),
                    expected,
                )
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()
        self.assertEqual(capture_limits.debug_capture_pending_writes(), 0)

    def test_audio_debug_scheduler_reports_global_writer_saturation(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0

        class Capture:
            call_id = "saturated-debug"

            @staticmethod
            def write(_counters: dict[str, int]) -> None:
                raise AssertionError("saturated writer must not be scheduled")

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}
                self.scheduled = 0

            def async_add_executor_job(self, _target, *_args):
                self.scheduled += 1
                raise AssertionError("saturated writer must not reach executor")

        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            hass = Hass()
            audio_ws_view._schedule_debug_capture_write(hass, Capture(), {})
            self.assertEqual(hass.scheduled, 0)
            self.assertEqual(
                hass.data[const.DOMAIN]["debug_capture_dropped_writes"],
                1,
            )
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

    def test_audio_debug_capture_rolls_back_partially_published_group(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_format = sdp.audio_format_to_rtp(pcm, 96)
        capture = audio_ws_view._DebugAudioCapture(
            "atomic-debug",
            rx_format=rtp_format,
            tx_format=rtp_format,
        )
        capture.note_rtp_rx(1.0, bytes(pcm.nominal_frame_bytes))
        capture.note_ws_rx(1.0, bytes(pcm.nominal_frame_bytes))
        real_commit = audio_ws_view.commit_capture_file
        commits = 0

        def fail_second_commit(temporary: Path, destination: Path) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                raise OSError("diagnostic rename failed")
            real_commit(temporary, destination)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(audio_ws_view, "DEBUG_CAPTURE_DIR", Path(temp_dir)),
            patch.object(
                audio_ws_view,
                "debug_capture_transaction",
                side_effect=lambda: contextlib.nullcontext(),
            ),
            patch.object(
                audio_ws_view,
                "commit_capture_file",
                side_effect=fail_second_commit,
            ),
        ):
            with self.assertRaisesRegex(OSError, "rename failed"):
                capture.write({})
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_conference_audio_lifetime_ends_with_matching_call_event(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()

        class Bus:
            def __init__(self) -> None:
                self.listener = None
                self.removed = False

            def async_listen(self, _event_type, callback):
                self.listener = callback

                def remove() -> None:
                    self.removed = True

                return remove

        bus = Bus()
        hass = types.SimpleNamespace(
            bus=bus,
            store={"call_id": "conference:Ops", "state": "in_call"},
        )
        ended, remove = audio_ws_view._listen_for_call_end(hass, "conference:Ops")
        self.assertFalse(ended.is_set())

        bus.listener(types.SimpleNamespace(data={"call_id": "other", "state": "idle"}))
        self.assertFalse(ended.is_set())
        bus.listener(
            types.SimpleNamespace(
                data={"call_id": "conference:Ops", "state": "idle"}
            )
        )

        await asyncio.wait_for(ended.wait(), timeout=1)
        remove()
        self.assertTrue(bus.removed)

    async def test_conference_audio_drops_oversized_pcm_before_mixer(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        class Bus:
            def async_listen(self, _event_type, _callback):
                return lambda: None

        class Manager:
            def __init__(self) -> None:
                self.frames: list[tuple[str, bytes]] = []

            def push_ha_audio(self, room: str, pcm: bytes) -> None:
                self.frames.append((room, bytes(pcm)))

        class WebSocket:
            def __init__(self, messages) -> None:
                self.messages = list(messages)
                self.json: list[dict] = []

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, _payload: bytes) -> None:
                return None

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

        frame = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        expected = int(frame.audio_format.nominal_frame_bytes)
        invalid = audio_ws.encode_audio_frame(bytes(expected + 1))
        valid = audio_ws.encode_audio_frame(bytes(expected))
        ws = WebSocket(
            [
                types.SimpleNamespace(type=WSMsgType.BINARY, data=invalid),
                types.SimpleNamespace(type=WSMsgType.BINARY, data=valid),
            ]
        )
        manager = Manager()
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {"conference_manager": manager}},
            store={"call_id": "conference:Ops", "state": "in_call"},
            bus=Bus(),
        )
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="conference:Ops",
            local_rtp_port=0,
            remote_rtp_host="",
            remote_rtp_port=0,
            send_format=frame,
            recv_format=frame,
            conference_room="Ops",
            conference_queue=asyncio.Queue(),
        )

        await audio_ws_view._run_conference_audio_session(hass, ws, session)

        self.assertEqual(manager.frames, [("conference:Ops", bytes(expected))])
        self.assertEqual(hass.store["tx_error"], 1)
        self.assertLessEqual(audio_ws_view._MAX_BROWSER_AUDIO_MESSAGE_BYTES, 4096)

    async def test_audio_websocket_reinvite_rebuilds_live_encoder_and_decoder(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        class Bus:
            def __init__(self) -> None:
                self.listeners: list = []

            def async_listen(self, _event_type, callback):
                self.listeners.append(callback)

                def remove() -> None:
                    self.listeners.remove(callback)

                return remove

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {const.CONF_DEBUG_MODE: False}}
                self.store = {"call_id": "audio-reinvite", "state": "in_call"}
                self.bus = Bus()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages: asyncio.Queue = asyncio.Queue()
                self.changed = asyncio.Event()

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))
                self.changed.set()

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.changed.set()

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        async def wait_until(predicate, timeout: float = 1.0) -> None:
            deadline = asyncio.get_running_loop().time() + timeout
            while not predicate():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("condition not reached")
                ws.changed.clear()
                await asyncio.wait_for(ws.changed.wait(), timeout=remaining)

        loop = asyncio.get_running_loop()
        remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        remote.bind(("127.0.0.1", 0))
        remote.setblocking(False)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = int(probe.getsockname()[1])
        probe.close()

        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 10)
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="audio-reinvite",
            local_rtp_port=local_port,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=int(remote.getsockname()[1]),
            send_format=pcma,
            recv_format=pcma,
            signaling_host="127.0.0.1",
        )
        hass = Hass()
        ws = WebSocket()
        runtime = asyncio.create_task(
            audio_ws_view._run_audio_session(hass, ws, session)
        )
        try:
            await wait_until(lambda: bool(ws.json))
            first_pcm = bytes(
                (index % 251 for index in range(pcma.audio_format.nominal_frame_bytes))
            )
            first_payload = sip_client.RtpPayloadEncoder(pcma).encode(first_pcm)
            oversized_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=0,
                    timestamp=1,
                    ssrc=7,
                    payload=first_payload + b"\x00",
                )
            )
            first_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=1,
                    timestamp=1,
                    ssrc=7,
                    payload=first_payload,
                )
            )
            await loop.sock_sendto(remote, oversized_packet, ("127.0.0.1", local_port))
            await loop.sock_sendto(remote, first_packet, ("127.0.0.1", local_port))
            await wait_until(lambda: bool(ws.binary))
            expected_first = sip_client.RtpPayloadDecoder(pcma).decode(first_payload)
            self.assertEqual(len(ws.binary), 1)
            self.assertEqual(audio_ws.decode_audio_frame(ws.binary[-1]), expected_first)

            session.send_format = l16
            session.recv_format = l16
            session.media_generation += 1
            session.update_event.set()
            await wait_until(lambda: any(item.get("type") == "media_update" for item in ws.json))

            second_pcm = bytes(
                (index * 3) % 251 for index in range(l16.audio_format.nominal_frame_bytes)
            )
            second_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=l16.payload_type,
                    sequence=2,
                    timestamp=321,
                    ssrc=8,
                    payload=sip_client.RtpPayloadEncoder(l16).encode(second_pcm),
                )
            )
            await loop.sock_sendto(remote, second_packet, ("127.0.0.1", local_port))
            await wait_until(lambda: len(ws.binary) >= 2)
            self.assertEqual(audio_ws.decode_audio_frame(ws.binary[-1]), second_pcm)

            await ws.messages.put(
                types.SimpleNamespace(
                    type=WSMsgType.BINARY,
                    data=audio_ws.encode_audio_frame(second_pcm),
                )
            )
            await asyncio.sleep(0.05)
            if runtime.done():
                self.fail(f"audio runtime ended during re-INVITE: {runtime.exception()!r}")
            decoded_tx = None
            deadline = loop.time() + 1.0
            decoder = sip_client.RtpPayloadDecoder(l16)
            while loop.time() < deadline and decoded_tx != second_pcm:
                data = await asyncio.wait_for(
                    loop.sock_recv(remote, 65535),
                    timeout=max(0.01, deadline - loop.time()),
                )
                packet = rtp.parse_packet(data)
                if packet.payload_type == l16.payload_type:
                    decoded_tx = decoder.decode(packet.payload)
            self.assertEqual(decoded_tx, second_pcm)
        finally:
            await ws.messages.put(None)
            await asyncio.wait_for(runtime, timeout=1)
            remote.close()

    async def test_audio_websocket_projects_negotiated_rfc4733_once(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")

        class Bus:
            def async_listen(self, _event_type, _callback):
                return lambda: None

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {const.CONF_DEBUG_MODE: False}}
                self.store = {"call_id": "audio-dtmf", "state": "in_call"}
                self.bus = Bus()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages: asyncio.Queue = asyncio.Queue()

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        loop = asyncio.get_running_loop()
        remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        remote.bind(("127.0.0.1", 0))
        remote.setblocking(False)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = int(probe.getsockname()[1])
        probe.close()
        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        digits: list[str] = []
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="audio-dtmf",
            local_rtp_port=local_port,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=int(remote.getsockname()[1]),
            send_format=pcma,
            recv_format=pcma,
            signaling_host="127.0.0.1",
            dtmf_payload_type=101,
            dtmf_events=frozenset(range(16)),
            on_dtmf=digits.append,
        )
        hass = Hass()
        ws = WebSocket()
        runtime = asyncio.create_task(
            audio_ws_view._run_audio_session(hass, ws, session)
        )
        try:
            deadline = loop.time() + 1.0
            while not ws.json and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(ws.json)
            for sequence, end in ((1, False), (2, True), (3, True)):
                packet = rtp.build_packet(
                    rtp.RtpPacket(
                        payload_type=101,
                        sequence=sequence,
                        timestamp=1234,
                        ssrc=9,
                        payload=dtmf.build_telephone_event_payload(
                            "6", duration=160, end=end
                        ),
                    )
                )
                await loop.sock_sendto(
                    remote, packet, ("127.0.0.1", local_port)
                )
            deadline = loop.time() + 1.0
            while digits != ["6"] and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(digits, ["6"])

            pcm = bytes(pcma.audio_format.nominal_frame_bytes)
            audio_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=4,
                    timestamp=1394,
                    ssrc=7,
                    payload=sip_client.RtpPayloadEncoder(pcma).encode(pcm),
                )
            )
            await loop.sock_sendto(
                remote, audio_packet, ("127.0.0.1", local_port)
            )
            deadline = loop.time() + 1.0
            while not ws.binary and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(ws.binary)
        finally:
            await ws.messages.put(None)
            await asyncio.wait_for(runtime, timeout=1)
            remote.close()

    async def test_cancelled_tcp_close_releases_media_before_writer_drain(self) -> None:
        class Reservation:
            def __init__(self) -> None:
                self.releases = 0

            def release(self) -> None:
                self.releases += 1

        class BlockingTcpWriter:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def close(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()

        class StreamWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        reservation = Reservation()
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            media_reservation=reservation,
            video_rtp_socket=rtp_socket,
            video_rtcp_socket=rtcp_socket,
        )
        tcp_writer = BlockingTcpWriter()
        stream_writer = StreamWriter()
        client._tcp_writer = tcp_writer
        client.writer = stream_writer

        close_task = asyncio.create_task(client.close())
        await asyncio.wait_for(tcp_writer.entered.wait(), timeout=1)
        close_task.cancel()
        await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        self.assertEqual(reservation.releases, 1)
        self.assertEqual(rtp_socket.fileno(), -1)
        self.assertEqual(rtcp_socket.fileno(), -1)
        self.assertFalse(stream_writer.closed)
        tcp_writer.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await close_task

        self.assertEqual(reservation.releases, 1)
        self.assertEqual(rtp_socket.fileno(), -1)
        self.assertEqual(rtcp_socket.fileno(), -1)
        self.assertTrue(stream_writer.closed)
        self.assertIsNone(client.media_reservation)
        self.assertIsNone(client._tcp_writer)
        self.assertIsNone(client.writer)
        self.assertTrue(client._closed)

    async def test_concurrent_tcp_close_waiters_share_one_completion_barrier(self) -> None:
        class BlockingTcpWriter:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def close(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()

        class StreamWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        tcp_writer = BlockingTcpWriter()
        stream_writer = StreamWriter()
        client._tcp_writer = tcp_writer
        client.writer = stream_writer

        first = asyncio.create_task(client.close())
        await asyncio.wait_for(tcp_writer.entered.wait(), timeout=1)
        second = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(tcp_writer.calls, 1)

        tcp_writer.release.set()
        await asyncio.gather(first, second)
        self.assertTrue(stream_writer.closed)
        self.assertTrue(client._closed)

    async def test_close_completes_deferred_cancel_before_closing_transport(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []
                self.closed = False

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                self.closed = True

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()

        async def read_response(_timeout: float):
            status, reason, method = await responses.get()
            return (
                sip.parse_message(
                    sip.build_response(
                        status,
                        reason,
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} {method}"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="127.0.0.2",
                remote_sip_port=5060,
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        close_task = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE"],
        )

        responses.put_nowait((100, "Trying", "INVITE"))
        while not any(
            sip.parse_message(raw).method == "CANCEL"
            for raw, _addr in transport.sent
        ):
            await asyncio.sleep(0)
        responses.put_nowait((200, "OK", "CANCEL"))
        responses.put_nowait((487, "Request Terminated", "INVITE"))

        self.assertEqual(await asyncio.wait_for(owner, timeout=1), "cancelled")
        await asyncio.wait_for(close_task, timeout=1)

        self.assertIsNone(client.dialog)
        self.assertIsNone(client.early_dialog)
        self.assertTrue(client._closed)
        self.assertTrue(transport.closed)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK"],
        )

    async def test_close_owns_final_waiter_and_acks_late_200_with_bye(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []
                self.closed = False

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                self.closed = True

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[
            tuple[sip.SipMessage, tuple[str, int]]
        ] = asyncio.Queue()

        async def read_response(_timeout: float):
            return await responses.get()

        def response(
            status: int,
            reason: str,
            method: str,
            *,
            body: bytes = b"",
        ) -> tuple[sip.SipMessage, tuple[str, int]]:
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 127.0.0.1:5060;branch="
                    f"{client.dialog_ids.branch}",
                ),
                (
                    "From",
                    f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Contact", "<sip:dialog@127.0.0.2:5090>"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers, body)),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="127.0.0.2",
                remote_sip_port=5060,
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        responses.put_nowait(response(180, "Ringing", "INVITE"))
        self.assertEqual(await asyncio.wait_for(owner, timeout=1), "ringing")

        final_waiter = asyncio.create_task(client.wait_for_final(timeout=2))
        while client._final_response_task is None:
            await asyncio.sleep(0)
        close_task = asyncio.create_task(client.close())
        while not any(
            sip.parse_message(raw).method == "CANCEL"
            for raw, _addr in transport.sent
        ):
            await asyncio.sleep(0)
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
        ).encode()
        responses.put_nowait(response(200, "OK", "INVITE", body=answer))

        self.assertEqual(
            await asyncio.wait_for(final_waiter, timeout=1),
            "cancelled",
        )
        await asyncio.wait_for(close_task, timeout=1)

        self.assertIsNone(client.dialog)
        self.assertTrue(client._closed)
        self.assertTrue(transport.closed)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK", "BYE"],
        )

    async def test_cancelled_dialog_waiter_joins_both_child_tasks(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.dialog = types.SimpleNamespace(remote_host="127.0.0.2")  # type: ignore[assignment]
        read_started = asyncio.Event()
        read_cancelled = asyncio.Event()
        release_read = asyncio.Event()

        async def blocked_read(_timeout: float):
            read_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                read_cancelled.set()
                await release_read.wait()
                raise

        client._read_response = blocked_read  # type: ignore[method-assign]
        waiter = asyncio.create_task(client.wait_for_dialog_termination())
        await asyncio.wait_for(read_started.wait(), timeout=1)
        waiter.cancel()
        await asyncio.wait_for(read_cancelled.wait(), timeout=1)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        release_read.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=1)

        pending_names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done()
        }
        self.assertFalse(
            any(name.startswith("voip-sip-dialog-") for name in pending_names)
        )
        await client.close()

    async def test_udp_start_cannot_publish_transport_after_close(self) -> None:
        entered = asyncio.Event()
        release_endpoint = asyncio.Event()

        class Transport:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            def get_extra_info(self, _name: str):
                return ("0.0.0.0", 5099)

        transport = Transport()

        async def create_endpoint(*_args, **_kwargs):
            entered.set()
            await release_endpoint.wait()
            return transport, object()

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "create_datagram_endpoint",
            new=create_endpoint,
        ):
            start_task = asyncio.create_task(client.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.wait_for(client.close(), timeout=1)
            release_endpoint.set()
            with self.assertRaisesRegex(RuntimeError, "closed while starting"):
                await start_task

        self.assertTrue(transport.closed)
        self.assertIsNone(client.transport)
        self.assertIsNone(client.protocol)
        self.assertTrue(client._closed)

    def test_video_endpoint_manager_requires_media_update_handler(self) -> None:
        async def on_invite(_invite):
            return None

        with self.assertRaisesRegex(ValueError, "media-update handler"):
            sip_endpoint.SipEndpointManager(
                host="0.0.0.0",
                port=5060,
                local_ip="127.0.0.1",
                local_rtp_port=41000,
                supported_formats=[
                    audio_format.AudioFormat(16000, "s16le", 1, 20)
                ],
                on_invite=on_invite,
                enable_video=True,
            )

    async def test_endpoint_manager_cancelled_partial_start_stops_both_servers(self) -> None:
        class Server:
            def __init__(self, *, blocked: bool = False) -> None:
                self.blocked = blocked
                self.entered = asyncio.Event()
                self.stop_calls = 0

            async def start(self) -> bool:
                self.entered.set()
                if self.blocked:
                    await asyncio.Event().wait()
                return True

            async def stop(self) -> None:
                self.stop_calls += 1

        udp = Server()
        tcp = Server(blocked=True)

        async def on_invite(_invite):
            return None

        manager = sip_endpoint.SipEndpointManager(
            host="0.0.0.0",
            port=5060,
            local_ip="127.0.0.1",
            local_rtp_port=41000,
            supported_formats=[
                audio_format.AudioFormat(16000, "s16le", 1, 20)
            ],
            on_invite=on_invite,
        )
        with (
            patch.object(sip_endpoint, "SipUdpServer", return_value=udp),
            patch.object(sip_endpoint, "SipTcpServer", return_value=tcp),
        ):
            starting = asyncio.create_task(manager.start())
            await asyncio.wait_for(tcp.entered.wait(), timeout=1)
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(starting, timeout=1)

        self.assertEqual(udp.stop_calls, 1)
        self.assertEqual(tcp.stop_calls, 1)
        self.assertIsNone(manager.udp_server)
        self.assertIsNone(manager.tcp_server)

    async def test_endpoint_manager_stop_cancels_inflight_start_without_resurrection(self) -> None:
        class Server:
            def __init__(self, *, blocked: bool = False) -> None:
                self.blocked = blocked
                self.entered = asyncio.Event()
                self.stop_calls = 0

            async def start(self) -> bool:
                self.entered.set()
                if self.blocked:
                    await asyncio.Event().wait()
                return True

            async def stop(self) -> None:
                self.stop_calls += 1

        udp = Server()
        tcp = Server(blocked=True)

        async def on_invite(_invite):
            return None

        manager = sip_endpoint.SipEndpointManager(
            host="0.0.0.0",
            port=5060,
            local_ip="127.0.0.1",
            local_rtp_port=41000,
            supported_formats=[
                audio_format.AudioFormat(16000, "s16le", 1, 20)
            ],
            on_invite=on_invite,
        )
        with (
            patch.object(sip_endpoint, "SipUdpServer", return_value=udp),
            patch.object(sip_endpoint, "SipTcpServer", return_value=tcp),
        ):
            starting = asyncio.create_task(manager.start())
            await asyncio.wait_for(tcp.entered.wait(), timeout=1)
            await asyncio.wait_for(manager.stop(), timeout=1)
            with self.assertRaises(asyncio.CancelledError):
                await starting

        self.assertEqual(udp.stop_calls, 1)
        self.assertEqual(tcp.stop_calls, 1)
        self.assertIsNone(manager.udp_server)
        self.assertIsNone(manager.tcp_server)
        self.assertTrue(manager._stopped)

    async def test_trunk_stop_cannot_resurrect_delayed_tcp_connection(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        reader = asyncio.StreamReader()

        class Writer:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self.closed

            def get_extra_info(self, _name: str):
                return ("127.0.0.1", 5060)

        writer = Writer()

        async def delayed_open_connection(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return reader, writer

        with patch.object(
            sip_trunk.asyncio,
            "open_connection",
            new=delayed_open_connection,
        ):
            starting = asyncio.create_task(trunk.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            stopping = asyncio.create_task(trunk.stop())
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, timeout=1)
            await asyncio.wait_for(starting, timeout=1)

        self.assertTrue(writer.closed)
        self.assertTrue(trunk._stopped)
        self.assertIsNone(trunk.reader)
        self.assertIsNone(trunk.writer)
        self.assertIsNone(trunk._tcp_writer)
        self.assertIsNone(trunk._receive_task)
        self.assertIsNone(trunk._refresh_task)

    async def test_video_capable_trunk_profile_negotiates_h264_end_to_end(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(5) as ports:
            sip_port, server_audio, server_video, client_audio, client_video = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        seen: dict[str, object] = {}

        async def on_invite(invite):
            seen["video"] = invite.video_format
            seen["local_video"] = invite.local_video_format
            seen["video_payload_types"] = invite.remote_video_payload_types
            self.assertIsNotNone(invite.video_format)
            answer = sdp.build_answer_directional(
                local,
                local,
                server_audio,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=server_video,
                video_format=invite.recv_video_format,
                video_direction="sendrecv",
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipUdpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_audio,
            supported_formats=[audio],
            on_invite=on_invite,
            enable_video=True,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="HA Mare",
            local_sip_port=5060,
            local_rtp_port=client_audio,
            supported_formats=[audio],
            local_video_rtp_port=client_video,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
            video_direction="sendrecv",
            username="390000000001",
            auth_username="390000000001",
            password="test-only",
        )
        try:
            self.assertEqual(
                await client.invite(
                    target="390000000002",
                    remote_host=local,
                    remote_sip_port=sip_port,
                ),
                "in_call",
            )
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertIsNotNone(client.dialog.video_format)
            assert client.dialog.video_format is not None
            self.assertEqual(client.dialog.video_format.encoding, "H264")
            self.assertIsNotNone(client.dialog.local_video_format)
            assert client.dialog.local_video_format is not None
            self.assertEqual(client.dialog.local_video_format.encoding, "H264")
            self.assertEqual(client.dialog.remote_video_rtp_port, server_video)
            self.assertEqual(client.dialog.local_video_direction, "sendrecv")
            self.assertIsNotNone(seen.get("video"))
            self.assertIsNotNone(seen.get("local_video"))
            self.assertEqual(
                seen.get("video_payload_types"),
                (sdp.DEFAULT_H264_FORMAT.payload_type,),
            )
        finally:
            client.bye()
            await client.close()
            await server.stop()

    async def test_outbound_dialog_keeps_h264_offer_and_answer_levels(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(5) as ports:
            sip_port, server_audio, server_video, client_audio, client_video = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        high = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )

        async def on_invite(invite):
            self.assertEqual(invite.video_format.profile_level_id, "42801f")
            low_answer = sdp.RtpVideoFormat(
                payload_type=invite.video_format.payload_type,
                profile_level_id="42800d",
                packetization_mode=invite.video_format.packetization_mode,
                level_asymmetry_allowed=True,
                direction=invite.video_format.direction,
                transport_profile=invite.video_format.transport_profile,
            )
            answer = sdp.build_answer_directional(
                local,
                local,
                server_audio,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=server_video,
                video_format=low_answer,
                video_direction="sendrecv",
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipUdpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_audio,
            supported_formats=[audio],
            on_invite=on_invite,
            enable_video=True,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=client_audio,
            supported_formats=[audio],
            local_video_rtp_port=client_video,
            video_formats=(high,),
            video_direction="sendrecv",
        )
        try:
            result = await client.invite(
                target="peer",
                remote_host=local,
                remote_sip_port=sip_port,
            )
            self.assertEqual(result, "in_call")
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(
                client.dialog.send_video_format.profile_level_id,
                "42800d",
            )
            self.assertEqual(
                client.dialog.recv_video_format.profile_level_id,
                "42801f",
            )
        finally:
            client.bye()
            await client.close()
            await server.stop()

    async def test_invite_100_trying_stops_udp_retransmission_without_reporting_ringing(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        sends: list[bytes] = []
        read_timeouts: list[float] = []

        async def fake_start() -> None:
            return None

        async def fake_send(raw: bytes, _host: str, _port: int) -> None:
            sends.append(raw)

        async def fake_read(timeout: float):
            read_timeouts.append(timeout)
            if len(read_timeouts) > 1:
                return None
            response = sip.build_response(
                100,
                "Trying",
                [
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                    ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("To", "<sip:ESP@127.0.0.2>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{client._invite_cseq} INVITE"),
                ],
            )
            return sip.parse_message(response), ("127.0.0.2", 5060)

        client.start = fake_start  # type: ignore[method-assign]
        client._send_raw = fake_send  # type: ignore[method-assign]
        client._read_response = fake_read  # type: ignore[method-assign]

        result = await client.invite(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            timeout=2.0,
        )

        self.assertEqual(result, "timeout")
        self.assertEqual(len(sends), 1)
        self.assertGreater(read_timeouts[-1], 1.0)

    async def test_outbound_client_advertises_bound_socket_port(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        try:
            await client.start()
            self.assertNotEqual(client.local_sip_port, 5060)
            self.assertGreater(client.local_sip_port, 0)
        finally:
            await client.close()

    def test_sip_listener_prefers_intercom_display_identity_headers(self) -> None:
        body = sdp.build_offer(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            [audio_format.AudioFormat(16000, "s16le", 1, 32)],
        ).encode()
        raw = sip.build_request(
            "INVITE",
            "sip:Spotpear_Ball_v2@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.47:5060;branch=z9hG4bKdisplay"),
                ("From", "<sip:Waveshare_S3_Audio@192.168.1.47>;tag=src"),
                ("To", "<sip:Spotpear_Ball_v2@192.168.1.10>"),
                ("Call-ID", "call-display"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Waveshare_S3_Audio@192.168.1.47:5060>"),
                ("Content-Type", "application/sdp"),
                ("X-Voip-Stack-Caller-Name", "Waveshare S3 Audio"),
                ("X-Voip-Stack-Dest-Name", "Spotpear Ball v2"),
            ],
            body,
        )
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
        )
        invite = endpoint._parse_invite(sip.parse_message(raw), ("192.168.1.47", 5060))
        self.assertIsNotNone(invite)
        assert invite is not None
        self.assertEqual(invite.caller, "Waveshare S3 Audio")
        self.assertEqual(invite.target, "Spotpear Ball v2")

    async def test_listener_replies_405_and_501_for_unsupported_methods(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )

        register = (
            b"REGISTER sip:Casa@192.168.1.10 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKreg;rport\r\n"
            b"From: <sip:ESP@192.168.1.20>;tag=src\r\n"
            b"To: <sip:Casa@192.168.1.10>\r\n"
            b"Call-ID: reg-1\r\n"
            b"CSeq: 1 REGISTER\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await endpoint._handle_datagram(register, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 405)
        self.assertIn("INVITE", sip.parse_message(sent[-1]).header("Allow"))

        custom = register.replace(b"REGISTER", b"BREW")
        await endpoint._handle_datagram(custom, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 501)

    async def test_listener_200_ok_invite_includes_contact(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(48000, "s16le", 1, 10)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
            signaling_transport="TCP",
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10;transport=tcp",
            [
                ("Via", "SIP/2.0/TCP 192.168.1.48:38946;branch=z9hG4bKcontact;rport"),
                ("From", '"Test Baresip" <sip:test@192.168.1.48>;tag=src'),
                ("To", "<sip:Casa@192.168.1.10;transport=tcp>"),
                ("Call-ID", "contact-200-ok"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:test@192.168.1.48:38946;transport=tcp>"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 38946))

        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.header("Contact"), "<sip:Casa@192.168.1.10:5060;transport=tcp>")
        self.assertIn("L16/48000/1", response.body.decode())

    async def test_listener_retransmits_udp_invite_2xx_until_matching_ack(self) -> None:
        sent: list[bytes] = []
        second_2xx = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            rtp_fmt,
            rtp_fmt,
        )

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            message = sip.parse_message(data)
            if message.status_code == 200 and sum(
                sip.parse_message(raw).status_code == 200 for raw in sent
            ) >= 2:
                second_2xx.set()

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=capture,
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bK2xx;rport"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "udp-2xx-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        addr = ("192.168.1.48", 5060)

        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.005),
                patch.object(sip_listener, "_SIP_T2", 0.01),
                patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.1),
            ):
                await endpoint._handle_datagram(invite, addr)
                await asyncio.wait_for(second_2xx.wait(), timeout=0.2)
                dialog = endpoint.active_dialogs["udp-2xx-call"]
                self.assertEqual(dialog.pending_ack_cseq, 7)
                self.assertGreaterEqual(dialog.invite_2xx_retransmissions, 1)
                self.assertEqual(endpoint.snapshot()["pending_invite_acks"], 1)

                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.168.1.10:5060",
                    [
                        ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKack"),
                        ("From", "<sip:test@192.168.1.48>;tag=remote"),
                        ("To", f"<sip:Casa@192.168.1.10>;tag={dialog.to_tag}"),
                        ("Call-ID", "udp-2xx-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                await endpoint._handle_datagram(ack, (addr[0], 5090))
                count_after_ack = len(sent)
                await asyncio.sleep(0.025)

                self.assertEqual(len(sent), count_after_ack)
                self.assertEqual(dialog.pending_ack_cseq, 0)
                self.assertIsNone(dialog.invite_2xx_task)
                self.assertEqual(endpoint.snapshot()["pending_invite_acks"], 0)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_retransmits_tcp_invite_2xx_and_accepts_proxy_ack(self) -> None:
        sent: list[bytes] = []
        retransmitted = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            if sip.parse_message(data).status_code == 200:
                retransmitted.set()

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=capture,
            signaling_transport="TCP",
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.0.2.20",
                [
                    ("Via", "SIP/2.0/TCP 192.0.2.10;branch=z9hG4bKtcp-2xx"),
                    ("From", "<sip:test@192.0.2.10>;tag=remote"),
                    ("To", "<sip:Casa@192.0.2.20>"),
                    ("Contact", "<sip:test@192.0.2.10:5060;transport=tcp>"),
                    ("Call-ID", "tcp-2xx-call"),
                    ("CSeq", "7 INVITE"),
                ],
            )
        )
        dialog = sip_listener._ActiveDialog(
            request,
            ("192.0.2.10", 5060),
            "local",
            8,
            "TCP",
            answer_sdp="v=0\r\n",
        )
        endpoint.active_dialogs["tcp-2xx-call"] = dialog

        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.005),
                patch.object(sip_listener, "_SIP_T2", 0.01),
                patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.1),
            ):
                endpoint._arm_invite_2xx(
                    dialog,
                    request,
                    dialog.addr,
                    200,
                    "OK",
                    "v=0\r\n",
                )
                await asyncio.wait_for(retransmitted.wait(), timeout=0.2)
                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.0.2.20",
                    [
                        ("Via", "SIP/2.0/TCP 192.0.2.99;branch=z9hG4bKproxy-ack"),
                        ("From", "<sip:test@192.0.2.10>;tag=remote"),
                        ("To", "<sip:Casa@192.0.2.20>;tag=local"),
                        ("Call-ID", "tcp-2xx-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                await endpoint._handle_datagram(ack, ("192.0.2.99", 5060))
                count_after_ack = len(sent)
                await asyncio.sleep(0.025)

                self.assertEqual(len(sent), count_after_ack)
                self.assertEqual(dialog.pending_ack_cseq, 0)
                self.assertIsNone(dialog.invite_2xx_task)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_invite_2xx_ack_timeout_sends_bye_and_terminates(self) -> None:
        sent: list[bytes] = []
        terminated = asyncio.Event()
        reasons: list[tuple[str, str]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_terminated(call_id: str, reason: str) -> None:
            reasons.append((call_id, reason))
            terminated.set()

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKtimeout"),
                    ("From", "<sip:test@192.0.2.10>;tag=remote"),
                    ("To", "<sip:Casa@192.0.2.20>"),
                    ("Contact", "<sip:test@192.0.2.10:5060>"),
                    ("Call-ID", "ack-timeout-call"),
                    ("CSeq", "1 INVITE"),
                ],
            )
        )
        dialog = sip_listener._ActiveDialog(
            request,
            ("192.0.2.10", 5060),
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )
        endpoint.active_dialogs["ack-timeout-call"] = dialog

        with (
            patch.object(sip_listener, "_SIP_T1", 0.001),
            patch.object(sip_listener, "_SIP_T2", 0.002),
            patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.006),
        ):
            endpoint._arm_invite_2xx(
                dialog,
                request,
                dialog.addr,
                200,
                "OK",
                "v=0\r\n",
            )
            await asyncio.wait_for(terminated.wait(), timeout=0.2)

        self.assertEqual(reasons, [("ack-timeout-call", "ack_timeout")])
        self.assertEqual(dialog.pending_ack_cseq, 0)
        self.assertNotIn("ack-timeout-call", endpoint.active_dialogs)
        self.assertIn("BYE", [sip.parse_message(raw).method for raw in sent])

    async def test_listener_coalesces_invite_retransmits_and_replays_final_response(self) -> None:
        sent: list[bytes] = []
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKdedupe"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "dedupe-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        addr = ("192.168.1.48", 5060)

        first = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        await endpoint._handle_datagram(invite, (addr[0], 5090))
        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [100, 100])

        release.set()
        await first
        await endpoint._handle_datagram(invite, addr)
        self.assertEqual(calls, 1)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 180)

        self.assertTrue(endpoint.send_final_response("dedupe-call", 200, "OK", answer_sdp=answer))
        await endpoint._handle_datagram(invite, addr)
        replay = sip.parse_message(sent[-1])
        self.assertEqual(replay.status_code, 200)
        rendered_answer = endpoint.active_dialogs["dedupe-call"].answer_sdp
        self.assertEqual(replay.body.decode(), rendered_answer)
        origin = next(
            line for line in rendered_answer.splitlines() if line.startswith("o=")
        ).split()
        self.assertNotEqual(origin[1], "0")
        self.assertEqual(origin[2], "0")
        self.assertEqual(calls, 1)

    async def test_listener_sends_sdp_in_deferred_183_early_media_response(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer(
            "192.168.1.48", "192.168.1.48", 40900, [fmt]
        ).encode()
        answer = sdp.build_answer_directional(
            "192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt
        )

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(
                183,
                "Session Progress",
                answer_sdp=answer,
                defer_final=True,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKearly"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "early-media-call"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 5060))

        progress = sip.parse_message(sent[-1])
        self.assertEqual(progress.status_code, 183)
        self.assertEqual(progress.header("Content-Type"), "application/sdp")
        self.assertIn(b"m=audio 40000", progress.body)
        self.assertIn("early-media-call", endpoint.pending_invites)
        self.assertNotIn("early-media-call", endpoint.active_dialogs)

    async def test_listener_response_preserves_complete_via_chain(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        options = sip.build_request(
            "OPTIONS",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.1:5060;branch=z9hG4bKproxy;rport"),
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "via-chain-call"),
                ("CSeq", "7 OPTIONS"),
            ],
        )

        await endpoint._handle_datagram(options, ("192.168.1.1", 5090))

        response = sip.parse_message(sent[-1])
        vias = response.header_values("Via")
        self.assertEqual(len(vias), 2)
        self.assertIn("branch=z9hG4bKproxy", vias[0])
        self.assertIn(";rport=5090", vias[0])
        self.assertNotIn(";received=", vias[0])
        self.assertEqual(vias[1], "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient")

    async def test_listener_response_adds_received_without_rport(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        options = sip.build_request(
            "OPTIONS",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 10.0.0.50:5060;branch=z9hG4bKnat"),
                ("From", "<sip:test@10.0.0.50>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "via-received-call"),
                ("CSeq", "7 OPTIONS"),
            ],
        )

        await endpoint._handle_datagram(options, ("198.51.100.50", 5090))

        via = sip.parse_message(sent[-1]).header("Via")
        self.assertIn("received=198.51.100.50", via)
        self.assertNotIn(";rport=", via)

    async def test_listener_replays_negative_invite_final_without_rerouting(self) -> None:
        sent: list[bytes] = []
        calls = 0
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            return sip_listener.SipInviteResult(486, "Busy Here", decline_reason="busy")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKbusy"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "busy-replay-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 5060))
        await endpoint._handle_datagram(invite, ("192.168.1.48", 5090))

        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [486, 486])
        self.assertIn("busy-replay-call", endpoint.completed_invites)
        self.assertNotIn("busy-replay-call", endpoint.pending_invites)
        endpoint.cancel_request_tasks()

    async def test_listener_retransmits_negative_invite_final_until_transaction_ack(self) -> None:
        sent: list[bytes] = []
        retransmitted = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40900, [fmt]).encode()

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            finals = [
                message
                for raw in sent
                if (message := sip.parse_message(raw)).status_code == 486
            ]
            if len(finals) >= 2:
                retransmitted.set()

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(486, "Busy Here", decline_reason="busy")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=capture,
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.0.2.20:5060",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bKbusy-final"),
                ("From", "<sip:test@192.0.2.10>;tag=remote"),
                ("To", "<sip:Casa@192.0.2.20>"),
                ("Call-ID", "busy-timer-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.002),
                patch.object(sip_listener, "_SIP_T2", 0.004),
                patch.object(sip_listener, "_INVITE_NON2XX_TIMEOUT", 0.1),
            ):
                await endpoint._handle_datagram(invite, ("192.0.2.10", 5060))
                await asyncio.wait_for(retransmitted.wait(), timeout=0.2)
                completed = endpoint.completed_invites["busy-timer-call"]
                self.assertGreaterEqual(completed.final_retransmissions, 1)
                self.assertEqual(endpoint.snapshot()["pending_invite_error_acks"], 1)

                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.0.2.20:5060",
                    [
                        (
                            "Via",
                            "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bKbusy-final",
                        ),
                        ("From", "<sip:test@192.0.2.10>;tag=remote"),
                        ("To", f"<sip:Casa@192.0.2.20>;tag={completed.to_tag}"),
                        ("Call-ID", "busy-timer-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                # Packet source may move between SBC nodes; transaction
                # identity is the top Via branch/sent-by plus CSeq.
                await endpoint._handle_datagram(ack, ("192.0.2.99", 5060))
                count_after_ack = len(sent)
                await asyncio.sleep(0.012)

                self.assertEqual(len(sent), count_after_ack)
                self.assertNotIn("busy-timer-call", endpoint.completed_invites)
                self.assertEqual(endpoint.snapshot()["pending_invite_error_acks"], 0)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_final_answer_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKfastanswer"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "fast-answer-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        task = asyncio.create_task(endpoint._handle_datagram(invite, ("192.168.1.48", 5060)))
        await started.wait()
        self.assertTrue(endpoint.send_final_response("fast-answer-call", 200, "OK", answer_sdp=answer))
        release.set()
        await task

        self.assertFalse(terminated)
        self.assertIn("fast-answer-call", endpoint.active_dialogs)

    async def test_listener_cancel_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        headers = [
            ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKcancelwait"),
            ("From", "<sip:test@192.168.1.48>;tag=remote"),
            ("To", "<sip:Casa@192.168.1.10>"),
            ("Call-ID", "cancel-wait-call"),
            ("CSeq", "9 INVITE"),
            ("Content-Type", "application/sdp"),
        ]
        invite = sip.build_request("INVITE", "sip:Casa@192.168.1.10:5060", headers, offer)
        addr = ("192.168.1.48", 5060)
        task = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        cancel_headers = [(key, "9 CANCEL" if key == "CSeq" else value) for key, value in headers if key != "Content-Type"]
        cancel = sip.build_request("CANCEL", "sip:Casa@192.168.1.10:5060", cancel_headers, b"")
        await endpoint._handle_datagram(cancel, (addr[0], 5090))
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("cancel-wait-call", endpoint.pending_invites)

        release.set()
        await task
        self.assertGreaterEqual(terminated.count(("cancel-wait-call", "cancelled")), 1)
        self.assertNotIn("cancel-wait-call", endpoint.active_dialogs)

    async def test_listener_keeps_cancel_and_bye_transaction_scopes_separate(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)

        def request(method: str, call_id: str, *, cseq: int = 2, branch: str = "z9hG4bKscope") -> bytes:
            return sip.build_request(
                method,
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48:5060;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        invite = sip.parse_message(request("INVITE", "pending-call"))
        active_invite = sip.parse_message(request("INVITE", "active-call"))
        endpoint.pending_invites["pending-call"] = sip_listener._PendingInvite(invite, addr, "local", "UDP")
        endpoint.active_dialogs["active-call"] = sip_listener._ActiveDialog(active_invite, addr, "local", 3, "UDP")

        await endpoint._handle_datagram(request("CANCEL", "active-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("active-call", endpoint.active_dialogs)

        await endpoint._handle_datagram(request("BYE", "pending-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call"), ("192.168.1.99", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", cseq=3), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", branch="z9hG4bKother"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        translated_addr = (addr[0], 5090)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("pending-call", endpoint.pending_invites)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)

        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertNotIn("active-call", endpoint.active_dialogs)
        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(terminated, [("pending-call", "cancelled"), ("active-call", "remote_hangup")])

    async def test_listener_accepts_in_dialog_reinvite_without_restarting_route(self) -> None:
        sent: list[bytes] = []
        applied_ports: list[int] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        original_offer = sdp.build_offer(
            "192.168.1.48", "192.168.1.48", 40000, [fmt]
        ).encode()
        negotiated = sdp.audio_format_to_rtp(fmt, 96)
        initial_answer = sdp.rewrite_sdp_origin(
            sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                42000,
                negotiated,
                negotiated,
                remote_sdp=original_offer,
            ),
            5151,
            0,
        )
        async def on_media_update(_previous, updated, _method):
            answer = sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                42000,
                updated.send_format,
                updated.recv_format,
                remote_sdp=updated.remote_sdp,
            )

            async def commit() -> None:
                applied_ports.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer, commit=commit)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "reinvite-call"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                original_offer,
            )
        )
        endpoint.active_dialogs["reinvite-call"] = sip_listener._ActiveDialog(
            original,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp=initial_answer,
            local_sdp_session_id=5151,
        )
        reinvite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKrefresh"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            original_offer,
        )
        await endpoint._handle_datagram(reinvite, addr)
        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"m=audio 42000", response.body)
        self.assertIn(b"o=- 5151 0 IN IP4 192.168.1.10", response.body)
        self.assertIn("reinvite-call", endpoint.active_dialogs)
        self.assertEqual(applied_ports, [40000])

        compatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKhold"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "3 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer_directional(
                "192.168.1.48",
                "192.168.1.48",
                45000,
                [fmt],
                [fmt],
                audio_direction="sendonly",
            ).encode(),
        )
        await endpoint._handle_datagram(compatible_media_change, addr)
        hold_response = sip.parse_message(sent[-1])
        self.assertEqual(hold_response.status_code, 200)
        self.assertIn(b"a=recvonly", hold_response.body)
        self.assertIn(b"o=- 5151 1 IN IP4 192.168.1.10", hold_response.body)
        self.assertEqual(
            endpoint.active_dialogs["reinvite-call"].request.body,
            original_offer,
        )
        self.assertEqual(endpoint.active_dialogs["reinvite-call"].invite.remote_rtp_port, 45000)
        self.assertEqual(applied_ports, [40000, 45000])

        incompatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKincompatible"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "4 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer(
                "192.168.1.48",
                "192.168.1.48",
                45002,
                [audio_format.AudioFormat(8000, "s16le", 1, 20)],
            ).encode(),
        )
        await endpoint._handle_datagram(incompatible_media_change, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertIn("reinvite-call", endpoint.active_dialogs)
        self.assertEqual(applied_ports, [40000, 45000])

    async def test_listener_bye_invalidates_pending_reinvite_before_media_commit(self) -> None:
        sent: list[bytes] = []
        commits: list[int] = []
        rollbacks: list[int] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(fmt, 96)
        original_offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]).encode()
        updated_offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 41000, [fmt]).encode()
        answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            42000,
            negotiated,
            negotiated,
        )

        async def on_media_update(_previous, updated, _method):
            started.set()
            await release.wait()

            async def commit() -> None:
                commits.append(updated.remote_rtp_port)

            async def rollback() -> None:
                rollbacks.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer,
                commit=commit,
                rollback=rollback,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=42000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.0.2.10", 5060)
        initial = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:b@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKinitial"),
                    ("From", "<sip:a@192.0.2.10>;tag=remote"),
                    ("To", "<sip:b@192.0.2.20>"),
                    ("Contact", "<sip:a@192.0.2.10>"),
                    ("Call-ID", "bye-during-reinvite"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                original_offer,
            )
        )
        endpoint.active_dialogs["bye-during-reinvite"] = sip_listener._ActiveDialog(
            initial,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp=answer,
            invite=endpoint._parse_invite(initial, addr),
        )
        reinvite = sip.build_request(
            "INVITE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKreinvite"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "bye-during-reinvite"),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            updated_offer,
        )
        bye = sip.build_request(
            "BYE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKbye"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "bye-during-reinvite"),
                ("CSeq", "3 BYE"),
            ],
        )

        reinvite_task = asyncio.create_task(endpoint._handle_datagram(reinvite, addr))
        await started.wait()
        await endpoint._handle_datagram(bye, addr)
        release.set()
        await reinvite_task

        responses = [sip.parse_message(raw) for raw in sent]
        self.assertEqual(
            [(item.status_code, item.header("CSeq")) for item in responses[-2:]],
            [(200, "3 BYE"), (487, "2 INVITE")],
        )
        self.assertEqual(commits, [])
        self.assertEqual(rollbacks, [41000])
        self.assertNotIn("bye-during-reinvite", endpoint.active_dialogs)

        await endpoint._handle_datagram(reinvite, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 487)
        self.assertEqual(commits, [])
        self.assertEqual(rollbacks, [41000])

    async def test_listener_update_requires_dialog_and_commits_once(self) -> None:
        sent: list[bytes] = []
        routed: list[str] = []
        commits: list[int] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]).encode()

        async def on_invite(invite):
            routed.append(invite.call_id)
            return sip_listener.SipInviteResult(488, "Not Acceptable Here")

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "UPDATE")
            answer = sdp.build_answer_directional(
                "192.0.2.20",
                "192.0.2.20",
                42000,
                updated.send_format,
                updated.recv_format,
                remote_sdp=updated.remote_sdp,
            )

            async def commit() -> None:
                commits.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer, commit=commit)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=42000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.0.2.10", 5060)

        def update(call_id: str, *, cseq: int, branch: str, body: bytes = offer) -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 192.0.2.10;branch={branch}"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} UPDATE"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request("UPDATE", "sip:b@192.0.2.20", headers, body)

        await endpoint._handle_datagram(update("unknown", cseq=2, branch="z9hG4bKunknown"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertEqual(routed, [])

        initial = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:b@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKinitial"),
                    ("From", "<sip:a@192.0.2.10>;tag=remote"),
                    ("To", "<sip:b@192.0.2.20>"),
                    ("Call-ID", "active"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                offer,
            )
        )
        endpoint.active_dialogs["active"] = sip_listener._ActiveDialog(
            initial,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )
        request = update("active", cseq=2, branch="z9hG4bKupdate")
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

        stale_bye = sip.build_request(
            "BYE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKstale-bye"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "active"),
                ("CSeq", "2 BYE"),
            ],
        )
        await endpoint._handle_datagram(stale_bye, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("active", endpoint.active_dialogs)

        refresh = update("active", cseq=3, branch="z9hG4bKrefresh", body=b"")
        await endpoint._handle_datagram(refresh, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

        # A delayed UDP duplicate remains part of its original transaction
        # even after a newer in-dialog request has completed.
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

    async def test_listener_rejects_in_dialog_video_transport_change(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def offer(video_port: int) -> bytes:
            return sdp.build_offer_directional(
                "192.168.1.48",
                "192.168.1.48",
                40000,
                [fmt],
                [fmt],
                video_port=video_port,
                video_format=sdp.DEFAULT_H264_FORMAT,
            ).encode()

        def request(body: bytes, *, cseq: int, branch: str) -> bytes:
            return sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", "video-reinvite-call"),
                    ("CSeq", f"{cseq} INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                body,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
            enable_video=True,
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            request(offer(41002), cseq=1, branch="z9hG4bKvideo-initial")
        )
        endpoint.active_dialogs["video-reinvite-call"] = sip_listener._ActiveDialog(
            original,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )

        changed = request(offer(41004), cseq=2, branch="z9hG4bKvideo-change")
        await endpoint._handle_datagram(changed, addr)

        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertEqual(
            endpoint.active_dialogs["video-reinvite-call"].request.body,
            original.body,
        )

    async def test_listener_delivers_in_dialog_sip_info_dtmf(self) -> None:
        sent: list[bytes] = []
        received: list[str] = []

        async def on_info(request, _addr, _transport) -> None:
            received.append(dtmf.parse_sip_info_digit(request.header("Content-Type"), request.body))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_info=on_info,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "info-call"),
                    ("CSeq", "1 INVITE"),
                ],
            )
        )
        endpoint.active_dialogs["info-call"] = sip_listener._ActiveDialog(original, addr, "local", 2, "UDP")
        info = sip.build_request(
            "INFO",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinfo"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "info-call"),
                ("CSeq", "2 INFO"),
                ("Content-Type", "application/dtmf-relay"),
            ],
            b"Signal=6\r\nDuration=160\r\n",
        )
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])

    def test_listener_bye_uses_contact_as_target_and_from_as_identity(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget"),
                    ("From", '"Desk" <sip:desk@192.168.1.48>;tag=remote'),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", '"Desk phone" <sip:dialog@192.168.1.48:5090;transport=udp>'),
                    ("Call-ID", "remote-target-call"),
                    ("CSeq", "4 INVITE"),
                ],
                b"",
            )
        )
        endpoint.active_dialogs["remote-target-call"] = sip_listener._ActiveDialog(
            request,
            ("192.168.1.48", 5060),
            "local",
            5,
            "UDP",
        )

        self.assertTrue(endpoint.send_bye("remote-target-call"))
        bye = sip.parse_message(sent[0])
        self.assertEqual(bye.uri, "sip:dialog@192.168.1.48:5090;transport=udp")
        self.assertEqual(bye.header("To"), "<sip:desk@192.168.1.48>;tag=remote")

    async def test_listener_echoes_and_uses_incoming_record_route_set(self) -> None:
        sent: list[tuple[bytes, tuple[str, int]]] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_invite(invite) -> sip_listener.SipInviteResult:
            answer = sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                41000,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.10",
            local_rtp_port=41000,
            supported_formats=[pcm],
            on_invite=on_invite,
            send_override=lambda data, addr: sent.append((data, addr)),
        )
        route_field = (
            "<sip:edge@192.0.2.11:5080;lr>, "
            '"Core, proxy" <sip:core@192.0.2.12:5070;lr>'
        )
        offer = sdp.build_offer(
            "192.0.2.20",
            "192.0.2.20",
            42000,
            [pcm],
        ).encode()
        invite = sip.build_request(
            "INVITE",
            "sip:HA@192.0.2.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.20:5060;branch=z9hG4bKrouted;rport"),
                ("From", "<sip:desk@192.0.2.20>;tag=remote"),
                ("To", "<sip:HA@192.0.2.10>"),
                ("Contact", "<sip:dialog@192.0.2.20:5090>"),
                ("Record-Route", route_field),
                ("Call-ID", "routed-listener-call"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.0.2.20", 5060))

        dialog = endpoint.active_dialogs["routed-listener-call"]
        self.assertEqual(
            dialog.route_set,
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                '"Core, proxy" <sip:core@192.0.2.12:5070;lr>',
            ),
        )
        ok = next(
            sip.parse_message(raw)
            for raw, _addr in sent
            if sip.parse_message(raw).status_code == 200
        )
        self.assertEqual(ok.header_values("Record-Route"), [route_field])

        self.assertTrue(endpoint.send_bye("routed-listener-call"))
        bye_raw, bye_addr = sent[-1]
        bye = sip.parse_message(bye_raw)
        self.assertEqual(bye.uri, "sip:dialog@192.0.2.20:5090")
        self.assertEqual(
            bye.header_values("Route"),
            [
                "<sip:edge@192.0.2.11:5080;lr>",
                '"Core, proxy" <sip:core@192.0.2.12:5070;lr>',
            ],
        )
        self.assertEqual(bye_addr, ("192.0.2.11", 5080))

    async def test_listener_offerless_update_refreshes_remote_target(self) -> None:
        sent: list[tuple[bytes, tuple[str, int]]] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, addr: sent.append((data, addr)),
        )
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget-old"),
                    ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", "<sip:desk@192.168.1.48:5060>"),
                    ("Call-ID", "target-refresh-listener"),
                    ("CSeq", "4 INVITE"),
                ],
            )
        )
        endpoint.active_dialogs["target-refresh-listener"] = sip_listener._ActiveDialog(
            original,
            ("192.168.1.48", 5060),
            "local",
            5,
            "UDP",
            remote_target_uri="sip:desk@192.168.1.48:5060",
        )
        update = sip.build_request(
            "UPDATE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget-new"),
                ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Contact", "<sip:desk@192.168.1.48:5090;transport=udp>"),
                ("Call-ID", "target-refresh-listener"),
                ("CSeq", "5 UPDATE"),
            ],
        )

        await endpoint._handle_datagram(update, ("192.168.1.48", 5060))
        self.assertEqual(sip.parse_message(sent[-1][0]).status_code, 200)
        self.assertTrue(endpoint.send_bye("target-refresh-listener"))
        bye, target = sent[-1]
        self.assertEqual(
            sip.parse_message(bye).uri,
            "sip:desk@192.168.1.48:5090;transport=udp",
        )
        self.assertEqual(target, ("192.168.1.48", 5090))

    async def test_listener_bounds_and_expires_deferred_invites(self) -> None:
        sent: list[sip.SipMessage] = []
        terminated: list[tuple[str, str]] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_invite(_invite) -> sip_listener.SipInviteResult:
            return sip_listener.SipInviteResult(
                180,
                "Ringing",
                defer_final=True,
            )

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[pcm],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(sip.parse_message(data)),
            max_pending_invites=1,
            deferred_invite_timeout=0.05,
        )
        body = sdp.build_offer(
            "192.168.1.48",
            "192.168.1.48",
            41000,
            [pcm],
        ).encode()

        def invite(call_id: str, cseq: int) -> bytes:
            return sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bK{call_id}"),
                    ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", "<sip:desk@192.168.1.48:5060>"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                body,
            )

        await endpoint._handle_datagram(
            invite("deferred-one", 1),
            ("192.168.1.48", 5060),
        )
        self.assertIn("deferred-one", endpoint.pending_invites)
        await endpoint._handle_datagram(
            invite("deferred-two", 2),
            ("192.168.1.48", 5060),
        )
        self.assertEqual(sent[-1].status_code, 503)
        self.assertEqual(sent[-1].header("Retry-After"), "1")

        await asyncio.sleep(0.08)
        self.assertNotIn("deferred-one", endpoint.pending_invites)
        self.assertEqual(sent[-1].status_code, 480)
        self.assertEqual(terminated, [("deferred-one", "no_answer")])
        endpoint.cancel_request_tasks()

    async def test_listener_rejects_invite_sdp_with_wrong_content_type(self) -> None:
        sent: list[sip.SipMessage] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[pcm],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(sip.parse_message(data)),
        )
        body = sdp.build_offer(
            "192.168.1.48",
            "192.168.1.48",
            41000,
            [pcm],
        ).encode()
        request = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKwrong-type"),
                ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Contact", "<sip:desk@192.168.1.48:5060>"),
                ("Call-ID", "wrong-content-type"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/octet-stream"),
            ],
            body,
        )

        await endpoint._handle_datagram(request, ("192.168.1.48", 5060))

        self.assertEqual(sent[-1].status_code, 415)
        self.assertEqual(sent[-1].header("Accept"), "application/sdp")

    async def test_call_client_offerless_update_refreshes_remote_target(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 32)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=pcm,
            recv_format=pcm,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        update = sip.parse_message(
            sip.build_request(
                "UPDATE",
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKrefresh"),
                    ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Contact", "<sip:ESP@127.0.0.2:5090;transport=udp>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "2 UPDATE"),
                ],
            )
        )

        self.assertIsNone(
            await client._handle_dialog_media_request(update, "127.0.0.2", 5060)
        )
        assert client.dialog is not None
        self.assertEqual(
            client.dialog.remote_target_uri,
            "sip:ESP@127.0.0.2:5090;transport=udp",
        )
        self.assertTrue(client.bye())
        bye, target = transport.sent[-1]
        self.assertEqual(
            sip.parse_message(bye).uri,
            "sip:ESP@127.0.0.2:5090;transport=udp",
        )
        self.assertEqual(target, ("127.0.0.2", 5090))

    def test_decline_reason_header_overrides_generic_status(self) -> None:
        msg = sip.SipMessage(
            status_code=486,
            reason="Busy Here",
            headers=(
                ("Reason", 'X-Voip-Stack;cause=486;text="DND"'),
                ("X-Voip-Stack-Decline-Reason", "DND"),
            ),
        )
        self.assertEqual(sip_client._sip_decline_reason(msg), "DND")

    def test_roster_target_matching_ignores_spaces_and_underscores(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Spotpear Ball v2",
                name="Spotpear Ball v2",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Spotpear_Ball_v2", entries, "sip:Spotpear_Ball_v2@192.168.1.10:5060")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertIsNotNone(decision.entry)
        assert decision.entry is not None
        self.assertEqual(decision.entry.address, "192.168.1.31")

    def test_esp_roster_entry_with_address_is_direct_even_without_transport_param(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060, "sip_transport": "tcp"},
            ),
            roster.RosterEntry(
                id="Cucina",
                name="Cucina",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Cucina", entries, "sip:Cucina@192.168.1.10:5060;transport=tcp")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.sip_uri, "sip:Cucina@192.168.1.31")
