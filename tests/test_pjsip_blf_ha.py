"""Independent native PJSIP consumes the isolated HA notifier over UDP/TCP.

Build the optional native dependency with tools/build_pjsip_blf.py. Set
VOIP_REQUIRE_PJSIP_BLF=1 for a qualification gate that fails if it is absent.
This qualifies SIP interoperability, not a graphical phone's lamp rendering.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.call_projection import publish_bridge_projection
from custom_components.voip_stack.sip_registrar import SipAccount
from tools.build_pjsip_blf import DEFAULT_CACHE, REVISION
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = [pytest.mark.ha, pytest.mark.integration, pytest.mark.timeout(60)]
NS = {"d": "urn:ietf:params:xml:ns:dialog-info"}


@pytest.fixture(scope="module")
def pjsip_blf_binary():
    binary = Path(os.environ.get("VOIP_TEST_PJSIP_BLF_CLIENT", DEFAULT_CACHE / "voip-stack-blf-client"))
    if not binary.is_file() or shutil.which("baresip") is None:
        reason = "Build native BLF peer: python tools/build_pjsip_blf.py; baresip is also required"
        if os.environ.get("VOIP_REQUIRE_PJSIP_BLF") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    manifest = json.loads(binary.with_name("voip-stack-blf-build.json").read_text())
    source = Path(__file__).parents[1] / "tools/pjsip_blf_client.c"
    assert manifest["revision"] == REVISION
    assert manifest.get("driver_sanitizers") == ["address", "undefined"], (
        "Native BLF peer requires sanitizer coverage; rerun python tools/build_pjsip_blf.py")
    assert manifest["client_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(), (
        "Native BLF peer is stale; rerun python tools/build_pjsip_blf.py")
    return binary


class NativeBlfPeer:
    """Only drive the native client's stdin and observe its parsed wire events."""

    def __init__(self, process, output):
        self.process = process
        self.output = output
        self.events = []
        self.changed = asyncio.Condition()
        self.failure = None
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                self.output.write(line)
                self.output.flush()
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AssertionError(f"Native BLF peer emitted invalid JSON: {line!r}") from exc
                if event["event"] == "notify":
                    root = ElementTree.fromstring(event["xml"])
                    assert root.tag == "{" + NS["d"] + "}dialog-info"
                    assert root.attrib["state"] == "full"
                    event["root"] = root
                    event["dialogs"] = root.findall("d:dialog", NS)
                    # PJSIP's own parser must agree for the first dialog.
                    if event["dialogs"]:
                        first = event["dialogs"][0]
                        assert event["parsed_count"] == 1
                        assert event["parsed_state"] == first.find("d:state", NS).text
                        assert event["parsed_call_id"] == first.get("call-id", "")
                async with self.changed:
                    self.events.append(event)
                    self.changed.notify_all()
        except Exception as exc:
            self.failure = exc
        finally:
            async with self.changed:
                self.changed.notify_all()

    async def command(self, value):
        assert self.process.returncode is None
        self.process.stdin.write((value + "\n").encode())
        await self.process.stdin.drain()

    async def wait(self, predicate, *, after=0, timeout=5):
        async with asyncio.timeout(timeout):
            async with self.changed:
                while True:
                    if self.failure is not None:
                        raise self.failure
                    for event in self.events[after:]:
                        if predicate(event):
                            return event
                    assert not self.reader.done(), f"Native PJSIP peer exited: {self.events!r}"
                    await self.changed.wait()

    async def notify(self, predicate=lambda event: True, *, after=0, timeout=5):
        return await self.wait(lambda event: event["event"] == "notify" and predicate(event),
                               after=after, timeout=timeout)


@asynccontextmanager
async def native_peer(lab, binary, tmp_path, transport, *, endpoint="sip:bedroom0"):
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(),
                               SipAccount("blfwatch", "Independent BLF watcher", "isolated-test-only")])
    lab.runtime.endpoints.update(endpoint, extension="101")
    methods = lab.calls.component("sip_application")
    with (tmp_path / f"pjsip-{transport}.stderr").open("wb") as errors, \
         (tmp_path / f"pjsip-{transport}.jsonl").open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            str(binary), "127.0.0.1", str(lab.runtime.transport_config["sip_port"]), transport,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors, cwd=tmp_path)
        peer = NativeBlfPeer(process, output)
        try:
            registered = await peer.wait(lambda event: event["event"] == "registration")
            assert registered["code"] == 200 and registered["status"] == 0
            yield peer
        finally:
            try:
                if methods.subscriptions and process.returncode is None:
                    start = len(peer.events)
                    await peer.command("subscribe 0")
                    await peer.notify(lambda event: "terminated" in event["subscription_state"], after=start)
                if process.returncode is None:
                    start = len(peer.events)
                    await peer.command("unregister")
                    result = await peer.wait(lambda event: event["event"] == "registration"
                                             and event["unregister"], after=start)
                    assert result["code"] == 200
                    await peer.command("quit")
                    await asyncio.wait_for(process.wait(), 5)
                assert process.returncode == 0
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                await peer.reader
            assert peer.failure is None
            assert not methods.subscriptions
            async with asyncio.timeout(3):
                while methods.tasks or methods.notifiers:
                    await asyncio.sleep(0.01)
            assert not methods.subscription_timers
            assert methods.dialog_unsubscribe is None
            assert methods.endpoint_unsubscribe is None
            assert "blfwatch" not in registrar.registrations


