#!/usr/bin/env python3
"""Exercise an incoming or outgoing SIP video call on the HA card.

The caller is intentionally external to this process (bareSIP or a real video
phone).  Start the probe, place the call while it is waiting, and let the probe
answer through the actual Lovelace card.  The resulting JSON records backend,
card, WebCodecs and canvas evidence rather than relying on visual inspection.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from tools.ha_voip_lab.refresh_playwright_auth import (
        playwright_storage_origin,
        refresh_playwright_auth,
    )
except ModuleNotFoundError:  # Direct execution adds tools/, not the repo root.
    from ha_voip_lab.refresh_playwright_auth import (
        playwright_storage_origin,
        refresh_playwright_auth,
    )


LOCAL_SECRET_ROOT = Path.home() / ".secrets/esphome-intercom"


def _local_default(environment_name: str, filename: str) -> str:
    configured = os.environ.get(environment_name, "")
    if configured:
        return configured
    candidate = LOCAL_SECRET_ROOT / filename
    return str(candidate) if candidate.is_file() else ""


DEFAULT_URL = os.environ.get("HA_URL", "")
DEFAULT_STORAGE_STATE = _local_default(
    "PLAYWRIGHT_STORAGE_STATE",
    "ha_playwright_storage.json",
)
DEFAULT_CHROMIUM = os.environ.get("CHROMIUM_PATH", "") or shutil.which("chromium") or ""
DEFAULT_REFRESH_CREDENTIALS = _local_default(
    "HA_PLAYWRIGHT_REFRESH_CREDENTIALS",
    "ha_home_auth.json",
)
DEFAULT_REFRESH_URL = os.environ.get("HA_PLAYWRIGHT_REFRESH_URL", "")
DEFAULT_DASHBOARD_PATH = os.environ.get(
    "HA_PLAYWRIGHT_DASHBOARD_PATH",
    "/lovelace/test",
)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test_runs"))


class _ProbeComplete(Exception):
    """Internal successful short-circuit for a pre-answer cancellation test."""


_SIGNED_QUERY_RE = re.compile(r"(?i)(authSig|access_token)=[^&'\"\s]+")


def _sanitise_browser_message(value: object) -> str:
    """Keep browser diagnostics useful without persisting signed credentials."""

    return _SIGNED_QUERY_RE.sub(r"\1=<redacted>", str(value))


def _dashboard_url(value: str, storage_path: Path | None, dashboard_path: str) -> str:
    """Resolve the full Lovelace URL without changing its authenticated origin."""

    resolved = str(value or "")
    if not resolved:
        if storage_path is None:
            return ""
        resolved = playwright_storage_origin(storage_path)
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return resolved
    if parsed.path not in {"", "/"}:
        return resolved
    path = str(dashboard_path or "")
    if not path.startswith("/"):
        path = f"/{path}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _is_ha_auth_url(value: str) -> bool:
    path = urlsplit(value).path.rstrip("/")
    return path.startswith("/auth/") or path == "/auth"

DEEP_CARD = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const configured = (card) => Boolean(
    card?.isConnected
    && (card.config?.mode || card.config?.card_mode || "") === "ha_softphone"
  );
  const visible = (card) => Boolean(
    configured(card) && card.getClientRects().length > 0
  );
  globalThis.__voipStackProbeFindCard = () => {
    const current = globalThis.__voipStackProbeCard;
    if (configured(current)) return current;
    const cards = deep("voip-stack-card, intercom-card")
      .filter((card) => (card.config?.mode || card.config?.card_mode || "") === "ha_softphone");
    // Lovelace can briefly detach/recreate the card while the same global
    // media engine keeps a call alive. Do not erase the last controller during
    // that handoff; prefer a visible replacement as soon as it exists.
    const next = cards.find(visible) || cards.find(configured) || cards[0] || current || null;
    globalThis.__voipStackProbeCard = next;
    const deviceId = String(next?.config?.device_id || "");
    if (deviceId) globalThis.__voipStackProbeDeviceId = deviceId;
    return next;
  };
  return globalThis.__voipStackProbeFindCard();
}
"""

BACKEND_SAMPLE = r"""
async () => {
  const hass = document.querySelector("home-assistant")?.hass;
  if (!hass?.connection) return {};
  const card = globalThis.__voipStackProbeCard
    || globalThis.__voipStackProbeFindCard?.()
    || null;
  const deviceId = String(
    card?.config?.device_id || globalThis.__voipStackProbeDeviceId || ""
  );
  return await hass.connection.sendMessagePromise({
    type: "voip_stack/ha_softphone_state",
    ...(deviceId ? { device_id: deviceId } : {}),
  });
}
"""

LIGHT_CARD_STATE = r"""
() => {
  const card = globalThis.__voipStackProbeCard
    || globalThis.__voipStackProbeFindCard?.()
    || null;
  if (!card) return null;
  const snapshot = card._softphoneSnapshot || {};
  const engine = globalThis.__voipStackEngine;
  return {
    card_state: String(snapshot.state || ""),
    terminal_reason: String(snapshot.terminal_reason || ""),
    card_error: String(card._errorMsg || globalThis.__voipStackProbeStartError || ""),
    call_id: String(snapshot.call_id || ""),
    video_active: Boolean(snapshot.video_active),
    video_offered: Boolean(snapshot.video_offered),
    video_format: String(snapshot.video_format || ""),
    video_direction: String(snapshot.video_direction || ""),
    engine_state: String(engine?.state || ""),
    engine_call_id: String(engine?.callId || ""),
    engine_video_active: Boolean(engine?.videoActive),
    engine_video_visible: Boolean(engine?.videoVisible),
    owns_current_call: Boolean(engine?.ownsSoftphoneSession?.(snapshot.call_id)),
    starting: Boolean(card._starting),
    stopping: Boolean(card._stopping),
    has_audio_attach_task: Boolean(card._audioAttachTask),
    has_cleanup_task: Boolean(card._cleanupTask),
    engine_stats: engine?.stats || null,
  };
}
"""

