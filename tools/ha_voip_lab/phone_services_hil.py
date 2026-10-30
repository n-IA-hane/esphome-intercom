"""Owned HA/ESP phone-service fixtures, using existing SIP and native API tools.

This module is called by qualify_sip_registration_esp.py. It starts no server.
Account passwords stay in private files and never enter HA action arguments.
"""
from __future__ import annotations

import asyncio
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
    if event and event != "message-summary":
        return None
    waiting = re.search(r"(?im)^Messages-Waiting:\s*(yes|no)\s*$", body)
    return {"timestamp": float(timestamp), "source": source, "line": line,
        "call_id": headers.get("Call-ID", ""), "cseq": cseq,
        "event": event, "waiting": waiting[1].lower() if waiting else None}


class MwiTrace:
    """Use tcpdump and tshark for one private, narrowly filtered SIP window."""
    def __init__(self, args, private):
        self.args, self.private = args, private
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
        self.remote = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", self.args.ha_ssh,
            "sudo -n sh -c 'echo $$ >&2; exec timeout -s INT 300 tcpdump -n -i any -U -s 0 -w - \"udp port " + str(port) + " and host " + address + "\"'",
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
        while line := await self.decoder.stdout.readline():
            fields = line.decode().rstrip("\n").split("\t")
            if len(fields) == 3 and fields[2]:
                record = notification_record(fields[2], fields[1], fields[0])
                if record is not None:
                    self.records.append(record)
                    self.changed.set()

    async def waiting(self, value, *, since, timeout=20):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                for record in self.records:
                    if (record["timestamp"] < since or record["event"] != "message-summary" or
                            record["waiting"] != value or record["cseq"][1] != "NOTIFY"):
                        continue
                    if any(r["source"] == self.args.address and r["line"].startswith("SIP/2.0 200 ") and
                           r["call_id"] == record["call_id"] and r["cseq"] == record["cseq"] for r in self.records):
                        return {"waiting": value, "notify_accepted": True, "timestamp": record["timestamp"]}
                await self.changed.wait()

    async def __aexit__(self, *_exception):
        try:
            if hasattr(self, "remote"):
                if self.remote.returncode is None and hasattr(self, "pid"):
                    stop = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", self.args.ha_ssh,
                        "sudo", "-n", "kill", "-INT", str(self.pid), stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
                    await stop.wait()
                await asyncio.wait_for(self.remote.wait(), 10)
            await asyncio.gather(*(getattr(self, name) for name in ("copy_task", "decode_task") if hasattr(self, name)))
            if hasattr(self, "decoder"):
                await self.decoder.wait()
        finally:
            for name in ("capture", "stderr"):
                if hasattr(self, name):
                    getattr(self, name).close()
        if hasattr(self, "decoder") and self.decoder.returncode:
            raise RuntimeError("SIP capture decoder rejected the qualification trace")


def contact_page_offsets(count: int, *, row_height=48, viewport=96):
    """Capture all actual contact pages without guessing a clickable identity."""
    if row_height <= 0 or viewport <= 0:
        raise ValueError("Native row geometry must be positive")
    maximum = max(0, count * row_height - viewport)
    return sorted({*range(0, maximum + 1, viewport), maximum})


def parse_snapshot(message):
    match = re.search(r"contacts=(\d+) imap=(\S+) available=(YES|NO) total=(\d+) page=(\d+) uidv=(\d+) selected=(\d+) player=(\d+)", message)
    if not match:
        raise ValueError("Native phone snapshot is incomplete")
    values = match.groups()
    return dict(zip(("contacts", "imap", "available", "total", "page", "uidv", "selected", "player"),
                    (int(values[0]), values[1], values[2] == "YES", *(int(v) for v in values[3:]))))


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
    first = len(witness.log)
    await witness.service("phone_runtime_snapshot")
    await witness.wait(lambda: any("rtp: running=" in r["message"] for r in witness.log[first:]), timeout=5)
    return parse_rtp_sample(next(r["message"] for r in witness.log[first:] if "rtp: running=" in r["message"]))


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
    await witness.wait(lambda: any("contacts=" in r["message"] and "imap=" in r["message"] for r in witness.log[first:]), timeout=5)
    return parse_snapshot(next(r["message"] for r in witness.log[first:] if "contacts=" in r["message"] and "imap=" in r["message"]))


