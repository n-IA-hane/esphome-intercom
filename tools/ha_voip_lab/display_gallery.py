"""Capture a real ESP display using the existing phone-service HIL owners.

Requires the private demo/scroll diagnostics already used for the Spotpear
gallery. This tool does not build firmware, add an API, or publish images.
Directory names, types and numbers are examples; BLF and mailbox state come
from real SIP peers and fresh native getters. JPEG semantic review stays open
until the resulting native captures have been inspected.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from tools import ha_softphone_matrix as peers
from tools.ha_voip_lab import phone_services_hil as hil
from tools.live_voip_qualification import HaRest, HaWs, _refresh_ha_token


ROOT = Path(__file__).resolve().parents[2]
DEMO_IDS = tuple("Gallery" + name for name in
                 ("Garage", "Home", "Office", "Mobile", "Assistant", "Guest", "Ring", "Conference"))
BLF_FIELDS = ("state", "presence", "version", "ringing", "active", "held")
BLF_RECORD = re.compile(
    r"demo_blf id=(\S+) available=(YES|NO) state=(\d+) presence=(\d+) "
    r"version=(\d+) ringing=(\d+) active=(\d+) held=(\d+)"
)
REQUIRED_ACTIONS = {
    "take_screenshot", "registration_runtime_diagnostics", "phone_runtime_snapshot",
    "screenshot_demo_contacts", "screenshot_scroll_phone_list", "screenshot_show_contacts",
    "screenshot_show_options", "screenshot_show_keypad", "screenshot_show_idle",
    "refresh_ldap_directory", "hangup_call", "send_dtmf", "start_call_on_line",
}


def decode_native_status(messages):
    rows, waiting = {}, None
    for message in messages:
        match = BLF_RECORD.search(message)
        if match:
            rows[match[1]] = {"available": match[2] == "YES",
                             **dict(zip(BLF_FIELDS, map(int, match.groups()[2:])))}
        end = re.search(r"demo_end waiting=(YES|NO) contacts=(\d+)", message)
        if end:
            waiting = end[1] == "YES"
    if set(rows) != set(DEMO_IDS) or waiting is None:
        raise RuntimeError("Native gallery snapshot is incomplete")
    return {"timestamp": time.time(), "blf": rows, "waiting": waiting}


def checked_profile_receipt(config, receipt):
    value = json.loads(receipt.read_text())
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if digest != value["config_sha256"]:
        raise RuntimeError("Private profile differs from its compiled receipt")
    for artifact in value["files"].values():
        if hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() != artifact["sha256"]:
            raise RuntimeError("Preserved firmware artifact differs from its receipt")
    return value


def compiled_transition_seconds(receipt):
    """Read the navigation contract from the source matching this artifact."""
    source = Path(receipt["private_debug_source"]) / "src/main.cpp"
    if hashlib.sha256(source.read_bytes()).hexdigest() != receipt["main_sha256"]:
        raise RuntimeError("Preserved UI source differs from the compiled receipt")
    values = [int(value) for value in re.findall(
        r"^\s*nav_anim_ms->value\(\)\s*=\s*(\d+);", source.read_text(), re.MULTILINE) if int(value)]
    if not values:
        raise RuntimeError("The compiled image has no identified round navigation duration")
    return max(values) / 1000


def compiled_call_ended_seconds(receipt):
    """Use the actual compiled terminal-page hold, not a capture timeout."""
    source = Path(receipt["private_debug_source"]) / "src/main.cpp"
    if hashlib.sha256(source.read_bytes()).hexdigest() != receipt["main_sha256"]:
        raise RuntimeError("Preserved UI source differs from the compiled receipt")
    text = source.read_text()
    start = re.search(r"new\((\w+)\)\s+Automation<[^;]+>\(ui_call_ended\);", text)
    if start is None:
        raise RuntimeError("The compiled image has no identified call-ended script")
    end = re.search(re.escape(start[1]) + r"->add_actions\(", text[start.end():])
    if end is None:
        raise RuntimeError("The compiled call-ended script has no complete action list")
    body = text[start.end():start.end() + end.start()]
    delays = [int(value) for value in re.findall(
        r"\w+->set_delay\([^{}]+\{\s*return\s+(\d+);\s*\}\);", body)]
    if not delays:
        raise RuntimeError("The compiled call-ended script has no identified duration")
    return sum(delays) / 1000


def load_esp_tool(source):
    spec = spec_from_file_location("display_gallery_esp", source / "tools/qualify_sip_registration_esp.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gallery_witness(esp):
    class Witness(esp.EspWitness):
        def __init__(self, client, args, server):
            super().__init__(client)
            self.args, self.server = args, server
            self.native_samples, self.captures = [], []
            self.expected_page = None
            self._diagnostic_lock = asyncio.Lock()
            self._diagnostic_completed_at = 0.0

        def on_log(self, entry):
            super().on_log(entry)
            value = entry.message.decode(errors="replace") if isinstance(entry.message, bytes) else str(entry.message)
            if "ui.hil" in value:
                self.log.append({"time": time.monotonic(), "message": value})
                self.changed.set()

        async def service(self, name, data=None):
            if name not in {"phone_runtime_snapshot", "registration_runtime_diagnostics"}:
                return await super().service(name, data)
            # Both private actions call the same native diagnostics owner.
            # dump_diagnostics() refuses concurrent requests and requests less
            # than 1000 ms apart. Observe its END before releasing this boundary.
            async with self._diagnostic_lock:
                remaining = self._diagnostic_completed_at + 1.01 - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                first = len(self.log)
                try:
                    await super().service(name, data)
                    await self.wait(lambda: hil._completed_diagnostic_dump(self.log[first:]) is not None, timeout=5)
                finally:
                    self._diagnostic_completed_at = time.monotonic()

        async def native_status(self):
            first = len(self.log)
            await self.service("phone_runtime_snapshot")
            await self.wait(lambda: any("demo_end waiting=" in r["message"] for r in self.log[first:]), timeout=5)
            sample = decode_native_status(r["message"] for r in self.log[first:])
            self.native_samples.append(sample)
            return sample

        async def wait_native(self, predicate, timeout=12):
            deadline = time.monotonic() + timeout
            while True:
                value = await self.native_status()
                if predicate(value):
                    return value
                if time.monotonic() >= deadline:
                    raise TimeoutError("Fresh native gallery state did not reach its target")
                await asyncio.sleep(.3)

        async def ui_checkpoint(self):
            first = len(self.log)
            await self.service("registration_runtime_diagnostics")
            await self.wait(lambda: any("ui.hil" in r["message"] and "page=" in r["message"]
                                       for r in self.log[first:]), timeout=5)
            message = next(r["message"] for r in self.log[first:] if "ui.hil" in r["message"] and "page=" in r["message"])
            fields = dict(re.findall(r"(\w+)=([^\s]+)", message))
            known_page = fields.get("page") not in {None, "unknown"}
            if ((not known_page and self.expected_page != "settings") or
                    (known_page and fields["page"] != self.expected_page) or
                    fields.get("avatar_visible") != "NO"):
                raise RuntimeError("Gallery UI checkpoint differs from the requested safe phone page")
            fields["page_identity_available"] = known_page
            return fields

        async def screenshot(self, address, output):
            # Reuse the existing mailbox scenario while keeping its two idle
            # captures on the phone's contact page, away from private avatars.
            if "-idle-unread" in output.name or "-idle-read" in output.name:
                if "-idle-read" in output.name:
                    # The terminal page intentionally owns the UI after idle.
                    await asyncio.sleep(self.args.ui_call_ended_seconds)
                await self.service("screenshot_demo_contacts", {"server": self.server})
                await self.service("screenshot_show_contacts")
                self.expected_page = "call_idle"
            elif "-mailbox-call" in output.name:
                self.expected_page = "call_dialer"
            if self.expected_page not in {"call_idle", "call_dialer", "settings"}:
                raise RuntimeError("A safe expected page is required for a gallery capture")
            # lv_screen_load_anim() queues scr_to_load before its start
            # callback updates the active screen. prev_scr can still be NULL
            # in that interval. Honor the compiled navigation contract before
            # asking the first checkpoint to identify the requested page.
            await asyncio.sleep(self.args.ui_transition_seconds)
            before = await self.ui_checkpoint()
            if "transition" in before:
                deadline = time.monotonic() + 5
                while before["transition"] not in {"0", "NO"}:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("The native screen load has not completed")
                    before = await self.ui_checkpoint()
                before["native_screen_load_observed"] = True
            else:
                # Older private images expose the selected logical page only.
                # Require a second fresh checkpoint after the compiled wait.
                # Pixel semantic review remains mandatory.
                before = await self.ui_checkpoint()
                before["native_screen_load_observed"] = False
                before["compiled_transition_ms"] = round(self.args.ui_transition_seconds * 1000)
            result = await super().screenshot(address, output, expected_size=(self.args.width, self.args.height))
            after = await self.ui_checkpoint()
            result.update(page=self.expected_page, native_before=before, native_after=after,
                          semantic_review="pending", physical_touch_qualified=False)
            self.captures.append(result)
            hil.write_private(self.args.output / "capture-provenance.json", self.captures)
            return result

    return Witness


async def demo(witness):
    await witness.service("screenshot_demo_contacts", {"server": witness.server})
    await hil.wait_snapshot(witness, lambda value: value["contacts"] == 8)


async def state_ready(witness, target, state):
    expected = {"idle": 1, "ringing": 2, "active": 3, "held": 4}[state]
    return await witness.wait_native(lambda sample: sample["blf"][target]["available"] and
        sample["blf"][target]["state"] == expected and
        (state != "idle" or sample["blf"][target]["presence"] == 1))


async def create_accounts(fixture):
    fixture.baseline = {row["username"] for row in (await fixture.action("list_accounts", response=True))["accounts"]}
    names = ("Garage", "Home", "Office", "Guest")
    if any(("Gallery" + name).casefold() in {value.casefold() for value in fixture.baseline} for name in names):
        raise RuntimeError("Gallery refuses to replace an existing account")
    for name in names:
        username = "Gallery" + name
        row = {"username": username, "display_name": "Demo " + name + " " + fixture.run_id}
        fixture.owned[username] = dict(row)
        # Keep ownership before the request, including the case where HA
        # creates the account but its HTTP response is interrupted.
        hil.write_private(fixture.output / "fixture-accounts.json", {
            "run_id": fixture.run_id, "baseline": sorted(fixture.baseline), "owned": list(fixture.owned.values())})
        value = await fixture.action("create_account", row, response=True)
        if not value.get("password_generated") or not value.get("password"):
            raise RuntimeError("The PBX did not return the owned generated credential")
        fixture.owned[username].update(value)
        hil.write_private(fixture.output / "fixture-accounts.json", {
            "run_id": fixture.run_id, "baseline": sorted(fixture.baseline), "owned": list(fixture.owned.values())})
    return list(fixture.owned.values())


def start_peer(account, output, server, port, local_ip):
    directory = output / account["username"]
    directory.mkdir(mode=0o700)
    (directory / "config").write_text(f"""net_interface {local_ip}