CARD_SAMPLE = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = globalThis.__voipStackProbeFindCard?.() || null;
  if (!card) return null;
  const snapshot = card._softphoneSnapshot || {};
  const root = card.shadowRoot || card;
  const surface = deep("ha-card.card", root)[0] || null;
  const canvas = deep("canvas.video-canvas", card.shadowRoot || card)[0] || null;
  const nativeCameraHost = deep(".native-camera", root)[0] || null;
  const nativeCameraCard = nativeCameraHost?.querySelector?.(".native-camera-card") || null;
  const nativeCameraMedia = nativeCameraCard
    ? deep("img, video", nativeCameraCard.shadowRoot || nativeCameraCard)[0] || null
    : null;
  const hangup = deep("button.hangup", root)[0] || null;
  const header = deep(".header", root)[0] || null;
  const stats = deep(".hangup-stats", root)[0] || null;
  const rect = (element, relativeTo = null) => {
    if (!element || element.hidden) return null;
    const value = element.getBoundingClientRect();
    const base = relativeTo?.getBoundingClientRect?.() || { left: 0, top: 0 };
    return {
      x: value.left - base.left,
      y: value.top - base.top,
      width: value.width,
      height: value.height,
      right: value.right - base.left,
      bottom: value.bottom - base.top,
    };
  };
  const surfaceRect = rect(surface);
  const canvasRect = rect(canvas, surface);
  const hangupRect = rect(hangup, surface);
  const headerRect = rect(header, surface);
  const statsRect = rect(stats, surface);
  const overlaps = (left, right) => Boolean(
    left && right && left.x < right.right && left.right > right.x
      && left.y < right.bottom && left.bottom > right.y
  );
  let canvasEvidence = null;
  if (canvas && canvas.width && canvas.height) {
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const points = [
      [0, 0],
      [Math.floor(canvas.width / 2), Math.floor(canvas.height / 2)],
      [Math.max(0, canvas.width - 1), Math.max(0, canvas.height - 1)],
    ];
    const pixels = points.map(([x, y]) => [...context.getImageData(x, y, 1, 1).data]);
    canvasEvidence = {
      width: canvas.width,
      height: canvas.height,
      hidden: canvas.hidden,
      pixels,
      non_black: pixels.some((pixel) => pixel[0] || pixel[1] || pixel[2]),
    };
  }
  return {
    card_state: String(snapshot.state || ""),
    call_id: String(snapshot.call_id || ""),
    video_active: Boolean(snapshot.video_active),
    video_offered: Boolean(snapshot.video_offered),
    video_format: String(snapshot.video_format || ""),
    video_direction: String(snapshot.video_direction || ""),
    debug_mode: Boolean(snapshot.debug_mode),
    engine_state: String(globalThis.__voipStackEngine?.state || ""),
    engine_device_id: String(globalThis.__voipStackEngine?.deviceId || ""),
    engine_call_id: String(globalThis.__voipStackEngine?.callId || ""),
    engine_video_active: Boolean(globalThis.__voipStackEngine?.videoActive),
    engine_video_visible: Boolean(globalThis.__voipStackEngine?.videoVisible),
    owns_current_call: Boolean(globalThis.__voipStackEngine?.ownsSoftphoneSession?.(snapshot.call_id)),
    starting: Boolean(card._starting),
    stopping: Boolean(card._stopping),
    has_audio_attach_task: Boolean(card._audioAttachTask),
    has_cleanup_task: Boolean(card._cleanupTask),
    engine_stats: globalThis.__voipStackEngine?.stats || null,
    video_debug: globalThis.__voipStackEngine?._video ? {
      frame_queue: (globalThis.__voipStackEngine._video._frameQueue || []).map((frame) => Number(frame.timestamp || 0)),
      render_handle: Number(globalThis.__voipStackEngine._video._renderHandle || 0),
      playout_base_wall: Number(globalThis.__voipStackEngine._video._playoutBaseWall || 0),
      playout_base_timestamp: Number(globalThis.__voipStackEngine._video._playoutBaseTimestamp || 0),
      last_rendered_timestamp: Number(globalThis.__voipStackEngine._video._lastRenderedTimestamp || 0),
      last_decoded_timestamp: Number(globalThis.__voipStackEngine._video._lastDecodedTimestamp || 0),
      performance_now: performance.now(),
    } : null,
    layout: surface ? {
      surface: surfaceRect,
      canvas: canvasRect,
      hangup: hangupRect,
      header: headerRect,
      stats: statsRect,
      horizontal_overflow: surface.scrollWidth > surface.clientWidth + 1,
      vertical_overflow: surface.scrollHeight > surface.clientHeight + 1,
      header_stats_overlap: overlaps(headerRect, statsRect),
      stats_outside_hangup: Boolean(
        statsRect && hangupRect && (
          statsRect.x < hangupRect.x || statsRect.right > hangupRect.right
            || statsRect.y < hangupRect.y || statsRect.bottom > hangupRect.bottom
        )
      ),
      usable_video_height: Math.max(
        0,
        Number(hangupRect?.y || surface.clientHeight)
          - Number(headerRect?.bottom || 0),
      ),
    } : null,
    canvas: canvasEvidence,
    native_camera: {
      entity_id: String(card._nativeCameraEntityId || ""),
      host_hidden: Boolean(nativeCameraHost?.hidden),
      mounted: Boolean(nativeCameraCard),
      mount_pending: Boolean(card._nativeCameraMountTask),
      card_tag: String(nativeCameraCard?.tagName || "").toLowerCase(),
      entity_state: String(
        card._hass?.states?.[card._nativeCameraEntityId || ""]?.state || ""
      ),
      media_tag: String(nativeCameraMedia?.tagName || "").toLowerCase(),
      media_ready: Boolean(
        Number(nativeCameraMedia?.naturalWidth || nativeCameraMedia?.videoWidth || 0) > 0
      ),
      layout: rect(nativeCameraHost, surface),
    },
  };
}
"""

INSTALL_RESPONSIVENESS_MONITOR = r"""
() => {
  if (globalThis.__voipProbeResponsiveness?.active) return true;
  const state = {
    active: true,
    tick_interval_ms: 50,
    last_tick_at: performance.now(),
    tick_count: 0,
    max_main_thread_gap_ms: 0,
    max_main_thread_lag_ms: 0,
    gaps_over_100_ms: 0,
    gaps_over_250_ms: 0,
    tick_timer: 0,
    ws_ping_interval_ms: 1000,
    ws_timeout_ms: 2000,
    ws_timer: 0,
    ws_rtt_in_flight: false,
    ws_rtt_count: 0,
    ws_rtt_errors: 0,
    ws_rtt_timeouts: 0,
    ws_rtt_stalled: false,
    ws_rtt_sum_ms: 0,
    last_ws_rtt_ms: 0,
    max_ws_rtt_ms: 0,
    last_hangup_dispatch_ms: 0,
  };
  globalThis.__voipProbeResponsiveness = state;

  const tick = () => {
    if (!state.active) return;
    const now = performance.now();
    const gap = Math.max(0, now - state.last_tick_at);
    const lag = Math.max(0, gap - state.tick_interval_ms);
    state.last_tick_at = now;
    state.tick_count++;
    state.max_main_thread_gap_ms = Math.max(state.max_main_thread_gap_ms, gap);
    state.max_main_thread_lag_ms = Math.max(state.max_main_thread_lag_ms, lag);
    if (gap >= 100) state.gaps_over_100_ms++;
    if (gap >= 250) state.gaps_over_250_ms++;
    state.tick_timer = setTimeout(tick, state.tick_interval_ms);
  };

  const schedulePing = () => {
    if (!state.active) return;
    state.ws_timer = setTimeout(ping, state.ws_ping_interval_ms);
  };
  const ping = async () => {
    if (!state.active || state.ws_rtt_in_flight) return;
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass?.connection) {
      schedulePing();
      return;
    }
    state.ws_rtt_in_flight = true;
    const started = performance.now();
    let settled = false;
    const timeout = setTimeout(() => {
      if (settled || !state.active) return;
      state.ws_rtt_timeouts++;
      state.ws_rtt_stalled = true;
    }, state.ws_timeout_ms);
    try {
      const card = globalThis.__voipStackProbeCard
        || globalThis.__voipStackProbeFindCard?.()
        || null;
      const deviceId = String(
        card?.config?.device_id || globalThis.__voipStackProbeDeviceId || ""
      );
      await hass.connection.sendMessagePromise({
        type: "voip_stack/ha_softphone_state",
        ...(deviceId ? { device_id: deviceId } : {}),
      });
      settled = true;
      const elapsed = Math.max(0, performance.now() - started);
      state.ws_rtt_count++;
      state.ws_rtt_sum_ms += elapsed;
      state.last_ws_rtt_ms = elapsed;
      state.max_ws_rtt_ms = Math.max(state.max_ws_rtt_ms, elapsed);
    } catch (_) {
      settled = true;
      state.ws_rtt_errors++;
    } finally {
      clearTimeout(timeout);
      state.ws_rtt_in_flight = false;
      schedulePing();
    }
  };

  state.tick_timer = setTimeout(tick, state.tick_interval_ms);
  schedulePing();
  return true;
}
"""

READ_RESPONSIVENESS_MONITOR = r"""
() => {
  const state = globalThis.__voipProbeResponsiveness;
  if (!state) return {};
  return {
    tick_interval_ms: state.tick_interval_ms,
    tick_count: state.tick_count,
    max_main_thread_gap_ms: state.max_main_thread_gap_ms,
    max_main_thread_lag_ms: state.max_main_thread_lag_ms,
    gaps_over_100_ms: state.gaps_over_100_ms,
    gaps_over_250_ms: state.gaps_over_250_ms,
    ws_ping_interval_ms: state.ws_ping_interval_ms,
    ws_timeout_ms: state.ws_timeout_ms,
    ws_rtt_count: state.ws_rtt_count,
    ws_rtt_errors: state.ws_rtt_errors,
    ws_rtt_timeouts: state.ws_rtt_timeouts,
    ws_rtt_stalled: state.ws_rtt_stalled,
    average_ws_rtt_ms: state.ws_rtt_count
      ? state.ws_rtt_sum_ms / state.ws_rtt_count
      : 0,
    last_ws_rtt_ms: state.last_ws_rtt_ms,
    max_ws_rtt_ms: state.max_ws_rtt_ms,
    last_hangup_dispatch_ms: state.last_hangup_dispatch_ms,
  };
}
"""

STOP_RESPONSIVENESS_MONITOR = r"""
() => {
  const state = globalThis.__voipProbeResponsiveness;
  if (!state) return false;
  state.active = false;
  clearTimeout(state.tick_timer);
  clearTimeout(state.ws_timer);
  return true;
}
"""

CLICK_ANSWER = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = globalThis.__voipStackProbeFindCard?.() || null;
  const button = card && deep("button", card.shadowRoot || card)
    .find((item) => item.textContent.trim().toLowerCase() === "answer" && !item.hidden && !item.disabled);
  if (!button) return false;
  button.click();
  return true;
}
"""

