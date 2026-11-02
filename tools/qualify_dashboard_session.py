"""Bounded Dashboard mode for the existing real Lovelace qualification driver.

The parent driver owns browser authentication, HA service/symlink wrapper,
fixture views, SIP peers and CDP BrowserAudioCapture. No action runs on import.
The optional physical actor remains outside this module and outside LAB's
ESPHome integration. A host/loopback result never claims physical qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit
from urllib.request import urlopen

from qualification.final_session import run_owned_case


# Read only already-published data. In particular, never call peer capability
# discovery, a card render/update method, media acquisition or engine setters.
DASHBOARD_SAMPLE = r"""card => {
  const store = card._store;
  const phones = [...(store?.phones?.values() || [])].map(phone => ({
    endpoint_id:phone.endpoint_id,device_id:phone.device_id,state:phone.state,
    availability:phone.availability,calls:(phone.calls || []).map(call => ({
      id:call.id,call_id:call.call_id,peer_endpoint_id:call.peer_endpoint_id,
      pickup_available:call.pickup_available,state:call.state
    }))
  }));
  const buttons = [...(card.shadowRoot?.querySelectorAll('button') || [])].map(button => ({
    label:button.getAttribute('aria-label') || button.textContent,
    disabled:button.disabled,hidden:button.hidden
  }));
  return {ready:store?.ready === true,revision:store?.revision,owner_device_id:card._config?.device_id,
    pending:card._pending || '',phones,buttons};
}"""


def validate_browser_pcm(capture, *, request_ids, minimum_seconds=20):
    """Decode the actual maintained CDP capture, independently of GUI counters."""
    if capture.errors:
        raise ValueError("Browser PCM capture contains protocol errors")
    if (not isinstance(request_ids, (set, frozenset)) or not request_ids
            or not all(isinstance(value, str) and value for value in request_ids)):
        raise ValueError("Browser PCM requires current call WebSocket request IDs")
    directions = {"tx": {"samples": 0, "seconds": 0, "energy": 0},
                  "rx": {"samples": 0, "seconds": 0, "energy": 0}}
    for (request_id, side, token), stream in capture.streams.items():
        if request_id not in request_ids:
            continue
        rate, pcm, channels, _ptime = token.split(":")
        rate, channels = int(rate), int(channels)
        if side not in directions or pcm != "s16le" or not 8000 <= rate <= 192000 or channels not in {1, 2}:
            raise ValueError("Unsupported observed browser PCM direction")
        raw = stream["pcm"]
        if len(raw) % (channels * 2):
            raise ValueError("Browser PCM frame alignment differs")
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        value = directions[side]
        value["samples"] += len(samples)
        value["seconds"] += len(samples) / channels / rate
        value["energy"] += sum(sample * sample for sample in samples)
    if any(value["seconds"] < minimum_seconds or not value["energy"] for value in directions.values()):
        raise ValueError("Missing independent nonzero browser PCM in both directions")
    return {side: {"seconds": value["seconds"], "samples": value["samples"],
                   "rms": math.sqrt(value["energy"] / value["samples"])}
            for side, value in directions.items()}


def verify_served_frontend(base_url: str, source: Path):
    if urlsplit(base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Dashboard qualification uses only the existing loopback HA LAB")
    frontend = source / "custom_components/voip_stack/frontend"
    hashes = {}
    for name in ["voip-stack-card.js", "voip-stack-engine.js", "voip-stack-dashboard-card.js",
                 "voip-stack-dashboard-model.js", "voip-stack-dashboard-view.js"]:
        expected = hashlib.sha256((frontend / name).read_bytes()).hexdigest()
        url = base_url.rstrip("/") + "/voip-stack/" + name
        with urlopen(url, timeout=5) as response:
            if response.status != 200 or response.geturl() != url:
                raise RuntimeError("Candidate frontend resource was redirected or unavailable")
            actual = hashlib.sha256(response.read()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Served frontend differs from candidate: {name}")
        hashes[name] = actual
    return hashes


class DashboardSession:
    """Use ordinary GUI actions with the existing call owner and real actors."""

    def __init__(self, page, *, owner_state, peer_state, runtime_snapshot, timeout=20):
        self.page, self.owner_state, self.peer_state = page, owner_state, peer_state
        self.runtime_snapshot, self.timeout = runtime_snapshot, timeout
        self.card = page.locator("voip-stack-dashboard-card")
        self.owned_call_id = ""

    async def sample(self):
        return await self.card.evaluate(DASHBOARD_SAMPLE)

    async def click(self, action, endpoint_id=None):
        if action in {"call", "pickup"}:
            if not isinstance(endpoint_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", endpoint_id):
                raise ValueError("One explicit Dashboard endpoint is required")
            selector = f'.tile[data-key="endpoint:{endpoint_id}"] ' + (".tile-main" if action == "call" else ".pickup")
        elif action in {"answer", "hangup"}:
            selector = ".owner-call ." + action
        else:
            raise ValueError("Unknown ordinary Dashboard action")
        button = self.card.locator(selector)
        await button.wait_for(state="visible", timeout=self.timeout * 1000)
        await button.click(timeout=self.timeout * 1000)

    async def established(self, peer_endpoint):
        async with asyncio.timeout(self.timeout):
            while True:
                owner, peer = await self.owner_state(), await self.peer_state()
                calls = owner.get("calls") or []
                selected = [call for call in calls if call.get("peer_endpoint_id") == peer_endpoint]
                if owner.get("state") != "idle" and calls and len(selected) != 1:
                    raise RuntimeError("Dashboard call belongs to a different or ambiguous peer")
                call_id = selected[0].get("call_id") if selected else None
                if self.owned_call_id and call_id != self.owned_call_id:
                    raise RuntimeError("Dashboard dialog identity changed")
                if call_id:
                    self.owned_call_id = call_id
                peer_calls = peer.get("calls") or []
                if (owner.get("state") == peer.get("state") == "in_call" and call_id
                        and any(call.get("call_id") == call_id for call in peer_calls)):
                    return call_id
                await asyncio.sleep(0.1)

    async def run(self, action, *, peer_endpoint, stage_peer, measure_media, cleanup_peer):
        """stage_peer/measure_media are existing real-actor hooks, not test tones.

        The outer driver must retain private PCAP, independent SIP-peer WAV,
        CDP PCM and screenshots and check media EOF after this owner releases.
        """
        async def operation():
            sample = await self.sample()
            if not sample["ready"] or sample["pending"]:
                raise RuntimeError("Real Dashboard monitor has no current ready snapshot")
            await stage_peer()
            await self.click(action, peer_endpoint)
            call_id = await self.established(peer_endpoint)
            await measure_media(call_id)

        async def cleanup():
            try:
                current = await self.owner_state()
                calls = current.get("calls") or []
                if current.get("state") == "idle":
                    return
                if not self.owned_call_id or not any(call.get("call_id") == self.owned_call_id for call in calls):
                    raise RuntimeError("Refusing to hang up a call owned by another actor")
                await self.click("hangup")
            finally:
                # The injected existing peer owner cleans only its own dialog,
                # even if the GUI failed before its Call-ID became observable.
                await cleanup_peer()

        result = await run_owned_case(operation, cleanup, self.runtime_snapshot)
        return {**result, "scope": "real-LAB-Dashboard-actor-observations", "call_id": self.owned_call_id,
                "physical": False, "acoustic": False, "G0_pass": False}
