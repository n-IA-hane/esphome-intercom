#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET_HOST=${SIPP_TARGET_HOST:-127.0.0.1}
TARGET_PORT=${SIPP_TARGET_PORT:-15060}
LOCAL_HOST=${SIPP_LOCAL_HOST:-127.0.0.1}
LOCAL_PORT=${SIPP_LOCAL_PORT:-16060}
TARGET_EXTENSION=${SIPP_TARGET_EXTENSION:-2600}
CAPTURE_DIR=${SIPP_CAPTURE_DIR:-$ROOT/test_captures/sipp-lab}

command -v sipp >/dev/null || {
  printf '%s\n' "SIPp is required"
  exit 2
}

mkdir -p "$CAPTURE_DIR"
cd "$CAPTURE_DIR"

exec sipp "$TARGET_HOST:$TARGET_PORT" \
  -sf "$ROOT/tests/sipp/inbound-cancel.xml" \
  -s "$TARGET_EXTENSION" \
  -i "$LOCAL_HOST" \
  -p "$LOCAL_PORT" \
  -m 1 \
  -r 1 \
  -rp 1000 \
  -timeout 10s \
  -trace_msg \
  -trace_err \
  -trace_stat