CLICK_HANGUP = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = globalThis.__voipStackProbeFindCard?.() || null;
  const button = card && deep("button", card.shadowRoot || card)
    .find((item) => item.classList.contains("hangup") && !item.hidden && !item.disabled);
  if (!button) return false;
  const started = performance.now();
  button.click();
  const dispatchMs = Math.max(0, performance.now() - started);
  if (globalThis.__voipProbeResponsiveness) {
    globalThis.__voipProbeResponsiveness.last_hangup_dispatch_ms = dispatchMs;
  }
  return { clicked: true, dispatch_ms: dispatchMs };
}
"""

CLICK_CAMERA_SEND = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = globalThis.__voipStackProbeFindCard?.() || null;
  if (!card) return false;
  if (globalThis.__voipStackEngine?.videoCameraEnabled) return true;
  let root = card.shadowRoot || card;
  const settings = deep("button", root).find((item) => item.textContent.trim() === "Options");
  if (settings && !card._settingsOpen) {
    settings.click();
    for (let attempt = 0; attempt < 20 && !card._settingsOpen; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    root = card.shadowRoot || card;
  }
  const checkbox = deep("#ha-softphone-video-camera-cb", root)[0];
  if (!checkbox || checkbox.closest("[hidden]")) return false;
  if (!checkbox.checked) {
    checkbox.click();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  // Additional logical phones do not own the page-global engine while idle;
  // their camera preference is nevertheless authoritative on the card and is
  // handed to the engine when that endpoint starts or answers a call.
  return Boolean(checkbox.checked);
}
"""

