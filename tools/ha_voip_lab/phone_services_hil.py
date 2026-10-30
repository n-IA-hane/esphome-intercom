"""Owned HA/ESP phone-service fixtures, using existing SIP and native API tools.

This module is called by qualify_sip_registration_esp.py. It starts no server.
Account passwords stay in private files and never enter HA action arguments.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from email.parser import Parser
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import time
from urllib.parse import urlsplit


def baresip_audio_source_path(voice, cwd):
    """Fit baresip's 128-byte src_dev using its actual process directory."""
    value = os.path.relpath(voice.resolve(), cwd.resolve())
    # baresip v4.6.0 config_audio.src_dev includes its terminating NUL.
    if len(os.fsencode(value)) >= 128 or any(c in value for c in ",\r\n"):
        raise ValueError("The fixture WAV path exceeds baresip's audio_source field")
    return value


def notification_record(payload, source, timestamp):
    """Retain only the SIP fields needed to qualify MWI, never credentials."""
    text = bytes.fromhex(payload.replace(":", "")).decode("utf-8", errors="replace")
    head, separator, body = text.partition("\r\n\r\n")
    if not separator or "\r\n" not in head:
        return None
    line, fields = head.split("\r\n", 1)
    headers = Parser().parsestr(fields)
    cseq = headers.get("CSeq", "").split()
    if len(cseq) != 2 or cseq[1] not in {"SUBSCRIBE", "NOTIFY"}:
        return None
    event = headers.get("Event", "").partition(";")[0].strip().casefold()
    if event and event not in {"message-summary", "dialog", "presence"}:
        return None
    target = headers.get("To", "")
    address = re.search(r"<([^>]+)>", target)
    tail = target.partition(">")[2] if address else target
    tag = re.search(r"(?:^|;)\s*tag=([^;\s]+)", tail)
    waiting = re.search(r"(?im)^Messages-Waiting:\s*(yes|no)\s*$", body)
    return {"timestamp": float(timestamp), "source": source, "line": line,
        "call_id": headers.get("Call-ID", ""), "cseq": cseq,
        "event": event, "waiting": waiting[1].lower() if waiting else None,
        "to_uri": address[1] if address else target.partition(";")[0].strip(),
        "to_tag": tag[1] if tag else ""}