async def wait_snapshot(witness, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        value = await snapshot(witness)
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Native phone-service state did not reach its target")
        await asyncio.sleep(0.3)


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
 print(json.dumps({'uidv':validity,'uid':int(values[0]) if values else None,'total':int(total[0])}))
'''.replace("DEVICE", repr(args.mailbox_device)).replace("MESSAGE_ID", repr(message_id))
    process = await asyncio.create_subprocess_exec("ssh", "-o", "BatchMode=yes", args.ha_ssh,
        args.ha_python, "-", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    output, _errors = await process.communicate(program.encode())
    if process.returncode:
        raise RuntimeError("Standard IMAP identity lookup failed (details kept off public output)")
    return json.loads(output)


async def received_event(ws, mailbox_id, run_id, timeout=15):
    def wanted(packet):
        event = packet.get("event", {})
        data = event.get("data", {})
        return (event.get("event_type") == "voip_stack_voicemail_received" and
                data.get("mailbox_id") == mailbox_id and run_id in str(data.get("caller", "")))
    for event in ws.events:
        if wanted(event):
            return event["event"]["data"]
    async with asyncio.timeout(timeout):
        while True:
            packet = json.loads(await ws.ws.recv())
            if packet.get("type") == "event":
                ws.events.append(packet)
                if wanted(packet):
                    return packet["event"]["data"]


async def mailbox_scenario(witness, args, fixture, ws, result, sip_host, local_ip):
    async with MwiTrace(args, fixture.output) as trace:
        await _mailbox_scenario(witness, args, fixture, ws, result, sip_host, local_ip, trace)


async def _mailbox_scenario(witness, args, fixture, ws, result, sip_host, local_ip, trace):
    baseline = await fixture.phone(args.mailbox_device)
    initial = baseline["mailbox"]
    if initial["enabled"] or initial["new"] or initial["old"]:
        raise RuntimeError("This silent mailbox fixture requires an initially disabled, empty mailbox")
    if (await message_uid(args))["total"]:
        raise RuntimeError("IMAP contains existing messages; no mailbox settings changed")
    identity = None
    try:
        enabled_at = time.time()
        await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": True})
        await witness.service("refresh_voicemail")
        await wait_snapshot(witness, lambda s: s["available"] and s["total"] == 0)
        result["mwi_initial"] = await trace.waiting("no", since=enabled_at, timeout=65)
        await ws.command({"type": "subscribe_events", "event_type": "voip_stack_voicemail_received"})
        user = "codex_vm_hil_" + fixture.run_id
        deposited_at = time.time()
        process = await asyncio.create_subprocess_exec(sys.executable, str(args.intercom / "tools/sip_video_peer.py"),
            "--host", sip_host, "--port", str(args.ha_sip_port), "--target", "voicemail." + args.mailbox_username,
            "--user", user, "--display-name", "Codex VM HIL " + fixture.run_id,
            "--codec", "audio", "--audio-codec", "pcma", "--direction", "sendrecv", "--duration", "4",
            "--audio-file", str(args.voicemail_audio), "--local-ip", local_ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _stdout, _stderr = await asyncio.wait_for(process.communicate(), 25)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise RuntimeError("The existing SIP audio peer could not deposit the fixture")
        event = await received_event(ws, baseline["endpoint_id"], fixture.run_id)
        identity = await message_uid(args, event["message_id"])
        if identity["uid"] is None:
            raise RuntimeError("The deposited message is absent from its canonical IMAP mailbox")
        await witness.service("refresh_voicemail")
        await wait_snapshot(witness, lambda s: s["available"] and s["uidv"] == identity["uidv"] and s["total"] == 1)
        result["mwi_unread"] = await trace.waiting("yes", since=deposited_at)
        await witness.service("screenshot_show_idle")
        await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-idle-unread.jpg"))
        await witness.service("screenshot_show_voicemail")
        await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-voicemail.jpg"))
        await witness.service("screenshot_tap_phone_row", {"list": "voicemail", "slot": 0})
        await wait_snapshot(witness, lambda s: s["selected"] == identity["uid"] and s["player"] == 1)
        await wait_snapshot(witness, lambda s: s["player"] == 0)
        fields = {"uid": str(identity["uid"]), "uid_validity": str(identity["uidv"])}
        await witness.service("mark_voicemail_read", {**fields, "read": True})
        result["mwi_read"] = await trace.waiting("no", since=deposited_at)
        await witness.service("screenshot_show_idle")
        await witness.screenshot(args.address, args.output.with_name(args.output.stem + "-idle-read.jpg"))
        await witness.service("delete_voicemail", fields)
        await wait_snapshot(witness, lambda s: s["total"] == 0)
        result["mailbox"] = {"passed": True, "message_id": event["message_id"], **identity,
            "ui_row_played": True, "acoustic_qualification": False}
    finally:
        if identity is not None:
            await witness.service("delete_voicemail", {"uid": str(identity["uid"]), "uid_validity": str(identity["uidv"])})
            await witness.service("refresh_voicemail")
            await wait_snapshot(witness, lambda s: s["total"] == 0)
        state = await fixture.phone(args.mailbox_device)
        if state["mailbox"]["new"] or state["mailbox"]["old"] or (await message_uid(args))["total"]:
            raise RuntimeError("Mailbox left enabled to preserve unexpected messages; no bulk cleanup attempted")
        await fixture.action("set_voicemail_settings", {"device_id": args.mailbox_device, "enabled": False})
        await wait_snapshot(witness, lambda s: s["imap"] == "disabled")
        await witness.service("screenshot_close_voicemail")


async def run(witness, args, result, wire_class):
    from live_voip_qualification import HaRest, HaWs, _refresh_ha_token
    from ha_softphone_matrix import BareSip

    if not args.home_auth or not args.mailbox_device or not args.voicemail_audio or not args.mailbox_username:
        raise ValueError("phone-services requires --home-auth, --mailbox-device, --mailbox-username and --voicemail-audio")
    if not args.voicemail_audio.is_file() or any(c in str(args.voicemail_audio) for c in ",\r\n"):
        raise ValueError("Select an existing ordinary speech WAV with an unambiguous path")
    phases = set(args.phone_services_only or ("mailbox", "blf"))
    required = {"phone_runtime_snapshot", "take_screenshot"}
    if "mailbox" in phases:
        required.update({"refresh_voicemail", "screenshot_show_idle", "screenshot_show_voicemail", "screenshot_close_voicemail",
                         "screenshot_tap_phone_row", "mark_voicemail_read", "delete_voicemail"})
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
    private = args.output.with_suffix(".private")
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    fixture = HomeFixture(HaRest(auth["hass_url"], token), private, secrets.token_hex(6))
    peers, wires = [], []
    try:
        result["phone_services_stage"] = "home_websocket"
        async with HaWs(auth["hass_url"], token) as ws:
            if "mailbox" in phases:
                result["phone_services_stage"] = "mailbox"
                await mailbox_scenario(witness, args, fixture, ws, result, sip_host, local_ip)
            if phases & {"blf", "subscriptions"}:
                baseline_count = (await snapshot(witness))["contacts"]
                result["phone_services_stage"] = "fixture_accounts"
                accounts = await fixture.create_accounts()
                voice = private / "peer-source.wav"
                conversion = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-y", "-stream_loop", "-1",
                    "-i", str(args.voicemail_audio), "-t", "60", "-ar", "8000", "-ac", "1", str(voice))
                if await conversion.wait():
                    raise RuntimeError("Could not prepare the ordinary speech fixture")
                for index, account in enumerate(accounts):
                    directory = private / account["username"]
                    directory.mkdir(mode=0o700)
                    (directory / "config").write_text(f"""net_interface {local_ip}
sip_listen {local_ip}:0
call_max_calls 2
call_local_timeout 300
rtp_ports 22000-30000
audio_player aubridge,nil
audio_source aufile,{voice.resolve()}
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
                if current["imap"] != "disabled":
                    raise RuntimeError("The slot-based BLF fixture requires its mailbox link disabled")
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