START_OUTBOUND = r"""
async (destination) => {
  const card = globalThis.__voipStackProbeFindCard?.() || null;
  if (!card) return false;
  card._softphoneKeypadOpen = true;
  card._softphoneManualTarget = String(destination || "");
  // A real click does not await the whole asynchronous call operation. Keep
  // the probe equivalent so a delayed HA service response cannot pin this
  // page.evaluate() call and hide the still-usable Hang up control.
  globalThis.__voipStackProbeStartError = "";
  globalThis.__voipStackProbeStartPromise = Promise.resolve(card._startCall())
    .catch((error) => {
      globalThis.__voipStackProbeStartError = error?.message || String(error);
    });
  return true;
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=(
            "authenticated Home Assistant dashboard URL; when omitted, the "
            "origin is read from --storage-state"
        ),
    )
    parser.add_argument(
        "--dashboard-path",
        default=DEFAULT_DASHBOARD_PATH,
        help=(
            "Lovelace path appended when --url is omitted or contains only "
            "an origin (default: /lovelace/test)"
        ),
    )
    parser.add_argument(
        "--storage-state",
        default=DEFAULT_STORAGE_STATE,
        help="Playwright storage-state JSON for an authenticated HA user",
    )
    parser.add_argument(
        "--refresh-credentials",
        default=DEFAULT_REFRESH_CREDENTIALS,
        help="optional HA refresh-token file used before opening Chromium",
    )
    parser.add_argument(
        "--refresh-url",
        default=DEFAULT_REFRESH_URL,
        help="optional HA token endpoint URL; defaults to the dashboard origin",
    )
    parser.add_argument(
        "--chromium",
        default=DEFAULT_CHROMIUM,
        help="optional Chromium executable path (or set CHROMIUM_PATH)",
    )
    parser.add_argument("--ring-timeout", type=float, default=60)
    parser.add_argument("--video-timeout", type=float, default=25)
    parser.add_argument("--hold-seconds", type=float, default=8)
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0,
        help="record intermediate runtime samples at this interval while holding the call",
    )
    parser.add_argument("--no-hangup", action="store_true")
    parser.add_argument(
        "--expect-remote-hangup",
        action="store_true",
        help="wait for the SIP peer to end the established dialog",
    )
    parser.add_argument(
        "--send-camera",
        action="store_true",
        help="enable the card's Send Camera checkbox after the dialog connects",
    )
    parser.add_argument(
        "--deny-camera",
        action="store_true",
        help=(
            "enable Send Camera but make browser camera acquisition fail; "
            "incoming video and audio must remain usable"
        ),
    )
    parser.add_argument(
        "--allow-dark-video",
        action="store_true",
        help=(
            "allow an all-black decoded canvas when frame counters still prove "
            "that remote video is being received and rendered"
        ),
    )
    parser.add_argument(
        "--expect-audio-only",
        action="store_true",
        help="require a working browser audio call with no active video path",
    )
    parser.add_argument(
        "--expect-native-camera",
        action="store_true",
        help=(
            "require the audio-only peer's ESPHome camera to be mounted through "
            "Home Assistant's native camera card"
        ),
    )
    parser.add_argument(
        "--expect-video-reinvite",
        action="store_true",
        help="allow audio-only ringing and require video to appear after answer",
    )
    parser.add_argument(
        "--screenshot",
        help="optional screenshot path captured while video is flowing",
    )
    parser.add_argument(
        "--reload-during-ring",
        action="store_true",
        help="reload the HA page while the outbound call is ringing",
    )
    parser.add_argument(
        "--reload-in-call",
        action="store_true",
        help="reload the HA page after the audio/video dialog is connected",
    )
    parser.add_argument(
        "--startup-settle-seconds",
        type=float,
        default=10.0,
        help=(
            "wait for Home Assistant startup/auth resource reloads before "
            "starting the call (default: 10 seconds)"
        ),
    )
    parser.add_argument(
        "--auth-check-only",
        action="store_true",
        help="verify authenticated dashboard/card access without starting a call",
    )
    parser.add_argument(
        "--outbound",
        metavar="DESTINATION",
        help="originate from the card instead of waiting for an incoming call",
    )
    parser.add_argument(
        "--cancel-during-ring",
        action="store_true",
        help="cancel an outbound INVITE before the remote endpoint answers",
    )
    parser.add_argument(
        "--out",
        default="/tmp/sip_video_browser_probe.json",
    )
    args = parser.parse_args()
    if args.expect_audio_only and (args.send_camera or args.deny_camera):
        parser.error("--expect-audio-only cannot be combined with camera options")
    if args.expect_native_camera and not args.expect_audio_only:
        parser.error("--expect-native-camera requires --expect-audio-only")
    if args.expect_remote_hangup and args.no_hangup:
        parser.error("--expect-remote-hangup cannot be combined with --no-hangup")
    if args.cancel_during_ring and not args.outbound:
        parser.error("--cancel-during-ring requires --outbound")
    if args.deny_camera:
        args.send_camera = True
    storage_state = (
        Path(args.storage_state).expanduser() if args.storage_state else None
    )
    if storage_state is not None and not storage_state.is_file():
        parser.error(f"Playwright storage state does not exist: {storage_state}")
    try:
        args.url = _dashboard_url(args.url, storage_state, args.dashboard_path)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        parser.error(f"Cannot resolve Playwright Home Assistant origin: {err}")
    if not args.url:
        parser.error("--url or an authenticated --storage-state is required")
    parsed_url = urlsplit(args.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        parser.error("--url must be an absolute HTTP(S) Home Assistant URL")
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    context_options: dict = {}
    if storage_state is not None:
        if args.refresh_credentials:
            try:
                refresh_playwright_auth(
                    token_url=str(args.refresh_url),
                    credentials_path=Path(args.refresh_credentials).expanduser(),
                    storage_path=storage_state,
                    storage_hass_url=origin,
                )
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as err:
                parser.error(f"Cannot refresh Playwright authentication: {err}")
        try:
            playwright_storage_origin(storage_state, preferred_url=origin)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            parser.error(
                f"Playwright storage state is incompatible with {origin}: {err}"
            )
        context_options["storage_state"] = str(storage_state)
    else:
        from ha_playwright_auth import context_kwargs

        context_options.update(context_kwargs())

    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ModuleNotFoundError:
        parser.error(
            "Playwright is required to run the browser probe; install the "
            "qualification dependencies first"
        )

    console: list[str] = []
    result: dict = {
        "samples": [],
        "console": console,
        "audio_websockets": [],
        "document_events": [],
        "websocket_events": [],
    }
    failure: BaseException | None = None
    with sync_playwright() as playwright:
        launch_options = {
            "headless": True,
            "args": [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                f"--unsafely-treat-insecure-origin-as-secure={origin}",
            ],
        }
        if args.chromium:
            launch_options["executable_path"] = args.chromium
        browser = playwright.chromium.launch(
            **launch_options,
        )
        context = browser.new_context(
            **context_options,
            viewport={"width": args.viewport_width, "height": args.viewport_height},
        )
        # Lovelace can perform a same-view document reload while applying a
        # freshly versioned custom-card resource. Install the card locator in
        # every document so media qualification resumes after that handoff
        # without polling the full shadow DOM during steady state.
        context.add_init_script(f"({DEEP_CARD})()")
        context.add_init_script(f"({INSTALL_RESPONSIVENESS_MONITOR})()")
        context.grant_permissions(["camera", "microphone"], origin=origin)
        if args.deny_camera:
            context.add_init_script(
                """
                (() => {
                  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                  navigator.mediaDevices.getUserMedia = (constraints = {}) => {
                    if (constraints && constraints.video) {
                      return Promise.reject(new DOMException(
                        "Camera permission denied by qualification probe",
                        "NotAllowedError",
                      ));
                    }
                    return original(constraints);
                  };
                })();
                """
            )
        page = context.new_page()
        browser_started = time.monotonic()

        def record_document_event(kind: str, url: str = "") -> None:
            result["document_events"].append(
                {
                    "at_s": round(time.monotonic() - browser_started, 3),
                    "kind": kind,
                    "url": str(url or "").split("?", 1)[0],
                }
            )

        page.on(
            "console",
            lambda msg: console.append(
                f"{time.monotonic() - browser_started:.3f}s "
                f"{msg.type}: {_sanitise_browser_message(msg.text)}"
            ),
        )
        page.on(
            "pageerror",
            lambda error: console.append(
                f"{time.monotonic() - browser_started:.3f}s "
                f"pageerror: {_sanitise_browser_message(error)}"
            ),
        )
        page.on(
            "framenavigated",
            lambda frame: (
                record_document_event("navigated", frame.url)
                if frame == page.main_frame
                else None
            ),
        )
        page.on(
            "domcontentloaded",
            lambda: record_document_event("domcontentloaded", page.url),
        )
        page.on("load", lambda: record_document_event("load", page.url))
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        websocket_urls: dict[str, str] = {}

        def record_websocket_created(event: dict) -> None:
            url = str(event.get("url") or "")
            request_id = str(event.get("requestId") or "")
            if "/api/voip_stack/" not in url:
                return
            websocket_urls[request_id] = url
            result["websocket_events"].append(
                {
                    "at_s": round(time.monotonic() - browser_started, 3),
                    "kind": "created",
                    "request_id": request_id,
                    "url": _sanitise_browser_message(url),
                }
            )
            if "/api/voip_stack/ws?" not in url:
                return
            initiator = event.get("initiator") or {}
            frames = ((initiator.get("stack") or {}).get("callFrames") or [])
            result["audio_websockets"].append(
                {
                    "at_s": round(time.monotonic() - browser_started, 3),
                    "url": _sanitise_browser_message(url),
                    "stack": [
                        {
                            "function": str(frame.get("functionName") or ""),
                            "url": str(frame.get("url") or "").split("?", 1)[0],
                            "line": int(frame.get("lineNumber") or 0),
                            "column": int(frame.get("columnNumber") or 0),
                        }
                        for frame in frames[:12]
                    ],
                }
            )

        def record_websocket_closed(event: dict) -> None:
            request_id = str(event.get("requestId") or "")
            url = websocket_urls.pop(request_id, "")
            if not url:
                return
            result["websocket_events"].append(
                {
                    "at_s": round(time.monotonic() - browser_started, 3),
                    "kind": "closed",
                    "request_id": request_id,
                    "url": _sanitise_browser_message(url),
                }
            )

        def record_websocket_error(event: dict) -> None:
            request_id = str(event.get("requestId") or "")
            url = websocket_urls.get(request_id, "")
            if not url:
                return
            result["websocket_events"].append(
                {
                    "at_s": round(time.monotonic() - browser_started, 3),
                    "kind": "error",
                    "request_id": request_id,
                    "url": _sanitise_browser_message(url),
                    "error": _sanitise_browser_message(
                        event.get("errorMessage") or ""
                    ),
                }
            )

        cdp.on("Network.webSocketCreated", record_websocket_created)
        cdp.on("Network.webSocketClosed", record_websocket_closed)
        cdp.on("Network.webSocketFrameError", record_websocket_error)
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        if _is_ha_auth_url(page.url):
            raise RuntimeError(
                "Home Assistant redirected Playwright to OAuth after the "
                "automatic token refresh; the stored refresh token or "
                "dashboard origin is no longer valid"
            )
        page.wait_for_timeout(max(0, int(args.startup_settle_seconds * 1000)))
        try:
            page.wait_for_function(
                f"() => Boolean(({DEEP_CARD})())",
                timeout=15_000,
                polling=100,
            )
        except PlaywrightTimeoutError:
            # A lab browser can reach Lovelace during the short interval in
            # which HA is already serving HTTP but the custom-card resource
            # has not yet been registered. Reload once after integration
            # startup instead of turning that harmless race into a false
            # qualification failure.
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_function(
                    f"() => Boolean(({DEEP_CARD})())",
                    timeout=45_000,
                    polling=100,
                )
            except PlaywrightTimeoutError as err:
                card_count = page.locator("voip-stack-card, intercom-card").count()
                raise RuntimeError(
                    f"VoIP card not found at {page.url!r}: pass the full "
                    "Lovelace dashboard path, not the HA root "
                    f"(card_count={card_count}, "
                    f"console_tail={console[-10:]!r})"
                ) from err

        monitor_active = False

        def start_responsiveness_monitor() -> None:
            nonlocal monitor_active
            page.evaluate(INSTALL_RESPONSIVENESS_MONITOR)
            monitor_active = True

        def finish_responsiveness_monitor(label: str) -> dict:
            nonlocal monitor_active
            if not monitor_active:
                return {}
            metrics = page.evaluate(READ_RESPONSIVENESS_MONITOR) or {}
            metrics["label"] = label
            result.setdefault("responsiveness_segments", []).append(metrics)
            result["responsiveness"] = metrics
            page.evaluate(STOP_RESPONSIVENESS_MONITOR)
            monitor_active = False
            return metrics

        start_responsiveness_monitor()

        def sample(label: str) -> dict:
            for attempt in range(3):
                try:
                    item = page.evaluate(CARD_SAMPLE) or {}
                    item["backend_state"] = page.evaluate(BACKEND_SAMPLE) or {}
                    break
                except PlaywrightError as err:
                    if (
                        "Execution context was destroyed" not in str(err)
                        or attempt == 2
                    ):
                        raise
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    page.wait_for_function(
                        f"() => Boolean(({DEEP_CARD})())",
                        timeout=30_000,
                        polling=100,
                    )
                    page.evaluate(INSTALL_RESPONSIVENESS_MONITOR)
            item["label"] = label
            result["samples"].append(item)
            print(json.dumps(item, separators=(",", ":")), flush=True)
            return item

        if args.auth_check_only:
            sample("authenticated_card_ready")
            result["ok"] = True
            finish_responsiveness_monitor("auth_check")
            Path(args.out).write_text(
                json.dumps(result, indent=2, ensure_ascii=False)
            )
            context.close()
            browser.close()
            return 0

        def wait_for_idle_cleanup(
            label: str,
            *,
            hangup_started: float | None = None,
            hangup_click: dict | None = None,
        ) -> dict:
            page.wait_for_function(
                f"""() => {{
                  const x = ({LIGHT_CARD_STATE})();
                  if (!x || String(x.card_state || '').toLowerCase() !== 'idle') return false;
                  if (x.engine_call_id || x.engine_video_active || x.engine_video_visible) return false;
                  if (x.has_audio_attach_task || x.has_cleanup_task) return false;
                  return true;
                }}""",
                timeout=15_000,
                polling=100,
            )
            hangup_timing = None
            if hangup_started is not None:
                hangup_timing = {
                    "label": label,
                    "dispatch_ms": float((hangup_click or {}).get("dispatch_ms") or 0),
                    "to_ui_idle_ms": (time.monotonic() - hangup_started) * 1000,
                }
                result.setdefault("hangup_timings", []).append(hangup_timing)
            deadline = time.monotonic() + 15
            backend = {}
            while time.monotonic() < deadline:
                backend = page.evaluate(BACKEND_SAMPLE) or {}
                if not backend.get("debug_mode"):
                    break
                debug = backend.get("media_debug") or {}
                registry = debug.get("call_registry") or {}
                if (
                    int(registry.get("sessions") or 0) == 0
                    and int(registry.get("active_sessions") or 0) == 0
                    and not registry.get("pending_call_ids")
                    and not registry.get("media_call_ids")
                    and not registry.get("bridge_call_ids")
                    and not debug.get("audio_ws_owner_call_ids")
                    and not debug.get("video_ws_owner_call_ids")
                    and not debug.get("video_transcoder_call_id")
                ):
                    break
                page.wait_for_timeout(100)
            else:
                raise RuntimeError(f"backend resources survived teardown: {backend}")
            if hangup_timing is not None:
                hangup_timing["to_backend_cleanup_ms"] = (
                    time.monotonic() - hangup_started
                ) * 1000
            cleaned = sample(label)
            backend = cleaned.get("backend_state") or {}
            if backend.get("pending_transactions") or backend.get("active_dialogs"):
                raise RuntimeError(f"SIP transactions or dialogs survived teardown: {cleaned}")
            if backend.get("pending_call_ids") or backend.get("active_call_ids"):
                raise RuntimeError(f"SIP call ids survived teardown: {cleaned}")
            return cleaned

        def click_hangup_and_wait(label: str, unavailable: str) -> dict:
            started = time.monotonic()
            clicked = page.evaluate(CLICK_HANGUP)
            if not clicked or not clicked.get("clicked"):
                raise RuntimeError(unavailable)
            return wait_for_idle_cleanup(
                label,
                hangup_started=started,
                hangup_click=clicked,
            )

        try:
            reload_rendered = 0
            if args.send_camera:
                page.wait_for_function(
                    f"() => Boolean(({DEEP_CARD})()?._softphoneSnapshot?.video_camera_send_enabled)",
                    timeout=10_000,
                    polling=100,
                )
                if not page.evaluate(CLICK_CAMERA_SEND):
                    raise RuntimeError("Send Camera option was not available before the video call")
            sample("ready")
            if args.outbound:
                print(f"PLACING_VIDEO_CALL {args.outbound}", flush=True)
                if not page.evaluate(START_OUTBOUND, args.outbound):
                    raise RuntimeError("HA softphone card could not start the outbound call")
                outbound_states = (
                    "['remote_ringing']"
                    if args.cancel_during_ring
                    else "['calling','connecting','remote_ringing','in_call']"
                )
                page.wait_for_function(
                    f"() => {outbound_states}.includes((({LIGHT_CARD_STATE})()?.card_state || '').toLowerCase())",
                    timeout=15_000,
                    polling=100,
                )
                sample("outbound_progress")
                if args.reload_during_ring:
                    finish_responsiveness_monitor("before_reload_during_ring")
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_function(
                        f"() => Boolean(({DEEP_CARD})())",
                        timeout=30_000,
                        polling=100,
                    )
                    start_responsiveness_monitor()
                    page.wait_for_function(
                        f"() => ['calling','connecting','remote_ringing','in_call'].includes((({LIGHT_CARD_STATE})()?.card_state || '').toLowerCase())",
                        timeout=15_000,
                        polling=100,
                    )
                    sample("outbound_after_reload")
                if args.cancel_during_ring:
                    click_hangup_and_wait(
                        "idle_after_outbound_cancel",
                        "outbound Hangup button was unavailable during ringing",
                    )
                    result["ok"] = True
                    raise _ProbeComplete
                print("WAITING_FOR_REMOTE_ANSWER", flush=True)
            else:
                print("READY_FOR_VIDEO_CALL", flush=True)
                page.wait_for_function(
                    f"() => ['ringing','in_call'].includes((({LIGHT_CARD_STATE})()?.card_state || '').toLowerCase())",
                    timeout=int(args.ring_timeout * 1000),
                    polling=100,
                )
                incoming = sample("incoming_progress")
                if (
                    not args.expect_audio_only
                    and not args.expect_video_reinvite
                    and not incoming.get("video_offered")
                ):
                    raise RuntimeError(
                        f"incoming call did not offer video: {incoming}"
                    )
                # An enabled HA softphone auto-answer may cross ringing and
                # reach in_call between two 100 ms observations. That is a
                # valid completed answer, not a missing Answer button.
                if str(incoming.get("card_state") or "").lower() == "ringing":
                    if not page.evaluate(CLICK_ANSWER):
                        try:
                            page.wait_for_function(
                                f"() => ['answering','connecting','in_call'].includes((({LIGHT_CARD_STATE})()?.card_state || '').toLowerCase())",
                                timeout=2_000,
                                polling=100,
                            )
                        except PlaywrightTimeoutError as err:
                            raise RuntimeError(
                                "visible Answer button not found and auto-answer did not advance"
                            ) from err
            page.wait_for_function(
                f"""() => {{
                  const state = String((({LIGHT_CARD_STATE})()?.card_state || '')).toLowerCase();
                  return ['in_call','idle','busy','declined','cancelled','media_incompatible','transport_unreachable','auth_required_unsupported','protocol_error','error'].includes(state);
                }}""",
                timeout=int(args.ring_timeout * 1000),
                polling=100,
            )
            connected = sample("in_call")
            if str(connected.get("card_state") or "").lower() != "in_call":
                raise RuntimeError(
                    "call terminated before connection: "
                    f"state={connected.get('card_state')!r} "
                    f"reason={connected.get('terminal_reason')!r} "
                    f"error={connected.get('card_error')!r}"
                )
            if args.reload_in_call:
                finish_responsiveness_monitor("before_reload_in_call")
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(
                    f"() => Boolean(({DEEP_CARD})())",
                    timeout=30_000,
                    polling=100,
                )
                start_responsiveness_monitor()
                page.wait_for_function(
                    f"() => (({LIGHT_CARD_STATE})()?.card_state || '').toLowerCase() === 'in_call'",
                    timeout=15_000,
                    polling=100,
                )
                after_reload = sample("in_call_after_reload")
                reload_rendered = int(
                    ((after_reload.get("engine_stats") or {}).get("video") or {}).get(
                        "rendered", 0
                    )
                )
            if args.expect_audio_only:
                page.wait_for_function(
                    f"() => {{ const x = ({LIGHT_CARD_STATE})(); const s = x?.engine_stats || {{}}; return s.sent > 0 && s.received > 0 && !x?.video_active && !x?.engine_video_active; }}",
                    timeout=int(args.video_timeout * 1000),
                    polling=100,
                )
                if args.expect_native_camera:
                    page.wait_for_function(
                        f"""() => {{
                          const card = ({LIGHT_CARD_STATE})();
                          const controller = globalThis.__voipStackProbeCard
                            || globalThis.__voipStackProbeFindCard?.()
                            || null;
                          return Boolean(
                            card?.card_state === 'in_call'
                            && String(controller?._nativeCameraEntityId || '').startsWith('camera.')
                            && controller?._nativeCameraCard
                            && !controller?._els?.nativeCameraHost?.hidden
                          );
                        }}""",
                        timeout=int(args.video_timeout * 1000),
                        polling=100,
                    )
            else:
                page.wait_for_function(
                    f"() => {{ const x = ({LIGHT_CARD_STATE})(); const d = String(x?.video_direction || 'sendrecv'); const v = x?.engine_stats?.video || {{}}; const rx = !['recvonly','sendrecv'].includes(d) || (v.received > 0 && (v.rendered > 0 || x?.engine_video_visible)); const tx = {str(args.deny_camera).lower()} || !['sendonly','sendrecv'].includes(d) || v.sent > 0; return x?.engine_video_active && rx && tx; }}",
                    timeout=int(args.video_timeout * 1000),
                    polling=100,
                )
            if args.sample_interval > 0:
                deadline = time.monotonic() + args.hold_seconds
                sample_number = 0
                while (remaining := deadline - time.monotonic()) > 0:
                    page.wait_for_timeout(int(min(args.sample_interval, remaining) * 1000))
                    sample_number += 1
                    sample(f"hold_{sample_number:03d}")
            else:
                page.wait_for_timeout(int(args.hold_seconds * 1000))
            active = sample("video_flowing")
            engine_stats = active.get("engine_stats") or {}
            video_stats = engine_stats.get("video") or {}
            if engine_stats.get("tx_dropped", 0) != 0:
                raise RuntimeError(f"browser audio TX dropped frames: {active}")
            if engine_stats.get("frames_drop", 0) != 0:
                raise RuntimeError(f"browser audio playout dropped frames: {active}")
            if engine_stats.get("underruns", 0) != 0:
                raise RuntimeError(f"browser audio playout underrun: {active}")
            if args.expect_audio_only:
                if active.get("video_active") or active.get("engine_video_active"):
                    raise RuntimeError(f"audio-only call unexpectedly attached video: {active}")
                if engine_stats.get("sent", 0) <= 0:
                    raise RuntimeError(f"audio-only call did not transmit browser audio: {active}")
                if engine_stats.get("received", 0) <= 0:
                    raise RuntimeError(f"audio-only call did not receive SIP audio: {active}")
                if args.expect_native_camera:
                    native_camera = active.get("native_camera") or {}
                    if (
                        not str(native_camera.get("entity_id") or "").startswith("camera.")
                        or native_camera.get("host_hidden")
                        or not native_camera.get("mounted")
                        or not native_camera.get("media_ready")
                    ):
                        raise RuntimeError(
                            f"native ESPHome camera was not mounted by the HA card: {active}"
                        )
            direction = str(active.get("video_direction") or "sendrecv")
            expects_receive = direction in {"recvonly", "sendrecv"}
            expects_send = direction in {"sendonly", "sendrecv"}
            if not args.expect_audio_only and expects_receive and video_stats.get("received", 0) <= 0:
                raise RuntimeError(f"no remote video access units reached WebCodecs: {active}")
            if (
                not args.expect_audio_only
                and expects_send
                and not args.deny_camera
                and video_stats.get("sent", 0) <= 0
            ):
                raise RuntimeError(f"no browser video access units returned to SIP: {active}")
            if args.deny_camera and video_stats.get("sent", 0) != 0:
                raise RuntimeError(f"camera denial still transmitted video: {active}")
            if (
                args.reload_in_call
                and not args.expect_audio_only
                and expects_receive
                and video_stats.get("rendered", 0)
                < reload_rendered + 3
            ):
                raise RuntimeError(
                    f"video did not continue rendering after page reload: {active}"
                )
            if (
                not args.expect_audio_only
                and expects_receive
                and not args.allow_dark_video
                and not (active.get("canvas") or {}).get("non_black")
            ):
                raise RuntimeError(f"decoded canvas has no non-black sample: {active}")
            if not args.expect_audio_only:
                layout = active.get("layout") or {}
                surface = layout.get("surface") or {}
                canvas_layout = layout.get("canvas") or {}
                hangup_layout = layout.get("hangup") or {}
                if layout.get("horizontal_overflow"):
                    raise RuntimeError(f"video card has horizontal overflow: {active}")
                if layout.get("header_stats_overlap"):
                    raise RuntimeError(f"video debug overlay covers the card title: {active}")
                if layout.get("stats_outside_hangup"):
                    raise RuntimeError(f"video diagnostics escape the hangup bar: {active}")
                if abs(float(canvas_layout.get("width", 0)) - float(surface.get("width", 0))) > 2:
                    raise RuntimeError(f"video canvas does not fill card width: {active}")
                if abs(float(canvas_layout.get("height", 0)) - float(surface.get("height", 0))) > 2:
                    raise RuntimeError(f"video canvas does not fill card height: {active}")
                if abs(float(hangup_layout.get("bottom", 0)) - float(surface.get("height", 0))) > 2:
                    raise RuntimeError(f"video hangup bar is not bottom-aligned: {active}")
                if abs(float(hangup_layout.get("width", 0)) - float(surface.get("width", 0))) > 2:
                    raise RuntimeError(f"video hangup bar does not span card width: {active}")
                if not 48 <= float(hangup_layout.get("height", 0)) <= 84:
                    raise RuntimeError(f"video hangup bar has an unusable height: {active}")
                if float(layout.get("usable_video_height", 0)) < min(
                    80,
                    float(surface.get("height", 0)) * 0.25,
                ):
                    raise RuntimeError(f"video overlays leave too little visible video: {active}")
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            if args.expect_remote_hangup:
                wait_for_idle_cleanup("idle_after_remote_hangup")
            elif not args.no_hangup:
                click_hangup_and_wait(
                    "idle_after_hangup",
                    "visible Hangup button not found",
                )
            result["ok"] = True
        except _ProbeComplete:
            pass
        except BaseException as err:  # Persist browser evidence before re-raising.
            failure = err
            result["ok"] = False
            result["error"] = f"{type(err).__name__}: {err}"
            try:
                sample("failure")
            except BaseException:
                pass
        finally:
            if failure is not None:
                try:
                    state = page.evaluate(LIGHT_CARD_STATE) or {}
                    if str(state.get("card_state") or "").lower() != "idle":
                        click_hangup_and_wait(
                            "idle_after_failure_cleanup",
                            "failure cleanup could not find the Hangup button",
                        )
                except BaseException as cleanup_error:
                    result["failure_cleanup_error"] = (
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            try:
                finish_responsiveness_monitor("final")
            except BaseException as monitor_error:
                result["responsiveness_error"] = (
                    f"{type(monitor_error).__name__}: {monitor_error}"
                )
            Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
            for line in console:
                print(f"BROWSER {line}", flush=True)
            context.close()
            browser.close()
    if failure is not None:
        raise failure
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