class MwiTrace:
    """Use tcpdump and tshark for one private, narrowly filtered SIP window."""
    def __init__(self, args, private, *, media=False):
        self.args, self.private = args, private
        self.media = media
        self.records, self.changed = [], asyncio.Event()

    async def __aenter__(self):
        try:
            return await self._start()
        except BaseException:
            try:
                await self.__aexit__()
            except BaseException:
                pass
            raise

    async def _start(self):
        address = str(ipaddress.IPv4Address(self.args.address))
        port = int(self.args.ha_sip_port)
        if not 1 <= port <= 65535:
            raise ValueError("The SIP capture port is invalid")
        self.path = self.private / "mwi-sip.pcap"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.capture = os.fdopen(fd, "wb")
        self.stderr = (self.private / "mwi-tshark.log").open("wb")
        capture_filter = (f"(udp or tcp port {port}) and host {address}" if self.media
                          else f"(udp or tcp) and port {port} and host {address}")
        self.remote = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", self.args.ha_ssh,
            "sudo -n sh -c 'echo $$ >&2; exec timeout -s INT 300 tcpdump --immediate-mode -n -i any -U -s 0 -w - \"" + capture_filter + "\"'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.pid = int((await asyncio.wait_for(self.remote.stderr.readline(), 8)).strip())
        self.decoder = await asyncio.create_subprocess_exec("tshark", "-l", "-n", "-r", "-", "-T", "fields",
            "-e", "frame.time_epoch", "-e", "ip.src", "-e", "udp.payload",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.stderr)
        self.copy_task = asyncio.create_task(self._copy())
        self.decode_task = asyncio.create_task(self._decode())
        async with asyncio.timeout(8):
            while True:
                line = await self.remote.stderr.readline()
                if not line:
                    raise RuntimeError("SIP capture did not start")
                if b"listening on" in line:
                    break
        return self

    async def _copy(self):
        while chunk := await self.remote.stdout.read(65536):
            self.capture.write(chunk)
            self.decoder.stdin.write(chunk)
            await self.decoder.stdin.drain()
        self.decoder.stdin.close()

    async def _decode(self):
        # Live MWI waits decode UDP only. TCP SIP is retained in the PCAP for
        # complete SDP/dialog evidence, independently of this projection.
        while line := await self.decoder.stdout.readline():
            fields = line.decode().rstrip("\n").split("\t")
            if len(fields) == 3 and fields[2]:
                record = notification_record(fields[2], fields[1], fields[0])
                if record is not None:
                    self.records.append(record)
                    self.changed.set()

    async def waiting(self, value, *, since, timeout=20, after=None):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                for record in self.records:
                    if (record["timestamp"] < since or record["event"] != "message-summary" or
                            record["waiting"] != value or record["cseq"][1] != "NOTIFY"):
                        continue
                    try:
                        sequence = int(record["cseq"][0])
                    except (ValueError, TypeError):
                        continue
                    if not 0 <= sequence < 2**31:
                        continue
                    if after is not None:
                        if (not after.get("to_uri") or record.get("to_uri") != after["to_uri"] or
                                record["timestamp"] < after["timestamp"]):
                            continue
                        if record["call_id"] == after["call_id"] and sequence <= after["cseq"]:
                            continue
                        if record["call_id"] != after["call_id"] and record["timestamp"] <= after["timestamp"]:
                            continue
                    if any(r["source"] == self.args.address and r["line"].startswith("SIP/2.0 200 ") and
                           r["call_id"] == record["call_id"] and r["cseq"] == record["cseq"] and
                           r["timestamp"] >= record["timestamp"] for r in self.records):
                        return {"waiting": value, "notify_accepted": True, "timestamp": record["timestamp"],
                            "call_id": record["call_id"], "cseq": sequence, "to_uri": record.get("to_uri", "")}
                await self.changed.wait()

    def recovered_subscriptions(self, since):
        requests = [record for record in self.records if record["timestamp"] >= since and
                    record["source"] == self.args.address and record["line"].startswith("SUBSCRIBE ")]
        expected = {(record["event"], record["to_uri"]) for record in requests if record.get("to_tag")}
        if not any(event == "message-summary" for event, _uri in expected) or not any(event == "dialog" for event, _uri in expected):
            return None
        recovered = set()
        for request in requests:
            key = (request["event"], request["to_uri"])
            if key not in expected:
                continue
            for notify in self.records:
                if (not notify["line"].startswith("NOTIFY ") or notify["event"] != request["event"] or
                        notify["call_id"] != request["call_id"] or notify["timestamp"] < request["timestamp"]):
                    continue
                if any(ack["source"] == self.args.address and ack["line"].startswith("SIP/2.0 200 ") and
                       ack["call_id"] == notify["call_id"] and ack["cseq"] == notify["cseq"] and
                       ack["timestamp"] >= notify["timestamp"] for ack in self.records):
                    recovered.add(key)
        if recovered != expected:
            return None
        return {"current_client_subscriptions": len(expected), "accepted_notifications": len(recovered),
                "packages": sorted({event for event, _uri in recovered})}

    async def __aexit__(self, exception_type=None, exception=None, traceback=None):
        failure = None
        self.cleanup_errors = []

        def failed(stage, error):
            nonlocal failure
            self.cleanup_errors.append({"stage": stage, "type": type(error).__name__})
            if failure is None:
                failure = error

        async def finish_process(process, stage, timeout):
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except BaseException as error:
                failed(stage, error)
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except BaseException as kill_error:
                        failed(stage + " kill", kill_error)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except BaseException as wait_error:
                    failed(stage + " reap", wait_error)

        try:
            if hasattr(self, "remote"):
                if self.remote.returncode is None and hasattr(self, "pid"):
                    try:
                        stop = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                            self.args.ha_ssh, "sudo", "-n", "kill", "-INT", str(self.pid),
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        await finish_process(stop, "capture stop", 8)
                    except BaseException as error:
                        failed("capture stop launch", error)
                await finish_process(self.remote, "capture wait", 10)
            tasks = [getattr(self, name) for name in ("copy_task", "decode_task") if hasattr(self, name)]
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks), 10)
                except BaseException as error:
                    failed("capture drain", error)
        finally:
            tasks = [getattr(self, name) for name in ("copy_task", "decode_task") if hasattr(self, name)]
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException as error:
                failed("capture tasks cleanup", error)
            if hasattr(self, "decoder"):
                if getattr(self.decoder, "stdin", None) is not None:
                    try:
                        self.decoder.stdin.close()
                    except BaseException as error:
                        failed("decoder input close", error)
                await finish_process(self.decoder, "decoder wait", 5)
                if self.decoder.returncode:
                    failed("decoder status", RuntimeError("SIP capture decoder rejected the qualification trace"))
            # A partially started capture may have no reader. Reaping a child
            # alone does not drain its PIPE transports; no reader owns them now.
            for name in ("remote", "decoder"):
                process = getattr(self, name, None)
                if process is not None and (getattr(process, "stdout", None) is not None or
                                            getattr(process, "stderr", None) is not None):
                    try:
                        await asyncio.wait_for(process.communicate(), 2)
                    except BaseException as error:
                        failed(name + " pipe cleanup", error)
            for name in ("capture", "stderr"):
                if hasattr(self, name):
                    try:
                        getattr(self, name).close()
                    except BaseException as error:
                        failed(name + " close", error)
        if failure is not None:
            if exception is None:
                raise failure
            exception.add_note("SIP capture cleanup: " + ", ".join(item["stage"] + " (" + item["type"] + ")"
                                                                  for item in self.cleanup_errors))
        return False


def contact_page_offsets(count: int, *, row_height=48, viewport=96):
    """Capture all actual contact pages without guessing a clickable identity."""
    if row_height <= 0 or viewport <= 0:
        raise ValueError("Native row geometry must be positive")
    maximum = max(0, count * row_height - viewport)
    return sorted({*range(0, maximum + 1, viewport), maximum})


def parse_snapshot(message):
    match = re.search(r"(?:^|\s)contacts=(\d+)(?:\s|$)", message)
    if match is None:
        raise ValueError("Missing native contact count")
    return {"contacts": int(match[1])}


def dialog_still_ringing(records, call_id):
    for record in records:
        if record["headers"].get("Call-ID") != call_id:
            continue
        if record["line"].startswith(("CANCEL ", "BYE ")):
            return False
        if (record["line"].startswith("SIP/2.0 ") and
                record["headers"].get("CSeq", "").endswith(" INVITE") and
                int(record["line"].split()[1]) >= 200):
            return False
    return True


def parse_rtp_sample(message):
    match = re.search(r"rtp: running=(YES|NO).*?tx_packets=(\d+) rx_packets=(\d+)", message)
    if match is None:
        raise ValueError("Native RTP counters are missing")
    return {"running": match[1] == "YES", "tx_packets": int(match[2]), "rx_packets": int(match[3])}


