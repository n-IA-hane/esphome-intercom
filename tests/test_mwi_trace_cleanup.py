"""Capture teardown always releases local owners and preserves the body error."""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.ha_voip_lab.phone_services_hil import MwiTrace


class Process:
    def __init__(self, *, code=None, errors=(), on_wait=None):
        self.returncode = code
        self.errors = list(errors)
        self.on_wait = on_wait
        self.done = asyncio.Event()
        self.entered = asyncio.Event()
        self.stdin = io.BytesIO()
        self.waits = self.kills = 0
        if code is not None:
            self.done.set()

    def finish(self, code=0):
        self.returncode = code
        self.done.set()

    def kill(self):
        self.kills += 1
        self.finish(-9)

    async def wait(self):
        self.waits += 1
        self.entered.set()
        if self.errors:
            raise self.errors.pop(0)
        if self.on_wait:
            self.on_wait()
        await self.done.wait()
        return self.returncode


async def prepared_trace(monkeypatch, mode="normal"):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3", ha_ssh="fixture"), Path("/unused"))
    remote = trace.remote = Process(errors=[TimeoutError("remote wait")] if mode == "remote-timeout" else [])
    decoder = trace.decoder = Process(errors=[TimeoutError("decoder wait")] if mode in {"decoder-timeout", "copy-error"} else [])
    trace.pid = 12345
    trace.capture, trace.stderr = io.BytesIO(), io.BytesIO()
    stop = Process(code=0, on_wait=None if mode == "cancel" else remote.finish)
    spawn = AsyncMock(return_value=stop)
    monkeypatch.setattr("tools.ha_voip_lab.phone_services_hil.asyncio.create_subprocess_exec", spawn)

    async def copy():
        await remote.done.wait()
        if mode == "copy-error":
            raise OSError("copy failed")
        if mode != "decoder-timeout":
            decoder.finish()

    async def decode():
        if mode != "decoder-timeout":
            await decoder.done.wait()

    trace.copy_task = asyncio.create_task(copy())
    trace.decode_task = asyncio.create_task(decode())
    trace._start = AsyncMock(return_value=trace)
    return trace, spawn


def released(trace):
    assert trace.copy_task.done() and trace.decode_task.done()
    assert trace.remote.returncode is not None and trace.decoder.returncode is not None
    assert trace.capture.closed and trace.stderr.closed and trace.decoder.stdin.closed


async def test_normal_capture_drains_and_reaps_every_owner(monkeypatch):
    trace, spawn = await prepared_trace(monkeypatch)
    assert await trace.__aexit__() is False
    released(trace)
    assert not trace.cleanup_errors
    assert trace.remote.kills == trace.decoder.kills == 0
    assert spawn.await_count == 1