def has_state(event, value):
    return any(dialog.find("d:state", NS).text == value for dialog in event["dialogs"])


def is_held(event):
    return any(param.get("pname") == "+sip.rendering" and param.get("pval") == "no"
               for dialog in event["dialogs"] for param in dialog.findall("d:local/d:target/d:param", NS))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("transport", ["UDP", "TCP"])
async def test_pjsip_blf_real_calls_ring_answer_hold_consultation_hangup(
        registered_phones, trunks, pjsip_blf_binary, tmp_path, transport):
    """Real baresip call states are consumed by a different SIP implementation."""
    lab = trunks
    caller, operator, destination = registered_phones
    async with native_peer(lab, pjsip_blf_binary, tmp_path, transport, endpoint="transfer_b") as peer:
        await peer.command("subscribe 300")
        initial = await peer.notify()
        await peer.wait(lambda event: event["event"] == "response" and event["code"] == 200
                         and event["method"] == "SUBSCRIBE")
        assert initial["dialogs"] == [] and initial["root"].get("version") == "0"
        await asyncio.to_thread(caller.dial, "transfer_b", wait_for="180 Ringing")
        ringing = await peer.notify(lambda event: has_state(event, "early"))
        await asyncio.to_thread(operator.command, "/accept")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        answered = await peer.notify(lambda event: has_state(event, "confirmed"))
        start = len(peer.events)
        await asyncio.to_thread(operator.command, "/hold")
        held = await peer.notify(is_held, after=start)
        assert has_state(held, "confirmed")
        start = len(peer.events)
        await asyncio.to_thread(operator.command, "/resume")
        resumed = await peer.notify(lambda event: has_state(event, "confirmed") and not is_held(event), after=start)
        start = len(peer.events)
        target = f'sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(operator.command, f"/atransferstart {target}")
        concurrent = await peer.notify(lambda event: len(event["dialogs"]) == 2
                                        and all(dialog.find("d:state", NS).text == "confirmed"
                                                for dialog in event["dialogs"]), after=start)
        assert len({dialog.get("id") for dialog in concurrent["dialogs"]}) == 2
        assert is_held(concurrent)
        start = len(peer.events)
        await asyncio.to_thread(destination.hangup)
        remaining = await peer.notify(lambda event: len(event["dialogs"]) == 1, after=start)
        start = len(peer.events)
        await asyncio.to_thread(caller.hangup)
        idle = await peer.notify(lambda event: not event["dialogs"], after=start)
        assert all(event["response"] == 200 for event in (initial, ringing, answered, held, resumed,
                                                         concurrent, remaining, idle))
        notifications = [event for event in peer.events if event["event"] == "notify"]
        assert [int(event["root"].get("version")) for event in notifications] == list(range(len(notifications)))
        sequences = [event["cseq"] for event in notifications]
        assert sequences == sorted(set(sequences))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transport", ["UDP", "TCP"])
@pytest.mark.parametrize("ending", ["unsubscribe", "expiry", "notify_481"])
async def test_pjsip_blf_refresh_terminal_notify_and_cleanup(
        trunks, pjsip_blf_binary, tmp_path, transport, ending):
    lab = trunks
    methods = lab.calls.component("sip_application")
    async with native_peer(lab, pjsip_blf_binary, tmp_path, transport) as peer:
        await peer.command("subscribe 30")
        initial = await peer.notify()
        original = next(iter(methods.subscriptions.values()))
        start = len(peer.events)
        expires = 1 if ending == "expiry" else 30
        await peer.command(f"subscribe {expires}")
        refreshed = await peer.notify(after=start)
        assert refreshed["root"].get("version") == "1"
        assert next(iter(methods.subscriptions.values())) is original
        assert refreshed["cseq"] > initial["cseq"]
        if ending == "notify_481":
            start = len(peer.events)
            await peer.command("reject481")
            await peer.wait(lambda event: event["event"] == "reject_ready", after=start)
            session = lab.calls.upsert("native-blf-rejected", state="ringing", owner="router",
                                       dest_endpoint_id="sip:bedroom0")
            lab.calls.claim_endpoint(session.call_id, "sip:bedroom0", role="destination")
            publish_bridge_projection(lab.hass, session)
            rejected = await peer.notify(lambda event: event["response"] == 481, after=start)
            assert has_state(rejected, "early")
            async with asyncio.timeout(3):
                while methods.subscriptions:
                    await asyncio.sleep(0.01)
            await lab.calls.terminate_call_wait(session.call_id, reason="test_done")
        else:
            start = len(peer.events)
            if ending == "unsubscribe":
                await peer.command("subscribe 0")
            terminal = await peer.notify(lambda event: "terminated" in event["subscription_state"],
                                          after=start, timeout=3)
            if ending == "expiry":
                assert "reason=timeout" in terminal["subscription_state"]
            assert terminal["root"].get("version") == "2"
            assert terminal["response"] == 200
            await peer.wait(lambda event: event["event"] == "subscription"
                             and event["state"] == "TERMINATED", after=start)
        assert not methods.subscriptions
