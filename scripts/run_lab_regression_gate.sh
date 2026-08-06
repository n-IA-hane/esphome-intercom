#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LAB_ROOT=${HA_VOIP_LAB_ROOT:-/home/codex/ha-voip-lab}
LAB_HOST=${HA_VOIP_LAB_HOST:-192.168.1.48}
CAPTURE_DIR=${SIP_REGRESSION_CAPTURE_DIR:-$ROOT/test_captures/lab-regression-gate}
STORAGE_STATE=${PLAYWRIGHT_STORAGE_STATE:-$LAB_ROOT/playwright-storage.json}
CREDENTIALS=${HA_PLAYWRIGHT_REFRESH_CREDENTIALS:-$LAB_ROOT/.credentials}
SINK_PID=

cleanup() {
  if [[ -n "$SINK_PID" ]] && kill -0 "$SINK_PID" 2>/dev/null; then
    kill "$SINK_PID"
    wait "$SINK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

cd "$ROOT"
mkdir -p "$CAPTURE_DIR"

if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  printf '%s\n' "The live regression gate requires a clean candidate worktree"
  exit 2
fi

printf 'candidate=%s\n' "$(git rev-parse HEAD)"

.venv/bin/python scripts/run_answered_sipp_lab.py
.venv/bin/python scripts/check_live_qualification.py \
  test_captures/sipp-answered-lab/summary.json \
  --require caller_bye \
  --require callee_bye

SIPP_CAPTURE_DIR="$CAPTURE_DIR/cancel" ./scripts/run_sipp_lab.sh >/dev/null

PLAYWRIGHT_STORAGE_STATE="$STORAGE_STATE" \
HA_PLAYWRIGHT_REFRESH_CREDENTIALS="$CREDENTIALS" \
LOCAL_SIP_TARGET="sip:Casa@$LAB_HOST:15060;transport=tcp" \
  .venv/bin/python tools/ha_softphone_matrix.py \
    --out "$CAPTURE_DIR/dtmf.json" \
    --only in_call_registered_sip_info_dtmf_event \
    --only in_call_rfc4733_dtmf_event \
    --only outbound_sip_info_dtmf_event \
    --only outbound_rfc4733_dtmf_event \
    --only outbound_rfc4733_dtmf_keypad >/dev/null

(
  cd "$CAPTURE_DIR"
  exec baresip -f "$LAB_ROOT/baresip-sink"
) >"$CAPTURE_DIR/baresip-sink.log" 2>&1 &
SINK_PID=$!
sleep 1
kill -0 "$SINK_PID"

.venv/bin/python tools/sip_video_peer.py \
  --host "$LAB_HOST" \
  --port 15060 \
  --target 2502 \
  --local-ip "$LAB_HOST" \
  --codec vp8 \
  --direction sendrecv \
  --add-video-after 1 \
  --expect-reinvite-status 200 \
  --duration 4 \
  --out "$CAPTURE_DIR/video-reinvite.json" >/dev/null

cleanup
SINK_PID=
.venv/bin/python scripts/run_answered_sipp_lab.py --quiescence-only >/dev/null
printf '%s\n' "lab_regression_gate=passed"