async def rtp_sample(witness):
    snapshot = await full_audio_snapshot(witness)
    return {key: snapshot["rtp"][key] for key in ("running", "tx_packets", "rx_packets")}


def _completed_audio_dump(records):
    marker = next((i for i, record in enumerate(records) if "phone.hil" in record["message"]
                   and re.search(r"\bcontacts=\d+\b", record["message"])), None)
    if marker is None:
        return None
    messages, uptime = [], None
    for record in records[marker + 1:]:
        message = record["message"]
        if "voip_stack.diagnostics" not in message:
            continue
        begin = re.search(r"BEGIN v=1 uptime_ms=(\d+)\b", message)
        if begin:
            if uptime is not None:
                raise ValueError("Native VoIP dump restarted before its matching END")
            uptime = int(begin[1])
        if uptime is None:
            continue
        messages.append(message)
        end = re.search(r"END v=1 uptime_ms=(\d+)\b", message)
        if end and int(end[1]) == uptime:
            return messages
    return None


def parse_full_audio_snapshot(messages):
    """Read the existing native diagnostic version, retaining its call boundary."""
    def one(pattern):
        matches = [match for line in messages if (match := re.search(pattern, line))]
        if len(matches) != 1:
            raise ValueError("Native audio snapshot has missing or repeated diagnostic fields")
        return matches[0]
    begin = one(r"BEGIN v=1 uptime_ms=(\d+) state=(\S+) call_hash=([0-9a-fA-F]{8}) transition=(YES|NO) failed=NO\b")
    end = one(r"END v=1 uptime_ms=(\d+)\b")
    if begin[1] != end[1]:
        raise ValueError("Native audio snapshot BEGIN and END belong to different dumps")
    rtp = one(r"rtp: running=(YES|NO).*?tx_packets=(\d+) rx_packets=(\d+) tx_bytes=(\d+) rx_bytes=(\d+)\b")
    queues = one(r"queues: tx_depth=(\d+) tx_drops=(\d+) rx_depth=(\d+) rx_drops=(\d+)\b")
    jitter = one(r"rx_jitter: depth=(\d+) capacity=(\d+) missing=(\d+) late=(\d+) duplicates=(\d+) drops=(\d+) discarded_frames=(\d+)\b")
    audio = one(r"audio: active=(YES|NO).*?route=([^\r\n]*)")
    return {"uptime_ms": int(begin[1]), "state": begin[2], "call_hash": begin[3].lower(),
        "transition": begin[4] == "YES", "audio_active": audio[1] == "YES", "route": audio[2],
        "rtp": {"running": rtp[1] == "YES", **dict(zip(
            ("tx_packets", "rx_packets", "tx_bytes", "rx_bytes"), map(int, rtp.groups()[1:]), strict=True))},
        "queues": dict(zip(("tx_depth", "tx_drops", "rx_depth", "rx_drops"), map(int, queues.groups()), strict=True)),
        "jitter": dict(zip(("depth", "capacity", "missing", "late", "duplicates", "drops", "discarded_frames"),
                           map(int, jitter.groups()), strict=True))}


async def full_audio_snapshot(witness):
    # Firmware emits a dump across several loop turns and admits at most one
    # per second. Drain and respect that existing boundary before requesting
    # another; otherwise an old idle RTP section can masquerade as this call.
    prior = [(i, record, match) for i, record in enumerate(witness.log)
             if "voip_stack.diagnostics" in record["message"] and
             (match := re.search(r"BEGIN v=1 uptime_ms=(\d+)\b", record["message"]))]
    if prior:
        index, record, begin = prior[-1]
        end = re.compile(r"END v=1 uptime_ms=" + begin[1] + r"\b")
        if not any(end.search(row["message"]) for row in witness.log[index + 1:]):
            await witness.wait(lambda: any(end.search(row["message"]) for row in witness.log[index + 1:]), timeout=5)
        if "time" in record:
            remaining = record["time"] + 1.01 - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
    first = len(witness.log)
    await witness.service("phone_runtime_snapshot")
    await witness.wait(lambda: _completed_audio_dump(witness.log[first:]) is not None, timeout=5)
    return parse_full_audio_snapshot(_completed_audio_dump(witness.log[first:]))


def check_audio_continuity(before, after):
    """Reject losses and counter resets within one uninterrupted physical call."""
    if (before["call_hash"] != after["call_hash"] or before["call_hash"] == "00000000" or
            any(sample["state"] != "in_call" or sample["transition"] or not sample["audio_active"] or
                not sample["rtp"]["running"] for sample in (before, after))):
        raise RuntimeError("Audio snapshots do not describe the same stable active call")
    if after["uptime_ms"] <= before["uptime_ms"]:
        raise RuntimeError("Audio snapshots are stale or device uptime restarted")
    counters = {"rtp": ("tx_packets", "rx_packets", "tx_bytes", "rx_bytes"),
                "queues": ("tx_drops", "rx_drops"),
                "jitter": ("missing", "late", "duplicates", "drops", "discarded_frames")}
    delta = {}
    for group, names in counters.items():
        delta[group] = {}
        for name in names:
            previous, current = before[group][name], after[group][name]
            if current < previous:
                raise RuntimeError(f"Native audio counter restarted: {group}.{name}")
            delta[group][name] = current - previous
    if any(value == 0 for value in delta["rtp"].values()):
        raise RuntimeError("Both native RTP directions and byte counters must advance")
    if (any(delta["queues"].values()) or
            any(delta["jitter"][name] for name in ("missing", "drops", "discarded_frames"))):
        raise RuntimeError("Native audio loss counters increased during DTMF")
    return delta


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
    path.chmod(0o600)