sip_listen {local_ip}:0
call_max_calls 1
call_local_timeout 120
rtp_ports 25000-35000
audio_player aubridge,nil
audio_source aufile,peer-source.wav
audio_alert aubridge,nil
module_path {os.environ.get('VOIP_TEST_BARESIP_MODULE_PATH', '/usr/lib/baresip/modules')}
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
    fd = os.open(directory / "accounts", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f'<sip:{account["username"]}@{server}:{port}>;auth_user={account["username"]};'
                     f'auth_pass={account["password"]};regint=60;answermode=manual;audio_codecs=PCMU/8000/1;ptime=20\n')
    return peers.BareSip(directory)


async def mailbox_args(args, fixture, mac):
    async with HaWs(fixture.rest.base_url, fixture.rest.token) as ws:
        devices = (await ws.command({"type": "config/device_registry/list"}))["result"]
        entities = (await ws.command({"type": "config/entity_registry/list"}))["result"]
    # A MAC can also identify another integration's device. The native entity
    # owner, as used by ESPHome provisioning, selects the existing phone.
    owners = {row["device_id"] for row in entities if row.get("platform") == "esphome"
              and row.get("disabled_by") is None and row.get("device_id")}
    matches = [row for row in devices if row["id"] in owners and any(kind == "mac" and value.upper() == mac.upper()
               for kind, value in row.get("connections", []))]
    if len(matches) != 1:
        raise RuntimeError("The physical ESP must identify exactly one existing HA device")
    values = SimpleNamespace(**{**vars(args), "mailbox_device": matches[0]["id"], "intercom": ROOT,
                                "output": args.output / "mailbox-scene.json"})
    initial = (await fixture.phone(values.mailbox_device))["mailbox"]
    identity = await hil.message_uid(values)
    if initial["enabled"] or initial["new"] or initial["old"] or identity["total"]:
        raise RuntimeError("Gallery preserves an existing enabled or nonempty mailbox")
    return values


