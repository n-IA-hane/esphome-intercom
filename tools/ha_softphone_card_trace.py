#!/usr/bin/env python3
"""Trace authoritative HA softphone snapshots and the Lovelace card in parallel."""

from __future__ import annotations

import argparse
import json
import os
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=50.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument(
        "--out", default=str(ROOT / "test_runs" / "ha_softphone_card_trace.json")
    )
    args = parser.parse_args()
    from ha_playwright_auth import context_kwargs
    from playwright.sync_api import sync_playwright

    console: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(**context_kwargs())
        page = context.new_page()
        page.on(
            "console", lambda message: console.append(f"{message.type}: {message.text}")
        )
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        # HA may replace the initial dashboard route once while restoring its
        # frontend navigation state. Do not attach to that disposable realm.
        page.wait_for_timeout(5_000)
        page.wait_for_function(
            """() => {
              const all = (root = document) => {
                let found = [...root.querySelectorAll('voip-stack-card, intercom-card')];
                for (const node of root.querySelectorAll('*')) if (node.shadowRoot) found = found.concat(all(node.shadowRoot));
                return found;
              };
              return all().some((card) => (card.config?.mode || card.config?.card_mode || '') === 'ha_softphone');
            }""",
            timeout=30_000,
        )
        page.evaluate(INSTALL_TRACE)
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            sample = page.evaluate(SAMPLE)
            print(json.dumps(sample, separators=(",", ":")), flush=True)
            page.wait_for_timeout(max(10, int(args.interval * 1000)))
        result = page.evaluate("() => window.__voipTrace")
        result["console"] = console
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