class HomeFixture:
    def __init__(self, rest, output, run_id):
        self.rest, self.output, self.run_id = rest, output, run_id
        self.owned = {}
        self.baseline = set()

    async def action(self, name, data=None, *, response=False):
        suffix = "?return_response" if response else ""
        value = await asyncio.to_thread(self.rest._request, "POST", "/api/services/voip_stack/" + name + suffix, data or {})
        return value.get("service_response", {}) if response else value

    async def phone(self, device):
        return await self.action("get_phone_state", {"device_id": device}, response=True)

    async def create_accounts(self):
        self.baseline = {a["username"] for a in (await self.action("list_accounts", response=True))["accounts"]}
        for role in ("a", "b"):
            username, name = f"codex_hil_{self.run_id}_{role}", f"HIL {role.upper()} {self.run_id}"
            if username in self.baseline:
                raise RuntimeError("Fixture account already exists")
            self.owned[username] = {"username": username, "display_name": name}
            value = await self.action("create_account", {"username": username, "display_name": name}, response=True)
            if not value.get("password_generated") or not value.get("password"):
                raise RuntimeError("HA did not return its generated fixture credential")
            self.owned[username].update(value)
            write_private(self.output / "accounts.json", list(self.owned.values()))
        return list(self.owned.values())

    async def cleanup_accounts(self):
        if not self.owned:
            return
        current = {a["username"]: a for a in (await self.action("list_accounts", response=True))["accounts"]}
        for username, expected in self.owned.items():
            if username not in current:
                continue
            if username in self.baseline or current[username]["display_name"] != expected["display_name"]:
                raise RuntimeError("Refusing to delete an account outside this fixture")
            await self.action("remove_account", {"username": username})


async def snapshot(witness):
    first = len(witness.log)
    await witness.service("phone_runtime_snapshot")
    await witness.wait(lambda: any("contacts=" in r["message"] for r in witness.log[first:]), timeout=5)
    return parse_snapshot(next(r["message"] for r in witness.log[first:] if "contacts=" in r["message"]))


