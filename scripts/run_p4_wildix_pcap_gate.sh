#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out_dir=${1:?usage: run_p4_wildix_pcap_gate.sh OUT_DIR [runner arguments...]}
shift

ha_ssh=${HA_SSH_ALIAS:-hass}
p4_host=${P4_HOST:-192.168.1.57}
wildix_host=${WILDIX_HOST:-tecnodata2q23xb.wildixin.com}
wildix_ip=$(getent ahostsv4 "$wildix_host" | awk 'NR == 1 { print $1 }')
[[ -n $wildix_ip ]] || { printf '%s\n' "cannot resolve Wildix capture host" >&2; exit 2; }

mkdir -p "$out_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
remote_dir=/var/lib/hass/.cache/voip_stack_test_captures
remote_pcap="$remote_dir/p4-wildix-$stamp.pcap"
remote_log="$remote_dir/p4-wildix-$stamp.tcpdump.log"
local_pcap="$out_dir/ha-full.pcap"
capture_pid=

stop_capture() {
  if [[ -n $capture_pid ]]; then
    # shellcheck disable=SC2029 # PID is validated locally before remote use.
    ssh "$ha_ssh" "sudo -n kill -INT '$capture_pid' 2>/dev/null || true"
    capture_pid=
    sleep 1
  fi
}
trap stop_capture EXIT

capture_filter="(host $p4_host or host $wildix_ip) and (udp or tcp port 5060)"
# shellcheck disable=SC2029 # Paths and host addresses are local gate inputs.
capture_pid=$(ssh "$ha_ssh" \
  "mkdir -p '$remote_dir'; (sudo -n tcpdump -i any -s 0 -U -w - '$capture_filter' >'$remote_pcap' 2>'$remote_log') & echo \$!")
[[ $capture_pid =~ ^[0-9]+$ ]] || { printf '%s\n' "invalid tcpdump PID" >&2; exit 2; }

set +e
"$repo_dir/.venv/bin/python" "$repo_dir/scripts/run_p4_wildix_hil.py" \
  --p4-host "$p4_host" \
  --out-dir "$out_dir" \
  --output "$out_dir/summary.json" \
  "$@"
runner_status=$?
set -e

stop_capture
scp -q "$ha_ssh:$remote_pcap" "$local_pcap"
scp -q "$ha_ssh:$remote_log" "$out_dir/tcpdump.log"
capinfos "$local_pcap" >"$out_dir/capinfos.txt"
"$repo_dir/.venv/bin/python" "$repo_dir/tools/rtp_pcap_evidence.py" \
  "$local_pcap" \
  --output "$out_dir/rtp-evidence.json" \
  --require-streams 4 \
  --max-loss 0

exit "$runner_status"
