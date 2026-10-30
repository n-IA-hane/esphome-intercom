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