async def wait_snapshot(witness, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        value = await snapshot(witness)
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Native phone-service state did not reach its target")
        await asyncio.sleep(0.3)


async def native_uptime(witness):
    first = len(witness.log)
    await witness.service("phone_runtime_snapshot")
    await witness.wait(lambda: any("BEGIN v=1 uptime_ms=" in row["message"] for row in witness.log[first:]), timeout=5)
    line = next(row["message"] for row in witness.log[first:] if "BEGIN v=1 uptime_ms=" in row["message"])
    return int(re.search(r"uptime_ms=(\d+)", line)[1])


async def backend_restart_scenario(witness, args, fixture, ws, result):
    """Replace the actual HA SIP owner while preserving the ESP and empty mailbox."""
    baseline = await fixture.phone(args.mailbox_device)
    if baseline["mailbox"]["enabled"] or (await message_uid(args))["total"]:
        raise RuntimeError("The backend restart fixture requires a disabled, empty mailbox")
    entries = (await ws.command({"type": "config_entries/get", "domain": "voip_stack"}))["result"]
    if len(entries) != 1:
        raise RuntimeError("The SIP backend owner is not unambiguous")
    identity = entries[0]["entry_id"]
    async def resources():
        data = await asyncio.to_thread(fixture.rest._request, "GET", "/api/diagnostics/config_entry/" + identity)
        return data["data"]["runtime"]["resources"]
    try:
        async with MwiTrace(args, fixture.output) as trace:
            enabled_at = time.time()
            await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": True})
            await trace.waiting("no", since=enabled_at, timeout=65)
            before = await resources()
            expected = before["sip_application"]["subscriptions"]
            if not before["call_scoped_quiescent"] or expected < 2:
                raise RuntimeError("The restart requires existing MWI and BLF subscriptions with no calls")
            uptime_before = await native_uptime(witness)
            result["backend_restart"] = {"passed": False, "entry_only": True,
                "observed_server_leases_before": expected, "uptime_before_ms": uptime_before}
            reloaded_at = time.time()
            response = await asyncio.to_thread(fixture.rest._request, "POST",
                "/api/config/config_entries/entry/" + identity + "/reload", {})
            if response.get("require_restart"):
                raise RuntimeError("The integration did not support an entry-only reload")
            renewed = await trace.waiting("no", since=reloaded_at, timeout=65)
            deadline = time.monotonic() + 30
            while True:
                after = await resources()
                devices = (await ws.command({"type": "voip_stack/list_devices"}))["result"]
                if isinstance(devices, dict):
                    devices = devices["devices"]
                device = next((item for item in devices if item.get("device_id") == args.mailbox_device), {})
                recovery = trace.recovered_subscriptions(reloaded_at)
                result["backend_restart"].update(registered=bool(device.get("registered")),
                    observed_server_leases_after=after["sip_application"]["subscriptions"], recovery=recovery)
                if device.get("registered") and recovery is not None:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("The new SIP owner did not recover registration and subscription leases")
                await asyncio.sleep(.3)
            uptime_after = await native_uptime(witness)
            if uptime_after <= uptime_before:
                raise RuntimeError("The ESP restarted during the backend-only recovery")
            result["backend_restart"] = {"passed": True, "entry_only": True,
                "observed_server_leases_before": expected, "observed_server_leases_after": after["sip_application"]["subscriptions"],
                "recovery": recovery,
                "registered": True, "mwi": renewed, "uptime_before_ms": uptime_before,
                "uptime_after_ms": uptime_after}
    finally:
        if (await message_uid(args))["total"]:
            raise RuntimeError("Unexpected voicemail preserved; the fixture did not disable its mailbox")
        await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": False})


async def capture_contact_pages(witness, args, label, count):
    images = []
    offsets = contact_page_offsets(count, row_height=args.contacts_row_height, viewport=args.contacts_viewport)
    requested = getattr(args, "contacts_capture_offset", None)
    if requested is not None:
        if not 0 <= requested <= offsets[-1]:
            raise ValueError("Requested contact capture is outside the actual scroll extent")
        offsets = [requested]
    for offset in offsets:
        await witness.service("screenshot_scroll_phone_list", {"list": "contacts", "offset": offset})
        path = args.output.with_name(args.output.stem + f"-{label}-{offset}.jpg")
        await witness.screenshot(args.address, path)
        images.append(str(path))
    return images


async def message_uid(args, message_id=None):
    """Use standard IMAP, with credentials read only by their HA owner host."""
    if message_id is not None and not re.fullmatch(r"[0-9a-f]{32}", message_id):
        raise ValueError("Unexpected canonical message identity")
    program = '''from pathlib import Path
import json,imaplib
entries=json.loads(Path('/var/lib/hass/.storage/core.config_entries').read_text())['data']['entries']
entry=next(e for e in entries if e['domain']=='voip_stack')
phone=entry['data']['managed_esphome_phones'][DEVICE]
settings=entry['data'].get('phone_services',{})
assert not settings.get('tls',False), 'Use the configured trust context for a TLS-only fixture'
with imaplib.IMAP4('127.0.0.1',int(settings.get('imap_port',1143)),timeout=8) as client:
 client.login(phone['username'],phone['password'])
 status,total=client.select('INBOX',readonly=True)
 assert status=='OK'
 validity=int(client.response('UIDVALIDITY')[1][0])
 identity=MESSAGE_ID
 values=[]
 if identity is not None:
  status,data=client.uid('search',None,'HEADER','Message-ID','"<'+identity+'@voip-stack.local>"')
  assert status=='OK'
  values=data[0].split()
 assert len(values)<=1
 print(json.dumps({'uidv':validity,'uid':int(values[0]) if values else None,'total':int(total[0]),'username':phone['username']}))
'''.replace("DEVICE", repr(args.mailbox_device)).replace("MESSAGE_ID", repr(message_id))
    process = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", args.ha_ssh,
        args.ha_python, "-", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, _errors = await process.communicate(program.encode())
    if process.returncode:
        raise RuntimeError("Standard IMAP identity lookup failed (details kept off public output)")
    return json.loads(output)


async def mailbox_event(ws, kind, predicate, timeout=25, *, since=0):
    """Observe the real HA archive operation, independently of the ESP sender."""
    def match(packet):
        event = packet.get("event", {})
        if since:
            try:
                timestamp = datetime.fromisoformat(event["time_fired"]).timestamp()
            except (KeyError, TypeError, ValueError):
                return False
            if timestamp < since:
                return False
        return event.get("event_type") == kind and predicate(event.get("data", {}))
    for packet in ws.events:
        if match(packet):
            return packet["event"]["data"]
    async with asyncio.timeout(timeout):
        while True:
            packet = json.loads(await ws.ws.recv())
            if packet.get("type") == "event":
                ws.events.append(packet)
                if match(packet):
                    return packet["event"]["data"]


async def mailbox_counts(fixture, device, new, old, timeout=35):
    async with asyncio.timeout(timeout):
        while True:
            state = await fixture.phone(device)
            if (state["mailbox"]["new"], state["mailbox"]["old"]) == (new, old):
                return state
            await asyncio.sleep(.25)


async def menu_wire_dtmf(path, address):
    """Decode the captured negotiated RTP events, independently of the ESP API."""
    address = str(ipaddress.IPv4Address(address))
    validate = await asyncio.create_subprocess_exec("capinfos", "-c", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await validate.communicate()
    if validate.returncode:
        raise RuntimeError("The mailbox RTP capture is malformed")
    process = await asyncio.create_subprocess_exec("tshark", "-n", "-r", str(path),
        "-Y", f"ip.src == {address} && rtpevent", "-T", "fields",
        "-e", "rtp.ssrc", "-e", "rtp.timestamp", "-e", "rtpevent.event_id",
        "-e", "rtpevent.end_of_event", "-e", "rtpevent.duration",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, _errors = await process.communicate()
    if process.returncode:
        raise RuntimeError("The mailbox RTP events could not be decoded")
    events = {}
    for line in output.decode().splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            raise RuntimeError("The mailbox RTP event fields are incomplete")
        source, timestamp, event, ended, duration = fields
        key = source, int(timestamp), int(event)
        record = events.setdefault(key, {"event": int(event), "packets": 0, "end_packets": 0})
        if not 0 < int(duration) <= 65535:
            raise RuntimeError("The mailbox RTP event has an invalid duration")
        if ended.casefold() not in {"true", "false", "1", "0"}:
            raise RuntimeError("The mailbox RTP End field is not a boolean")
        record["packets"] += 1
        record["end_packets"] += ended.casefold() in {"true", "1"}
    records = list(events.values())
    if [record["event"] for record in records] != [1, 3, 2, 0, 1, 11, 2, 11]:
        raise RuntimeError("The captured RFC4733 digits differ from the mailbox-menu actions")
    # The final # ends the SIP call, legitimately cancelling its remaining reports.
    if not all(record["end_packets"] for record in records[:-1]):
        raise RuntimeError("A completed in-call digit has no RTP End report")
    return {"rfc4733": True, "events": records, "final_digit_terminated_call": True}


async def mailbox_menu_scenario(witness, args, fixture, ws, result, sip_host, local_ip):
    """MWI, one ordinary SIP menu call and RFC4733 from the native keypad API."""
    baseline = await fixture.phone(args.mailbox_device)
    initial = baseline["mailbox"]
    identity = await message_uid(args)
    if initial["enabled"] or initial["new"] or initial["old"] or identity["total"]:
        raise RuntimeError("The SIP menu fixture requires a disabled, empty mailbox")
    owned, deleted = set(), set()
    failure = None
    for kind in ("voip_stack_voicemail_received", "voip_stack_voicemail_operation", "voip_stack_voicemail_changed"):
        await ws.command({"type": "subscribe_events", "event_type": kind})
    try:
        media_dir = fixture.output / "mailbox-menu"
        media_dir.mkdir(exist_ok=True, mode=0o700)
        async with MwiTrace(args, media_dir, media=True) as trace:
            enabled_at = time.time()
            await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": True})
            await trace.waiting("no", since=enabled_at, timeout=65)
            for index in range(2):
                caller = f"codex_vm_hil_{fixture.run_id}_{index}"
                process = await asyncio.create_subprocess_exec(sys.executable, str(args.intercom / "tools/sip_video_peer.py"),
                    "--host", sip_host, "--port", str(args.ha_sip_port),
                    "--target", "voicemail." + identity["username"], "--user", caller,
                    "--display-name", caller, "--codec", "audio", "--audio-codec", "pcma",
                    "--direction", "sendrecv", "--duration", "4", "--audio-file", str(args.speech_audio),
                    "--local-ip", local_ip, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    output, errors = await asyncio.wait_for(process.communicate(), 25)
                    for suffix, payload in (("json", output), ("log", errors)):
                        path = media_dir / f"deposit-{index}.{suffix}"
                        path.write_bytes(payload)
                        path.chmod(0o600)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                if process.returncode:
                    raise RuntimeError("The existing SIP peer could not deposit the owned message")
                event = await mailbox_event(ws, "voip_stack_voicemail_received", lambda d: (
                    d.get("mailbox_id") == baseline["endpoint_id"] and caller in str(d.get("caller", ""))))
                owned.add(event["message_id"])
            await mailbox_counts(fixture, args.mailbox_device, 2, 0)
            result["mwi_unread"] = await trace.waiting("yes", since=enabled_at)
            await witness.service("screenshot_show_idle")
            await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-idle-unread.jpg"))
            menu_started_at = time.time()
            await witness.service("start_call_on_line", {"dest": "*97", "line": "ha"})
            await witness.wait(lambda: witness.call_state == "in_call", timeout=20)
            await mailbox_counts(fixture, args.mailbox_device, 1, 1)
            await mailbox_event(ws, "voip_stack_voicemail_changed",
                lambda d: d.get("endpoint_id") == baseline["endpoint_id"], since=menu_started_at, timeout=35)
            audio_before = await full_audio_snapshot(witness)
            audio_window_started = time.time()
            async def press(digit):
                if witness.call_state != "in_call":
                    raise RuntimeError("The mailbox dialog ended before the keypad action")
                await witness.service("send_dtmf", {"digit": digit})
                # Pace actual key presses beyond the native 160 ms event duration.
                await asyncio.sleep(.25)
            next_started_at = time.time()
            await press("1")
            await mailbox_counts(fixture, args.mailbox_device, 0, 2)
            await mailbox_event(ws, "voip_stack_voicemail_changed",
                lambda d: d.get("endpoint_id") == baseline["endpoint_id"], since=next_started_at, timeout=35)
            result["mwi_read"] = await trace.waiting("no", since=menu_started_at, after=result["mwi_unread"])
            media_before = await rtp_sample(witness)
            replay_started_at = time.time()
            await press("3")
            await mailbox_event(ws, "voip_stack_voicemail_changed",
                lambda d: d.get("endpoint_id") == baseline["endpoint_id"], since=replay_started_at, timeout=35)
            media_after = await rtp_sample(witness)
            if (not media_after["running"] or media_after["rx_packets"] <= media_before["rx_packets"] or
                    media_after["tx_packets"] <= media_before["tx_packets"]):
                raise RuntimeError("Both native RTP directions must advance during mailbox replay")
            result["mailbox_replay"] = {"archive_play_completed": True,
                "rtp_before": media_before, "rtp_after": media_after}
            await witness.service("screenshot_show_keypad")
            await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-mailbox-call.jpg"))
            for selection in ("", "01#"):
                for digit in selection:
                    await press(digit)
                await press("2")
                event = await mailbox_event(ws, "voip_stack_voicemail_operation", lambda d: (
                    d.get("mailbox_id") == baseline["endpoint_id"] and d.get("operation") == "deleted" and
                    d.get("message_id") in owned - deleted))
                deleted.add(event["message_id"])
            audio_window_ended = time.time()
            audio_after = await full_audio_snapshot(witness)
            result["audio_during_dtmf"] = {"before": audio_before, "after": audio_after,
                "delta": check_audio_continuity(audio_before, audio_after),
                "window_epoch": [audio_window_started, audio_window_ended],
                "requires_pcm_wire_coverage": True,
                "untracked": ["cumulative hardware I2S underrun"]}
            await press("#")
            await witness.wait(lambda: witness.call_state == "idle", timeout=15)
            await mailbox_counts(fixture, args.mailbox_device, 0, 0)
            await witness.service("screenshot_show_idle")
            await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-idle-read.jpg"))
            result["mailbox_menu"] = {"passed": True, "owned_messages": sorted(owned),
                "deleted_messages": sorted(deleted), "native_dtmf": True,
                "transport_evidence": str(trace.path), "acoustic_qualification": False}
        result["mailbox_menu"]["wire_dtmf"] = await menu_wire_dtmf(trace.path, args.address)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            if witness.call_state not in {"", "idle"}:
                await witness.service("hangup_call")
                await witness.wait(lambda: witness.call_state == "idle", timeout=15)
            state = await fixture.phone(args.mailbox_device)
            if state["mailbox"]["new"] or state["mailbox"]["old"] or (await message_uid(args))["total"]:
                raise RuntimeError("Mailbox preserved for inspection; the fixture never bulk-deletes messages")
            await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": False})
        except BaseException as cleanup_error:
            result["mailbox_cleanup_failed"] = True
            if failure is None:
                raise
            failure.add_note(f"Mailbox fixture cleanup: {type(cleanup_error).__name__}: {cleanup_error}")


async def run(witness, args, result, wire_class):
    from live_voip_qualification import HaRest, HaWs, _refresh_ha_token
    from ha_softphone_matrix import BareSip, TEST_CAPTURE_DIR

    phases = set(args.phone_services_only or ("blf",))
    if not args.home_auth:
        raise ValueError("phone-services requires --home-auth")
    if phases & {"backend-restart", "mailbox-menu"} and not args.mailbox_device:
        raise ValueError("backend-restart and mailbox-menu require --mailbox-device")
    if phases & {"blf", "subscriptions", "mailbox-menu"} and (
            not args.speech_audio or not args.speech_audio.is_file() or
            any(c in str(args.speech_audio) for c in ",\r\n")):
        raise ValueError("Select an existing ordinary speech WAV with --speech-audio")
    required = {"phone_runtime_snapshot", "take_screenshot"}
    if "mailbox-menu" in phases:
        required.update({"send_dtmf", "screenshot_show_idle", "screenshot_show_keypad", "start_call_on_line", "hangup_call"})
    if phases & {"blf", "subscriptions"}:
        required.update({"refresh_ldap_directory", "screenshot_show_contacts", "start_call_on_line",
                         "screenshot_scroll_phone_list"})
    if not required <= witness.services.keys():
        raise RuntimeError("Running firmware lacks the required existing diagnostics")
    result["phone_services_stage"] = "home_authentication"
    auth = json.loads(args.home_auth.read_text())
    token = await asyncio.to_thread(_refresh_ha_token, args.home_auth)
    sip_host = args.ha_sip_host or urlsplit(auth["hass_url"]).hostname
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((sip_host, args.ha_sip_port))
        local_ip = probe.getsockname()[0]
    private = args.output.with_suffix(".private").resolve()
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    fixture = HomeFixture(HaRest(auth["hass_url"], token), private, secrets.token_hex(6))
    peers, wires = [], []
    try:
        result["phone_services_stage"] = "home_websocket"
        async with HaWs(auth["hass_url"], token) as ws:
            if "mailbox-menu" in phases:
                result["phone_services_stage"] = "sip_mailbox_menu"
                await mailbox_menu_scenario(witness, args, fixture, ws, result, sip_host, local_ip)
            if "backend-restart" in phases:
                result["phone_services_stage"] = "backend_restart"
                await backend_restart_scenario(witness, args, fixture, ws, result)
            if phases & {"blf", "subscriptions"}:
                baseline_count = (await snapshot(witness))["contacts"]
                result["phone_services_stage"] = "fixture_accounts"
                accounts = await fixture.create_accounts()
                voice = private / "peer-source.wav"
                conversion = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-y", "-stream_loop", "-1",
                    "-i", str(args.speech_audio), "-t", str(max(60, int(args.hold) + 15)), "-ar", "8000", "-ac", "1", str(voice))
                if await conversion.wait():
                    raise RuntimeError("Could not prepare the ordinary speech fixture")
                audio_source = baresip_audio_source_path(voice, TEST_CAPTURE_DIR)
                for index, account in enumerate(accounts):
                    directory = private / account["username"]
                    directory.mkdir(mode=0o700)
                    (directory / "config").write_text(f"""net_interface {local_ip}
sip_listen {local_ip}:0
call_max_calls 2
call_local_timeout 300
rtp_ports 22000-30000
audio_player aubridge,nil
audio_source aufile,{audio_source}
audio_alert aubridge,nil
module_path /usr/lib/baresip/modules
module stdio.so
module menu.so
module debug_cmd.so
module g711.so
module auconv.so
module auresamp.so
module aufile.so
module aubridge.so
module_app account.so
""")
                    credentials = f'<sip:{account["username"]}@{sip_host}:{args.ha_sip_port}>;auth_user={account["username"]};auth_pass={account["password"]};regint=60;answermode=manual\n'
                    fd = os.open(directory / "accounts", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        output.write(credentials)
                    result["phone_services_stage"] = f"peer_{index}_registration"
                    peer = await asyncio.to_thread(BareSip, directory)
                    peers.append(peer)
                    wires.append(wire_class(peer))
                    result["phone_services_stage"] = f"peer_{index}_sip_trace"
                    peer.command("/siptrace yes")
                    await wires[-1].wait_text("SIP trace is now enabled")
                result["phone_services_stage"] = "native_directory"
                await witness.service("refresh_ldap_directory")
                expected = accounts[1]["display_name"]
                current = await wait_snapshot(witness, lambda s: s["contacts"] == baseline_count + 2)
                if "blf" not in phases:
                    result["subscriptions"] = {"diagnostic_only": True, "baseline_contacts": baseline_count,
                        "fixture_contacts": current["contacts"], "calls_started": False}
                    return
                await witness.service("screenshot_show_contacts")
                result["phone_services_stage"] = "ringing_dialog"
                first = len(peers[1].output)
                caller_start = len(peers[0].output)
                peers[0].command(f'/dial sip:{accounts[1]["username"]}@{sip_host}:{args.ha_sip_port}')
                await wires[1].wait_text("Incoming call from:", first)
                incoming = await wires[1].wait_record(lambda r: r["line"].startswith("INVITE "), first)
                call_id = incoming["headers"]["Call-ID"]
                images = await capture_contact_pages(witness, args, "blf-ringing", current["contacts"])
                # The controller checks the real BLF projection before pickup.
                result["phone_services_stage"] = "ringing_visual_confirmation"
                print(json.dumps({"checkpoint": "inspect_blf_ringing", "expected_name": expected, "images": images}), flush=True)
                confirmation = await asyncio.to_thread(sys.stdin.readline)
                if confirmation.strip() != "CONFIRM " + expected:
                    raise RuntimeError("Fixture projection was not visually confirmed; no pickup sent")
                fresh = await snapshot(witness)
                if fresh["contacts"] != current["contacts"]:
                    raise RuntimeError("Contact population changed after visual confirmation")
                if not dialog_still_ringing(wires[1].records(first), call_id):
                    raise RuntimeError("The confirmed ringing dialog ended before pickup")
                source = await fixture.phone(accounts[0]["username"])
                eligible = await fixture.phone(accounts[1]["username"])
                if (accounts[1]["username"] not in fixture.owned or eligible["state"] != "ringing" or
                        not any(c.get("peer_endpoint_id") == source["endpoint_id"] for c in eligible["calls"])):
                    raise RuntimeError("The pickup resource is not this fixture's ringing call")
                result["eligible_pickup"] = {"endpoint_id": eligible["endpoint_id"], "calls": eligible["calls"]}
                result["phone_services_stage"] = "native_sip_pickup"
                target = f'sip:pickup.{accounts[1]["username"]}@{sip_host}:{args.ha_sip_port};transport=udp'
                await witness.service("start_call_on_line", {"dest": target, "line": "ha"})
                await witness.wait(lambda: witness.call_state == "in_call", timeout=15)
                await wires[0].wait_text("Call established", caller_start, timeout=15)
                result["phone_services_stage"] = "native_pickup_rtp"
                before_media = await rtp_sample(witness)
                if not before_media["running"]:
                    raise RuntimeError("The confirmed picked-up call has no running native RTP engine")
                await asyncio.sleep(args.hold)
                media = await rtp_sample(witness)
                if (not media["running"] or media["tx_packets"] <= before_media["tx_packets"] or
                        media["rx_packets"] <= before_media["rx_packets"]):
                    raise RuntimeError("The picked-up call did not advance both native RTP directions")
                result["native_pickup_rtp"] = {"before": before_media, "after": media, "window_seconds": args.hold}
                await asyncio.to_thread(peers[0].hangup)
                await witness.wait(lambda: witness.call_state == "idle", timeout=15)
                await witness.service("screenshot_show_contacts")
                result["phone_services_stage"] = "held_dialog"
                first = len(peers[1].output)
                caller_start = len(peers[0].output)
                peers[0].command(f'/dial sip:{accounts[1]["username"]}@{sip_host}:{args.ha_sip_port}')
                await wires[1].wait_text("Incoming call from:", first)
                peers[1].command("/accept")
                await wires[0].wait_text("Call established", caller_start)
                await wires[0].direction("/hold")
                images = await capture_contact_pages(witness, args, "blf-held", current["contacts"])
                result["phone_services_stage"] = "held_visual_confirmation"
                print(json.dumps({"checkpoint": "inspect_blf_held", "expected_name": accounts[0]["display_name"], "images": images}), flush=True)
                if (await asyncio.to_thread(sys.stdin.readline)).strip() != "CONFIRM " + accounts[0]["display_name"]:
                    raise RuntimeError("Held BLF rendering was not visually confirmed")
                await wires[0].direction("/resume")
                await asyncio.to_thread(peers[0].hangup)
                result["blf"] = {"passed": True, "native_sip_pickup": True, "native_row_tap": False,
                    "pointer_scope": "separate native LVGL host proof", "fixture_name": expected, "visual_confirmation": True}
    except BaseException:
        result["phone_services_failed_stage"] = result.get("phone_services_stage")
        raise
    finally:
        for wire in wires:
            wire.close()
        for index, peer in enumerate(peers):
            await asyncio.to_thread(peer.close)
            log = peer.output
            for account in fixture.owned.values():
                if account.get("password"):
                    log = log.replace(account["password"], "[redacted]")
            args.output.with_name(args.output.stem + f"-peer-{index}.log").write_text(log)
        await fixture.cleanup_accounts()
        if fixture.owned:
            await witness.service("refresh_ldap_directory")
            await wait_snapshot(witness, lambda s: s["contacts"] == baseline_count)
        for account in fixture.owned.values():
            directory = private / account["username"]
            if directory.exists():
                shutil.rmtree(directory)
        (private / "peer-source.wav").unlink(missing_ok=True)
        (private / "accounts.json").unlink(missing_ok=True)