@pytest.mark.parametrize("media,expected", [
    (True, "(udp or tcp port 5070) and host 192.0.2.3"),
    (False, "(udp or tcp) and port 5070 and host 192.0.2.3"),
])
async def test_capture_requests_immediate_delivery_and_narrow_signaling(monkeypatch, tmp_path, media, expected):
    import ctypes as c
    import ctypes.util
    import shlex
    import socket
    import struct

    def reader(data=b""):
        stream = asyncio.StreamReader()
        stream.feed_data(data)
        stream.feed_eof()
        return stream
    remote = SimpleNamespace(stdout=reader(), stderr=reader(b"12345\nlistening on fixture\n"))
    decoder = SimpleNamespace(stdout=reader(), stdin=io.BytesIO())
    spawn = AsyncMock(side_effect=[remote, decoder])
    monkeypatch.setattr("tools.ha_voip_lab.phone_services_hil.asyncio.create_subprocess_exec", spawn)
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3", ha_sip_port=5070, ha_ssh="fixture"), tmp_path, media=media)
    try:
        await trace._start()
        await asyncio.gather(trace.copy_task, trace.decode_task)
        command = shlex.split(spawn.await_args_list[0].args[-1])[-1]
        arguments = shlex.split(command)
        assert arguments[arguments.index("tcpdump") + 1] == "--immediate-mode"
        assert arguments[-1] == expected
        assert "6053" not in command and "http" not in command
        # Execute the actual compiled filter against independent packets.
        # libpcap's offline evaluator requires no capture privileges.
        class Program(c.Structure):
            _fields_ = [("length", c.c_uint), ("instructions", c.c_void_p)]
        class Timeval(c.Structure):
            _fields_ = [("seconds", c.c_long), ("microseconds", c.c_long)]
        class Header(c.Structure):
            _fields_ = [("time", Timeval), ("captured", c.c_uint32), ("length", c.c_uint32)]
        library = ctypes.util.find_library("pcap")
        assert library, "The packet-tools dependency must provide libpcap"
        pcap = c.CDLL(library)
        pcap.pcap_open_dead.argtypes, pcap.pcap_open_dead.restype = [c.c_int, c.c_int], c.c_void_p
        pcap.pcap_compile.argtypes = [c.c_void_p, c.POINTER(Program), c.c_char_p, c.c_int, c.c_uint32]
        pcap.pcap_offline_filter.argtypes = [c.POINTER(Program), c.POINTER(Header), c.POINTER(c.c_ubyte)]
        pcap.pcap_offline_filter.restype = c.c_uint
        pcap.pcap_freecode.argtypes = [c.POINTER(Program)]
        pcap.pcap_close.argtypes = [c.c_void_p]
        handle, program = pcap.pcap_open_dead(1, 65535), Program()
        assert handle
        try:
            assert pcap.pcap_compile(handle, c.byref(program), arguments[-1].encode(), 1, 0xffffffff) == 0
            cases = [
                (17, "192.0.2.3", "192.0.2.1", 40000, 40002, media),
                (17, "192.0.2.3", "192.0.2.1", 5060, 5070, True),
                (17, "192.0.2.1", "192.0.2.3", 5070, 5060, True),
                (6, "192.0.2.3", "192.0.2.1", 37000, 5070, True),
                (6, "192.0.2.1", "192.0.2.3", 5070, 37000, True),
                (6, "192.0.2.3", "192.0.2.1", 80, 37000, False),
                (6, "192.0.2.1", "192.0.2.3", 37000, 6053, False),
                (6, "192.0.2.3", "192.0.2.1", 37000, 5060, False),
                (17, "192.0.2.4", "192.0.2.1", 5070, 5070, False),
            ]
            for protocol, source, target, source_port, target_port, accepted in cases:
                transport = (struct.pack("!HHHH", source_port, target_port, 8, 0) if protocol == 17
                             else struct.pack("!HHIIHHHH", source_port, target_port, 0, 0, 5 << 12, 0, 0, 0))
                ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(transport), 0, 0, 64, protocol, 0,
                                 socket.inet_aton(source), socket.inet_aton(target))
                packet = bytes.fromhex("0200000000010200000000020800") + ip + transport
                data = c.create_string_buffer(packet)
                header = Header(Timeval(0, 0), len(packet), len(packet))
                actual = pcap.pcap_offline_filter(c.byref(program), c.byref(header), c.cast(data, c.POINTER(c.c_ubyte)))
                assert bool(actual) is accepted, (protocol, source, target, source_port, target_port)
        finally:
            pcap.pcap_freecode(c.byref(program))
            pcap.pcap_close(handle)
    finally:
        trace.capture.close()
        trace.stderr.close()


@pytest.mark.parametrize("address,port", [("192.0.2.3; true", 5060), ("192.0.2.3", 0),
                                         ("192.0.2.3", 65536), ("192.0.2.3", "5060; true")])
async def test_capture_rejects_invalid_host_and_port_before_launch(monkeypatch, tmp_path, address, port):
    spawn = AsyncMock()
    monkeypatch.setattr("tools.ha_voip_lab.phone_services_hil.asyncio.create_subprocess_exec", spawn)
    trace = MwiTrace(SimpleNamespace(address=address, ha_sip_port=port), tmp_path)
    with pytest.raises(ValueError):
        await trace._start()
    spawn.assert_not_awaited()


@pytest.mark.parametrize("mode,error", [("remote-timeout", TimeoutError),
                                       ("decoder-timeout", TimeoutError), ("copy-error", OSError)])
async def test_capture_faults_cleanup_before_propagating_the_original_error(monkeypatch, mode, error):
    trace, _ = await prepared_trace(monkeypatch, mode)
    with pytest.raises(error):
        await trace.__aexit__()
    released(trace)
    assert trace.cleanup_errors


@pytest.mark.parametrize("original", [ValueError("body failed"), asyncio.CancelledError("body cancelled")])
async def test_capture_cleanup_cannot_replace_the_body_exception(monkeypatch, original):
    trace, _ = await prepared_trace(monkeypatch, "remote-timeout")
    with pytest.raises(type(original)) as caught:
        async with trace:
            raise original
    assert caught.value is original
    assert any("SIP capture cleanup" in note for note in original.__notes__)
    released(trace)


async def test_cancellation_during_exit_reaps_subprocesses_and_tasks(monkeypatch):
    trace, _ = await prepared_trace(monkeypatch, "cancel")
    cleanup = asyncio.create_task(trace.__aexit__())
    await trace.remote.entered.wait()
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    released(trace)
    assert trace.remote.kills == 1


