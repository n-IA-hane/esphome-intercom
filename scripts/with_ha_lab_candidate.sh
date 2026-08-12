#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
lab_root=${HA_LAB_ROOT:-/home/codex/ha-voip-lab}
config_dir="$lab_root/config"
component_link="$config_dir/custom_components/voip_stack"
package_file="$config_dir/packages/voip_qualification.yaml"
candidate_component="$repo_root/custom_components/voip_stack"
candidate_package="$repo_root/qualification/home_assistant/voip_qualification.yaml"
lock_file=${HA_LAB_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/voip-ha-lab-v2.lock}
session_name=${HA_LAB_TMUX_SESSION:-ha-voip-lab}
ha_url=${HA_LAB_URL:-http://127.0.0.1:18123}
sip_ready_host=${HA_LAB_SIP_READY_HOST:-127.0.0.1}
sip_ready_port=${HA_LAB_SIP_READY_PORT:-15060}

if [[ ${1:-} != -- || $# -lt 2 ]]; then
  echo "usage: $0 -- command [args...]" >&2
  exit 2
fi
shift

for path in "$candidate_component" "$candidate_package" "$package_file" "$lab_root/.venv/bin/hass"; do
  [[ -e "$path" ]] || { echo "missing required path: $path" >&2; exit 2; }
done
[[ -L "$component_link" ]] || {
  echo "refusing to replace non-symlink component: $component_link" >&2
  exit 2
}

mkdir -p "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock -n 9 || { echo "Home Assistant lab is already reserved" >&2; exit 2; }

original_component=$(readlink "$component_link")
package_backup=$(mktemp)
cp "$package_file" "$package_backup"
restored=0

restart_ha() {
  tmux kill-session -t "$session_name" 2>/dev/null || true
  local -a pids=()
  mapfile -t pids < <(
    ps -eo pid=,comm=,args= | awk -v config="$config_dir" \
      'index($0, config) && ($2 == "hass" || $2 ~ /^python/) {print $1}'
  )
  if ((${#pids[@]})); then
    kill "${pids[@]}" 2>/dev/null || true
    for _ in $(seq 1 50); do
      mapfile -t pids < <(
        printf '%s\n' "${pids[@]}" |
          while read -r pid; do kill -0 "$pid" 2>/dev/null && echo "$pid"; done
      )
      ((${#pids[@]})) || break
      sleep 0.1
    done
    if ((${#pids[@]})); then
      kill -KILL "${pids[@]}" 2>/dev/null || true
    fi
  fi
  tmux new-session -d -s "$session_name" \
    "$lab_root/.venv/bin/hass -c $config_dir 2>&1 | tee -a $config_dir/home-assistant.log" \
    9>&-
  for _ in $(seq 1 120); do
    if curl -fsS "$ha_url/" >/dev/null 2>&1 && \
      nc -z "$sip_ready_host" "$sip_ready_port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "Home Assistant lab did not become ready" >&2
  return 1
}

restore_lab() {
  local status=$?
  trap - EXIT INT TERM
  if [[ $restored -eq 0 ]]; then
    restored=1
    ln -s "$original_component" "$component_link.restore"
    mv -Tf "$component_link.restore" "$component_link"
    cp "$package_backup" "$package_file"
    rm -f "$package_backup"
    restart_ha || status=1
  fi
  exit "$status"
}
trap restore_lab EXIT INT TERM

rm -f "$component_link.candidate" "$component_link.restore"
ln -s "$candidate_component" "$component_link.candidate"
mv -Tf "$component_link.candidate" "$component_link"
install -m 0644 "$candidate_package" "$package_file"
restart_ha
"$@"
