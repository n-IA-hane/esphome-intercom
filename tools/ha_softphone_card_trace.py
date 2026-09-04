#!/usr/bin/env python3
"""Trace authoritative HA softphone snapshots and the Lovelace card in parallel."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test_runs"))


DEFAULT_URL = os.environ.get(
    "HA_CARD_URL",
    "http://127.0.0.1:18123/lovelace/default_view",
)

INSTALL_TRACE = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card) throw new Error("HA softphone card not found");
  window.__voipTrace = { raw: [], softphone: [], samples: [] };
  window.__voipTraceUnsub = await card._hass.connection.subscribeMessage(
    (event) => window.__voipTrace.raw.push({ at: performance.now(), event }),
    { type: "voip_stack/subscribe_call_events" },
  );
  window.__voipTraceSoftphoneUnsub = await card._hass.connection.subscribeMessage(
    (state) => window.__voipTrace.softphone.push({ at: performance.now(), state }),
    { type: "voip_stack/subscribe_ha_softphone_state" },
  );
  return true;
}
"""

SAMPLE = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  const backend = await card._hass.connection.sendMessagePromise({
    type: "voip_stack/ha_softphone_state",
  });
  const snapshot = card._softphoneSnapshot || {};
  const item = {
    at: performance.now(),
    backend: {
      state: backend?.state || "",
      call_id: backend?.call_id || "",
      caller: backend?.caller || "",
      terminal_reason: backend?.terminal_reason || "",
    },
    card: {
      state: snapshot.state || "",
      call_id: snapshot.call_id || "",
      caller: snapshot.caller || "",
      terminal_reason: snapshot.terminal_reason || "",
    },
    subscribers: window.__voipStackEngine?._callSubscribers?.size ?? -1,
    raw_count: window.__voipTrace.raw.length,
    softphone_count: window.__voipTrace.softphone.length,
  };
  window.__voipTrace.samples.push(item);
  return item;
}
"""

CARD_HANDLE = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  return deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone") || null;
}
"""


def _safe_name(value: object) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "unknown")).strip("-")
    return text or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=50.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument(
        "--out", default=str(ROOT / "test_runs" / "ha_softphone_card_trace.json")
    )
    parser.add_argument("--screenshots-dir")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument("--language", choices=("de", "en", "it", "pt-BR"))
    args = parser.parse_args()
    from ha_playwright_auth import context_kwargs
    from playwright.sync_api import sync_playwright

    console: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            ignore_default_args=["--mute-audio"],
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                f"--unsafely-treat-insecure-origin-as-secure={args.url.split('/lovelace', 1)[0]}",
            ],
        )
        context = browser.new_context(
            **context_kwargs(),
            viewport={"width": args.viewport_width, "height": args.viewport_height},
        )
        if args.language:
            context.add_init_script(
                "localStorage.setItem('selectedLanguage', JSON.stringify("
                f"{json.dumps(args.language)}));"
            )
        page = context.new_page()
        page.on(
            "console", lambda message: console.append(f"{message.type}: {message.text}")
        )
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        # HA may replace the initial dashboard route once while restoring its
        # frontend navigation state. Do not attach to that disposable realm.
        page.wait_for_timeout(5_000)
        card_ready = """() => {
              const all = (root = document) => {
                let found = [...root.querySelectorAll('voip-stack-card, intercom-card')];
                for (const node of root.querySelectorAll('*')) if (node.shadowRoot) found = found.concat(all(node.shadowRoot));
                return found;
              };
              return all().some((card) => (card.config?.mode || card.config?.card_mode || '') === 'ha_softphone');
            }"""
        try:
            page.wait_for_function(card_ready, timeout=15_000)
        except Exception:
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(card_ready, timeout=30_000)
        page.evaluate(INSTALL_TRACE)
        screenshots_dir = Path(args.screenshots_dir) if args.screenshots_dir else None
        if screenshots_dir:
            screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_records: list[dict[str, object]] = []
        last_signature: tuple[str, str, str] | None = None
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            sample = page.evaluate(SAMPLE)
            print(json.dumps(sample, separators=(",", ":")), flush=True)
            backend = sample.get("backend") or {}
            signature = (
                str(backend.get("state") or ""),
                str(backend.get("call_id") or ""),
                str(backend.get("terminal_reason") or ""),
            )
            if screenshots_dir and signature != last_signature:
                index = len(screenshot_records)
                stem = (
                    f"{index:02d}-{_safe_name(signature[0])}-"
                    f"{_safe_name(signature[2] or signature[1] or 'no-call')}"
                )
                page_path = screenshots_dir / f"{stem}-page.png"
                card_path = screenshots_dir / f"{stem}-card.png"
                page.screenshot(path=str(page_path), full_page=True)
                handle = page.evaluate_handle(CARD_HANDLE).as_element()
                if handle is not None:
                    handle.screenshot(path=str(card_path))
                screenshot_records.append(
                    {
                        "signature": signature,
                        "page": str(page_path),
                        "card": str(card_path) if handle is not None else "",
                    }
                )
                last_signature = signature
            page.wait_for_timeout(max(10, int(args.interval * 1000)))
        result = page.evaluate("() => window.__voipTrace")
        result["console"] = console
        result["screenshots"] = screenshot_records
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
