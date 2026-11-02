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
# HA_LAB_SERVICE selects the existing systemd owner, including its restart policy.
lab_service=${HA_LAB_SERVICE:-home-assistant-voip-lab.service}
ha_url=${HA_LAB_URL:-http://127.0.0.1:18123}
sip_ready_host=${HA_LAB_SIP_READY_HOST:-127.0.0.1}
sip_ready_port=${HA_LAB_SIP_READY_PORT:-15060}
service_control=(systemctl)
if ((EUID != 0)); then
  service_control=(sudo -n systemctl)
fi

if [[ ${1:-} != -- || $# -lt 2 ]]; then
  echo "usage: $0 -- command [args...] (HA_LAB_SERVICE selects the existing systemd unit)" >&2
  exit 2
fi
shift

for path in "$candidate_component" "$candidate_package" "$package_file"; do
  [[ -e "$path" ]] || { echo "missing required path: $path" >&2; exit 2; }
done
[[ -L "$component_link" ]] || {
  echo "refusing to replace non-symlink component: $component_link" >&2
  exit 2
}
[[ -f "$package_file" && ! -L "$package_file" ]] || {
  echo "refusing to replace non-regular package: $package_file" >&2
  exit 2
}
[[ "$lab_service" =~ ^[[:alnum:]_.:@-]+[.]service$ ]] || {
  echo "HA_LAB_SERVICE must name one existing systemd service" >&2
  exit 2
}
load_state=$(systemctl show --property=LoadState --value -- "$lab_service")
[[ "$load_state" == loaded ]] || {
  echo "Home Assistant lab service is not loaded: $lab_service ($load_state)" >&2
  exit 2
}

mkdir -p "$(dirname "$lock_file")"
exec 9>"$lock_file"
flock -n 9 || { echo "Home Assistant lab is already reserved" >&2; exit 2; }

original_state=$(systemctl show --property=ActiveState --value -- "$lab_service")
case "$original_state" in
  active) original_active=1 ;;
  inactive|failed) original_active=0 ;;
  *) echo "Home Assistant lab service is changing state: $original_state" >&2; exit 2 ;;
esac
original_component=$(readlink -- "$component_link")
work_dir=$(mktemp -d "${component_link%/*}/.voip-stack-candidate.XXXXXX")
package_backup="$work_dir/package.original"
files_changed=0
service_touched=0
candidate_started=0
candidate_pid=
service_stopped=0

wait_ready() {
  for _ in $(seq 1 120); do
    if curl -fsS --max-time 2 "$ha_url/" >/dev/null 2>&1 && \
      nc -z "$sip_ready_host" "$sip_ready_port" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "Home Assistant lab did not become ready" >&2
  return 1
}

stop_lab() {
  local status=0 state
  service_stopped=0
  "${service_control[@]}" stop -- "$lab_service" 9>&- || status=1
  state=$(systemctl show --property=ActiveState --value -- "$lab_service") || return 1
  case "$state" in
    inactive|failed) service_stopped=1 ;;
    *) echo "Home Assistant lab service did not stop: $state" >&2; return 1 ;;
  esac
  return "$status"
}

restore_lab() {
  local status=$? cleanup_failed=0 originals_ready=1 state
  trap - EXIT
  trap '' INT TERM
  set +e
  if ((files_changed || candidate_started)); then
    stop_lab || cleanup_failed=1
    if ((service_stopped)); then
      mv -Tf -- "$work_dir/component.original" "$component_link" || { cleanup_failed=1; originals_ready=0; }
      cp -p -- "$package_backup" "$package_file" || { cleanup_failed=1; originals_ready=0; }
    else
      originals_ready=0
      echo "Lab restoration refused while the candidate service still owns its files" >&2
    fi
  fi
  if ((service_touched && originals_ready && original_active)); then
    state=$(systemctl show --property=ActiveState --value -- "$lab_service") || cleanup_failed=1
    if [[ "$state" != active ]]; then
      if "${service_control[@]}" start -- "$lab_service" 9>&-; then
        wait_ready || cleanup_failed=1
      else
        cleanup_failed=1
      fi
    fi
  fi
  if ((originals_ready)); then
    rm -rf -- "$work_dir" || cleanup_failed=1
  else
    echo "Original lab files preserved for recovery in $work_dir" >&2
    cleanup_failed=1
  fi
  ((cleanup_failed == 0)) || status=1
  exit "$status"
}

on_signal() {
  local status=$1
  if [[ -n "$candidate_pid" ]]; then
    # Only this command child belongs to the wrapper; systemd owns the service.
    kill -TERM "$candidate_pid" 2>/dev/null || true
    wait "$candidate_pid" 2>/dev/null || true
    candidate_pid=
  fi
  exit "$status"
}
trap restore_lab EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

cp -p -- "$package_file" "$package_backup"
ln -s -- "$original_component" "$work_dir/component.original"
ln -s -- "$candidate_component" "$work_dir/component.candidate"
service_touched=1
stop_lab
files_changed=1
mv -Tf -- "$work_dir/component.candidate" "$component_link"
install -m 0644 "$candidate_package" "$package_file"
candidate_started=1
"${service_control[@]}" start -- "$lab_service" 9>&-
wait_ready

"$@" 9>&- &
candidate_pid=$!
command_status=0
wait "$candidate_pid" || command_status=$?
candidate_pid=
exit "$command_status"
