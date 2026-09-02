#!/usr/bin/env python3
"""Exercise a real HA card in Android Chrome and save media counters."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, async_playwright


async def _wait_state(card: Locator, wanted: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await card.evaluate("(element) => element._softphoneSnapshot?.state") == wanted:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"HA card did not reach {wanted!r}")


def _local_storage(path: Path, origin: str) -> dict[str, str]:
    state = json.loads(path.read_text(encoding="utf-8"))
    entry = next(item for item in state["origins"] if item["origin"] == origin)
    return {item["name"]: item["value"] for item in entry["localStorage"]}


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    storage = _local_storage(args.storage_state, args.ha_url)
    results: list[dict[str, Any]] = []
    console_messages: list[dict[str, str]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(args.cdp_url)
        pages = [page for context in browser.contexts for page in context.pages]
        if not pages:
            raise RuntimeError("Android Chrome has no debuggable page")
        page = pages[0]
        for candidate in reversed(pages):
            if await candidate.locator("voip-stack-card").count():
                page = candidate
                break
        await page.goto(args.ha_url, wait_until="domcontentloaded")
        await page.evaluate(
            "(values) => Object.entries(values).forEach(([key, value]) => localStorage.setItem(key, value))",
            storage,
        )
        await page.reload(wait_until="domcontentloaded")
        page.on(
            "console",
            lambda message: console_messages.append(
                {"type": message.type, "text": message.text}
            ),
        )
        page.on(
            "pageerror",
            lambda error: console_messages.append(
                {"type": "pageerror", "text": str(error)}
            ),
        )
        card = page.locator("voip-stack-card").first
        await card.wait_for(timeout=20_000)
        await page.wait_for_timeout(2_000)

        for cycle in range(1, args.cycles + 1):
            active: dict[str, Any] | None = None
            try:
                await card.evaluate(
                    """(element, target) => {
                        element._softphoneKeypadOpen = true;
                        element._softphoneManualTarget = target;
                        element._render();
                    }""",
                    args.target,
                )
                await asyncio.wait_for(
                    card.evaluate("(element) => element._startCall()"),
                    timeout=30.0,
                )
                await _wait_state(card, "in_call")
                if args.reload_after > 0:
                    before_reload = min(args.reload_after, args.duration)
                    await page.wait_for_timeout(round(before_reload * 1_000))
                    await page.reload(wait_until="domcontentloaded")
                    card = page.locator("voip-stack-card").first
                    await card.wait_for(timeout=20_000)
                    await _wait_state(card, "in_call")
                    await page.wait_for_timeout(
                        round(max(0.0, args.duration - before_reload) * 1_000)
                    )
                else:
                    await page.wait_for_timeout(round(args.duration * 1_000))
                await _wait_state(card, "in_call", timeout=0.5)
                active = await card.evaluate(
                    """(element) => ({
                        snapshot: element._softphoneSnapshot,
                        error: element._errorMsg || '',
                        engine: {
                            active: globalThis.__voipStackEngine?.active,
                            call_id: globalThis.__voipStackEngine?.callId,
                            endpoint_id: globalThis.__voipStackEngine?.endpointId,
                            stats: globalThis.__voipStackEngine?._stats,
                        },
                    })"""
                )
            finally:
                async def cleanup_call() -> None:
                    if (
                        await card.evaluate(
                            "(element) => element._softphoneSnapshot?.state"
                        )
                        != "idle"
                    ):
                        await asyncio.wait_for(
                            card.evaluate("(element) => element._hangup()"),
                            timeout=10.0,
                        )
                        await _wait_state(card, "idle", timeout=10.0)

                cleanup_task = asyncio.create_task(cleanup_call())
                await asyncio.shield(cleanup_task)
            if active is None:
                raise RuntimeError("Call ended before active media counters were read")
            await page.wait_for_timeout(round(args.settle * 1_000))
            terminal = await card.evaluate("(element) => element._softphoneSnapshot")
            results.append(
                {
                    "cycle": cycle,
                    "active": active,
                    "terminal": terminal,
                    "console": list(console_messages),
                }
            )
        await browser.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/playwright-storage.json"),
    )
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--settle", type=float, default=5.0)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--reload-after", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = []
    for result in results:
        snapshot = result["active"]["snapshot"]
        audio = snapshot.get("media_debug", {}).get("audio", {})
        summary.append(
            {
                "cycle": result["cycle"],
                "error": result["active"]["error"],
                "tx_plc": audio.get("tx_playout_plc"),
                "tx_late_discard": audio.get("tx_playout_late_discard"),
                "rx_plc": audio.get("rx_playout_plc"),
                "rx_late_discard": audio.get("rx_playout_late_discard"),
                "capture_timing_reports": audio.get("tx_capture_timing_reports"),
                "capture_gap_max_ms": audio.get("tx_capture_timing_max_ms"),
                "target_ms": audio.get("tx_playout_target_ms"),
            }
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