async def test_partial_capture_start_without_pid_or_decoder_still_cleans_up(monkeypatch):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3", ha_ssh="fixture"), Path("/unused"))
    trace.remote = Process(errors=[TimeoutError("partial startup")])
    trace.capture, trace.stderr = io.BytesIO(), io.BytesIO()
    spawn = AsyncMock()
    monkeypatch.setattr("tools.ha_voip_lab.phone_services_hil.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await trace.__aexit__()
    assert trace.remote.returncode == -9
    assert trace.capture.closed and trace.stderr.closed
    spawn.assert_not_awaited()


async def test_partial_start_drains_real_subprocess_pipes_after_kill(monkeypatch):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3", ha_ssh="fixture"), Path("/unused"))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import os,time;os.write(1,b'x'*1048576);time.sleep(60)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    trace.remote = process
    trace.capture, trace.stderr = io.BytesIO(), io.BytesIO()
    original_wait_for = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        return await original_wait_for(awaitable, min(timeout, .05))

    monkeypatch.setattr("tools.ha_voip_lab.phone_services_hil.asyncio.wait_for", bounded_wait)
    try:
        with pytest.raises(TimeoutError):
            await trace.__aexit__()
        assert process.returncode is not None
        assert process.stdout.at_eof() and process.stderr.at_eof()
        assert trace.capture.closed and trace.stderr.closed
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


def record(*, call_id="current", sequence="4", time=20, waiting="no", source="192.0.2.1", to_uri="sip:phone@example.test"):
    return {"timestamp": time, "source": source, "line": "NOTIFY sip:phone SIP/2.0",
            "call_id": call_id, "cseq": [sequence, "NOTIFY"], "event": "message-summary",
            "waiting": waiting, "to_uri": to_uri, "to_tag": "tag"}


def accepted_ack(notification, timestamp):
    return {**notification, "timestamp": timestamp, "source": "192.0.2.3",
            "line": "SIP/2.0 200 OK", "waiting": None, "event": ""}


async def test_mwi_requires_ack_after_the_observed_notify(tmp_path):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3"), tmp_path)
    notify = record()
    trace.records.extend([notify, accepted_ack(notify, 19)])
    with pytest.raises(TimeoutError):
        await trace.waiting("no", since=18, timeout=.001)
    trace.records.append(accepted_ack(notify, 20))
    assert (await trace.waiting("no", since=18))["cseq"] == 4


@pytest.mark.parametrize("call_id,sequence,timestamp,target,accepted", [
    ("current", "1", 21, "sip:phone@example.test", False),
    ("current", "4", 21, "sip:phone@example.test", False),
    ("current", "5", 21, "sip:phone@example.test", True),
    ("new", "1", 21, "sip:phone@example.test", True),
    ("new", "1", 20, "sip:phone@example.test", False),
    ("new", "1", 21, "sip:other@example.test", False),
])
async def test_mwi_read_follows_unread_even_when_old_no_is_retransmitted(tmp_path, call_id, sequence, timestamp, target, accepted):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3"), tmp_path)
    unread = record(waiting="yes")
    trace.records.extend([unread, accepted_ack(unread, 20)])
    after = await trace.waiting("yes", since=19)
    notify = record(call_id=call_id, sequence=sequence, time=timestamp, to_uri=target)
    trace.records.extend([notify, accepted_ack(notify, timestamp)])
    if accepted:
        assert (await trace.waiting("no", since=20, timeout=.01, after=after))["call_id"] == call_id
    else:
        with pytest.raises(TimeoutError):
            await trace.waiting("no", since=20, timeout=.001, after=after)


async def test_recovery_cannot_use_ack_preceding_the_notify(tmp_path):
    trace = MwiTrace(SimpleNamespace(address="192.0.2.3"), tmp_path)
    for package in ("message-summary", "dialog"):
        request = {**record(call_id=package, time=10), "line": "SUBSCRIBE sip:server SIP/2.0",
                   "source": "192.0.2.3", "event": package, "cseq": ["2", "SUBSCRIBE"]}
        notify = {**record(call_id=package, time=12), "event": package}
        trace.records.extend([request, notify, accepted_ack(notify, 11)])
    assert trace.recovered_subscriptions(9) is None
    for notification in list(trace.records):
        if notification["line"].startswith("NOTIFY "):
            trace.records.append(accepted_ack(notification, 12))
    assert trace.recovered_subscriptions(9)["accepted_notifications"] == 2
