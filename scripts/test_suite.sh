#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-}
HA_PYTHON=${HA_PYTHON:-}
MODE=fast
KEEP_GOING=0
SEED=

usage() {
  printf '%s\n' \
    "Usage: scripts/test_suite.sh [fast|full|coverage|ha|browser|fault|mutation] [options]" \
    "" \
    "Options:" \
    "  --keep-going  run independent tests after a failure" \
    "  --seed N      set PYTHONHASHSEED and the Hypothesis seed" \
    "  -h, --help    show this help"
}

if [[ $# -gt 0 && ${1:0:1} != "-" ]]; then
  MODE=$1
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-going)
      KEEP_GOING=1
      shift
      ;;
    --seed)
      [[ $# -ge 2 ]] || { printf '%s\n' "Missing value for --seed" >&2; exit 2; }
      SEED=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z $PYTHON ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON=python
  fi
fi
if [[ $PYTHON != */* ]]; then
  PYTHON=$(command -v "$PYTHON" || true)
fi
[[ -x "$PYTHON" ]] || { printf 'Python not found: %s\n' "$PYTHON" >&2; exit 2; }

cd "$ROOT"
path_mode=$(
  ./scripts/yaml_paths.sh status |
    awk '$NF == "local" || $NF == "remote" { if (!mode) mode = $NF } END { print mode }'
)
[[ $path_mode == "local" || $path_mode == "remote" ]] || {
  printf '%s\n' "YAML paths are mixed or unknown" >&2
  exit 2
}

pytest_args=(tests -q --tb=short)
if [[ $KEEP_GOING -eq 1 ]]; then
  pytest_args+=(--maxfail=0)
fi
if [[ -n $SEED ]]; then
  [[ $SEED =~ ^[0-9]+$ ]] || { printf '%s\n' "Seed must be numeric" >&2; exit 2; }
  export PYTHONHASHSEED=$SEED
  pytest_args+=(--hypothesis-seed "$SEED")
fi

export YAML_PATH_MODE=$path_mode

case "$MODE" in
  fast)
    pytest_args+=(--ignore=tests/test_ha_integration_runtime.py)
    pytest_args+=(--ignore=tests/test_phone_control_ha.py)
    pytest_args+=(-m "not ha and not browser and not live and not mutation and not slow")
    ;;
  full)
    pytest_args+=(--ignore=tests/test_ha_integration_runtime.py)
    pytest_args+=(--ignore=tests/test_phone_control_ha.py)
    pytest_args+=(-m "not live and not mutation")
    ;;
  coverage)
    pytest_args+=(--ignore=tests/test_ha_integration_runtime.py)
    pytest_args+=(--ignore=tests/test_phone_control_ha.py)
    pytest_args+=(
      -m "not architecture and not ha and not live and not mutation"
      --cov=custom_components/voip_stack
      --cov-branch
      --cov-context=test
      --cov-report=term-missing:skip-covered
      --cov-report=xml
    )
    ;;
  ha)
    if [[ -z $HA_PYTHON ]]; then
      if [[ -x "$ROOT/../ha-voip-lab/.venv/bin/python" ]]; then
        HA_PYTHON="$ROOT/../ha-voip-lab/.venv/bin/python"
      else
        HA_PYTHON="$PYTHON"
      fi
    fi
    if [[ $HA_PYTHON != */* ]]; then
      HA_PYTHON=$(command -v "$HA_PYTHON" || true)
    fi
    [[ -x "$HA_PYTHON" ]] || {
      printf 'HA test Python not found: %s\n' "$HA_PYTHON" >&2
      exit 2
    }
    PYTHON=$HA_PYTHON
    pytest_args=(
      tests/test_ha_integration_runtime.py
      tests/test_phone_control_ha.py
      -q
      --tb=short
    )
    if [[ $KEEP_GOING -eq 1 ]]; then
      pytest_args+=(--maxfail=0)
    fi
    ;;
  browser)
    pytest_args+=(--ignore=tests/test_ha_integration_runtime.py)
    pytest_args+=(--ignore=tests/test_phone_control_ha.py)
    pytest_args+=(-m browser)
    ;;
  fault)
    pytest_args+=(--ignore=tests/test_ha_integration_runtime.py)
    pytest_args+=(--ignore=tests/test_phone_control_ha.py)
    pytest_args+=(-m fault)
    ;;
  mutation)
    git_dir=$(git rev-parse --path-format=absolute --git-dir)
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
    [[ $git_dir != "$common_dir" ]] || {
      printf '%s\n' "Mutation tests require a disposable git worktree" >&2
      exit 2
    }
    if [[ -n $HA_PYTHON ]]; then
      if [[ $HA_PYTHON != */* ]]; then
        HA_PYTHON=$(command -v "$HA_PYTHON" || true)
      fi
      [[ -x $HA_PYTHON ]] || {
        printf 'HA test Python not found: %s\n' "$HA_PYTHON" >&2
        exit 2
      }
      ha_site_packages=$(
        "$HA_PYTHON" -c \
          'import site; print(site.getsitepackages()[0])'
      )
      export PYTHONPATH="$ha_site_packages${PYTHONPATH:+:$PYTHONPATH}"
      export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
    fi
    "$PYTHON" -m mutmut run
    "$PYTHON" -m mutmut export-cicd-stats
    exec "$PYTHON" scripts/check_mutation_score.py \
      mutants/mutmut-cicd-stats.json
    ;;
  *)
    printf 'Unknown suite: %s\n' "$MODE" >&2
    usage >&2
    exit 2
    ;;
esac

printf 'suite=%s yaml_paths=%s fail_fast=%s seed=%s\n' \
  "$MODE" "$path_mode" "$((1 - KEEP_GOING))" "${SEED:-automatic}"
exec "$PYTHON" -m pytest "${pytest_args[@]}"