async def capture_mailbox(witness, args, fixture, server, local_ip):
    # The stock live decoder supports UDP only. Keep its TCP PCAP and use the
    # actual native aggregate getter for readiness, without claiming SIP ACK.
    original = hil.MwiTrace.waiting

    async def native_wait(trace, value, *, since, timeout=20, after=None):
        sample = await witness.wait_native(lambda row: row["waiting"] == (value == "yes") and
                                          row["timestamp"] >= since, timeout)
        return {"waiting": value, "timestamp": sample["timestamp"], "fresh_native_getter": True,
                "source": "VoipStack.has_waiting_messages", "sip_capture": str(trace.path)}

    hil.MwiTrace.waiting = native_wait
    result = {}
    try:
        async with HaWs(fixture.rest.base_url, fixture.rest.token) as ws:
            await hil.mailbox_menu_scenario(witness, args, fixture, ws, result, server, local_ip)
    finally:
        hil.MwiTrace.waiting = original
        hil.write_private(args.output.with_name(args.output.stem + "-evidence.json"), result)
    return result


async def run(args):
    from aioesphomeapi import APIClient

    receipt = checked_profile_receipt(args.config, args.receipt)
    args.ui_transition_seconds = compiled_transition_seconds(receipt)
    args.ui_call_ended_seconds = compiled_call_ended_seconds(receipt)
    if args.output.exists():
        raise RuntimeError("Select a fresh private output directory; existing evidence is preserved")
    args.output.mkdir(parents=True, mode=0o700)
    auth = json.loads(args.home_auth.read_text())
    token = await asyncio.to_thread(_refresh_ha_token, args.home_auth)
    rest = HaRest(auth["hass_url"], token)
    server = args.ha_sip_host or urlsplit(auth["hass_url"]).hostname
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((server, args.ha_sip_port))
        local_ip = probe.getsockname()[0]
    esp = load_esp_tool(args.voip_source)
    witness = gallery_witness(esp)(APIClient(**esp.api_parameters(args.config, args.address, args.expected_mac)), args, server)
    fixture = hil.HomeFixture(rest, args.output, "gallery-" + secrets.token_hex(4))
    actors, wires, preferences, baseline = [], [], {}, None
    surface_key, baseline_surface = None, None
    peer_capture_root = peers.TEST_CAPTURE_DIR
    result = {"passed": False, "semantic_review": "pending", "captures": witness.captures,
              "synthetic": "Eight demo directory names, types and numbers; SIP states are real",
              "firmware_identity": receipt["firmware_identity"], "physical_touch_qualified": False}
    try:
        await witness.open()
        info = await witness.client.device_info()
        if info.mac_address.upper() != args.expected_mac.upper() or info.compilation_time != receipt["firmware_identity"]["build_time"]:
            raise RuntimeError("Running ESP differs from the selected compiled firmware receipt")
        if not REQUIRED_ACTIONS <= witness.services.keys():
            raise RuntimeError("Running firmware lacks the existing gallery diagnostics")
        await witness.wait(lambda: witness.call_state != "")
        if witness.call_state != "idle":
            raise RuntimeError("Gallery requires an idle ESP")
        keys = {(type(e).__name__.removesuffix("Info") + "State", e.key): e.name for e in witness.entities
                if type(e).__name__ in {"NumberInfo", "SwitchInfo", "SelectInfo"}}
        await witness.wait(lambda: all(key in witness.values for key in keys))
        preferences = {key: witness.values[key] for key in keys}
        master = next(key for key, name in keys.items() if name == "Master Volume")
        if preferences[master] != 0:
            raise RuntimeError("Gallery requires Master Volume already zero; it never changes it")
        original_directory = await hil.snapshot(witness)
        surface = next(e for e in witness.entities if type(e).__name__ == "TextSensorInfo" and
                       (e.object_id == "voip_contacts" or e.name == "VoIP Contacts"))
        surface_key = ("TextSensorState", surface.key)
        await witness.wait(lambda: surface_key in witness.values)
        baseline_surface = witness.values[surface_key]
        menu_args = await mailbox_args(args, fixture, args.expected_mac)
        baseline = original_directory
        accounts = await create_accounts(fixture)
        conversion = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-y", "-i", str(args.speech_audio),
            "-t", "60", "-ar", "8000", "-ac", "1", str(args.output / "peer-source.wav"))
        if await conversion.wait():
            raise RuntimeError("Could not prepare the existing speech fixture")
        peers.TEST_CAPTURE_DIR = args.output

        async def peer(account):
            actor = await asyncio.to_thread(start_peer, account, args.output, server, args.ha_sip_port, local_ip)
            actors.append(actor)
            wire = esp.BareSipWire(actor)
            wires.append(wire)
            actor.command("/siptrace yes")
            await wire.wait_text("SIP trace is now enabled")
            return actor

        for account in accounts[:3]:
            await peer(account)
        wire_dir = args.output / "blf-wire"
        wire_dir.mkdir(mode=0o700)
        trace_args = SimpleNamespace(**vars(args))
        frames = result["frames"] = []

        async def capture(label, offset, duration):
            await demo(witness)
            await witness.service("screenshot_show_contacts")
            witness.expected_page = "call_idle"
            trace_args.contacts_capture_offset = offset
            trace_args.contacts_row_height = args.row_height
            trace_args.contacts_viewport = args.viewport
            trace_args.output = args.output / "scene.json"
            files = await hil.capture_contact_pages(witness, trace_args, label, 8)
            frames.append({"file": Path(files[0]).name, "duration_ms": duration, "state": label, "offset": offset})

        maximum = hil.contact_page_offsets(8, row_height=args.row_height, viewport=args.viewport)[-1]
        async with hil.MwiTrace(trace_args, wire_dir) as trace:
            await demo(witness)
            await state_ready(witness, "GalleryHome", "idle")
            await capture("idle", 0, 1100)
            await capture("services", maximum, 1200)
            first = len(actors[1].output)
            actors[0].command(f'/dial sip:{accounts[1]["username"]}@{server}:{args.ha_sip_port}')
            await wires[1].wait_text("Incoming call from:", since=first)
            await state_ready(witness, "GalleryHome", "ringing")
            await capture("ringing", 0, 1200)
            actors[1].command("/accept")
            await wires[0].wait_text("Call established")
            await state_ready(witness, "GalleryHome", "active")
            await capture("active", 0, 1100)
            await wires[0].direction("/hold")
            await state_ready(witness, "GalleryGarage", "held")
            await capture("held", 0, 1200)
            await wires[0].direction("/resume")
            actors[0].hangup()
            await state_ready(witness, "GalleryHome", "idle")
            for offset in (*range(0, maximum, max(1, args.row_height // 2)), maximum):
                await capture("scroll", offset, 650 if offset in {0, maximum} else 160)
            await peer(accounts[3])
            await state_ready(witness, "GalleryGuest", "idle")
            await capture("guest-idle", maximum, 1300)
            result["blf_sip_capture"] = str(trace.path)
        await witness.service("screenshot_show_options")
        witness.expected_page = "settings"
        result["options"] = await witness.screenshot(args.address, args.output / "options.jpg")
        result["mailbox"] = await capture_mailbox(witness, menu_args, fixture, server, local_ip)
        result["native_status_samples"] = witness.native_samples
        result["capture_session_completed"] = True
    finally:
        try:
            if baseline is not None:
                if witness.call_state != "idle":
                    await witness.service("hangup_call")
                    await witness.wait(lambda: witness.call_state == "idle")
                for wire in wires:
                    wire.close()
                for index, actor in enumerate(actors):
                    try:
                        actor.read()
                        text = actor.output
                        for account in fixture.owned.values():
                            if account.get("password"):
                                text = text.replace(account["password"], "[redacted]")
                        (args.output / f"peer-{index}.log").write_text(text)
                    finally:
                        await asyncio.to_thread(actor.close)
                await fixture.cleanup_accounts()
                account_names = {row["username"] for row in
                    (await fixture.action("list_accounts", response=True))["accounts"]}
                if account_names != fixture.baseline:
                    raise RuntimeError("The account baseline changed; unrelated accounts were preserved")
                await witness.service("refresh_ldap_directory")
                await hil.wait_snapshot(witness, lambda row: row["contacts"] == baseline["contacts"])
                await witness.wait(lambda: witness.values.get(surface_key) == baseline_surface)
                if {key: witness.values.get(key) for key in preferences} != preferences:
                    raise RuntimeError("Gallery changed an existing Number, Switch or Select preference")
                audio = await hil.full_audio_snapshot(witness)
                if audio["audio_active"] or audio["rtp"]["running"] or audio["queues"]["tx_depth"] or audio["queues"]["rx_depth"]:
                    raise RuntimeError("Gallery did not release the native media owners")
                result["cleanup"] = {"directory_surface_restored": True, "preferences_unchanged": True,
                                     "audio_quiescent": True, "owned_accounts_removed": True}
                for username in fixture.owned:
                    (args.output / username / "accounts").unlink(missing_ok=True)
                hil.write_private(args.output / "fixture-accounts.json", {
                    "run_id": fixture.run_id, "baseline": sorted(fixture.baseline),
                    "owned_ids": sorted(fixture.owned), "cleanup_verified": True})
        except BaseException as error:
            result["cleanup_failure_type"] = type(error).__name__
            raise
        finally:
            peers.TEST_CAPTURE_DIR = peer_capture_root
            await witness.close()
            result["native_status_samples"] = witness.native_samples
            hil.write_private(args.output / "native-api-log.json", witness.log)
            hil.write_private(args.output / "gallery-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--home-auth", required=True, type=Path)
    parser.add_argument("--speech-audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--voip-source", type=Path, default=ROOT.parent / "esphome-voip-stack")
    parser.add_argument("--ha-sip-host")
    parser.add_argument("--ha-sip-port", type=int, default=5060)
    parser.add_argument("--ha-ssh", default="hass")
    parser.add_argument("--ha-python", default="/var/lib/hass/.venv/bin/python")
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--row-height", type=int, default=52)
    parser.add_argument("--viewport", type=int, default=208)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
