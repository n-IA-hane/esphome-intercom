#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCENARIO="all"
REPEAT=1
SEED=""
MODE="all"
SOCKET="${SIM_SOCKET:-test_captures/simulator/voip-sim.sock}"
TRACE_DIR=""
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --changed)
      MODE="changed"
      shift
      ;;
    --all)
      MODE="all"
      shift
      ;;
    --scenario)
      SCENARIO="${2:?missing scenario name}"
      shift 2
      ;;
    --repeat)
      REPEAT="${2:?missing repeat count}"
      shift 2
      ;;
    --seed)
      SEED="${2:?missing seed}"
      shift 2
      ;;
    --socket)
      SOCKET="${2:?missing socket path}"
      shift 2
      ;;
    --trace-dir)
      TRACE_DIR="${2:?missing trace dir}"
      shift 2
      ;;
    --contract)
      # Kept as a no-op for old local command lines. The runner now always
      # uses the deterministic contract simulator.
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p test_captures/simulator

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python executable not found: $PYTHON_BIN" >&2
  exit 2
fi

"$PYTHON_BIN" tools/simulator/simctl.py --socket "$SOCKET" doctor
"$PYTHON_BIN" tools/simulator/scenario_runner.py "$SCENARIO" --validate-only

CONTRACT_PID=""
rm -f "$SOCKET"
"$PYTHON_BIN" tools/simulator/contract_simulator.py --socket "$SOCKET" &
CONTRACT_PID="$!"
for _ in $(seq 1 50); do
  [[ -S "$SOCKET" ]] && break
  sleep 0.1
done
trap '[[ -n "$CONTRACT_PID" ]] && "$PYTHON_BIN" tools/simulator/simctl.py --socket "$SOCKET" shutdown >/dev/null 2>&1 || true; [[ -n "$CONTRACT_PID" ]] && wait "$CONTRACT_PID" >/dev/null 2>&1 || true' EXIT

if [[ ! -S "$SOCKET" ]]; then
  echo "virtual device is not running: $SOCKET" >&2
  echo "contract simulator did not expose the JSON-RPC socket." >&2
  exit 2
fi

args=(tools/simulator/scenario_runner.py "$SCENARIO" --socket "$SOCKET" --repeat "$REPEAT")
if [[ -n "$SEED" ]]; then
  args+=(--seed "$SEED")
fi
if [[ -n "$TRACE_DIR" ]]; then
  args+=(--trace-dir "$TRACE_DIR")
fi

echo "mode=$MODE scenario=$SCENARIO repeat=$REPEAT seed=${SEED:-none}"
"$PYTHON_BIN" "${args[@]}"
