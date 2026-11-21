#!/usr/bin/env bash
# Run independent SIP/RTP peers in a private network namespace as the invoking user.
set -euo pipefail

peer_repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
peer_script="$peer_repo/scripts/run_community_peer_tests.sh"

if [[ ${1:-} == --namespace ]]; then
  shift
  [[ $EUID -eq 0 && $# -eq 5 ]] || exit 2
  peer_uid=$1
  peer_parent_namespace=$2
  peer_python=$3
  peer_evidence=$4
  peer_work=$5
  [[ $peer_uid =~ ^[1-9][0-9]*$ ]] || exit 2
  [[ $(readlink /proc/self/ns/net) != "$peer_parent_namespace" ]] || {
    printf '%s\n' 'Independent peers require a separate network namespace' >&2
    exit 2
  }
  ip link set lo up
  exec sudo -n -u "#$peer_uid" -- env \
    HA_PYTHON="$peer_python" \
    PYTHONHASHSEED="${PYTHONHASHSEED:-0}" \
    "$peer_script" --run "$peer_evidence" "$peer_work"
fi

if [[ ${1:-} == --run ]]; then
  shift
  [[ $EUID -ne 0 && $# -eq 2 ]] || exit 2
  peer_evidence=$1
  peer_work=$2
  cd "$peer_repo"
  export TMPDIR="$peer_work"
  export VOIP_TEST_SIPP_COMMAND="sudo -n $(command -v sipp)"
  export VOIP_COMMUNITY_EVIDENCE_DIR="$peer_evidence/media"
  # Test workers retain ordinary user ownership. Only packet playback and
  # capture subprocesses need raw sockets inside this private namespace.
  sudo -n "$HA_PYTHON" -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP).close()'
  "$HA_PYTHON" -c 'import importlib.metadata; print("native_home_assistant=" + importlib.metadata.version("homeassistant"))'
  "$HA_PYTHON" -m pytest \
    tests/test_community_independent_peer_ha.py \
    tests/test_hikvision_independent_peer_ha.py \
    tests/test_community_video_baresip_ha.py \
    tests/test_dahua_offer_payload_seed_ha.py \
    -q --tb=short -p no:respx \
    --basetemp="$peer_work/pytest" \
    --junitxml="$peer_evidence/results.xml"
  "$HA_PYTHON" scripts/check_test_results.py "$peer_evidence/results.xml"
  for peer_capture in "$peer_evidence"/media/*/wire.pcap; do
    capinfos -c -a -e "$peer_capture" > "${peer_capture%.pcap}.capinfos.txt"
  done
  exit 0
fi

[[ $# -eq 2 && $EUID -ne 0 ]] || {
  printf '%s\n' 'Usage: HA_PYTHON=/absolute/native-ha/python scripts/run_community_peer_tests.sh EVIDENCE_DIR PERSISTENT_WORK_DIR' >&2
  exit 2
}
: "${HA_PYTHON:?Set HA_PYTHON to the matching native Home Assistant test interpreter}"
[[ $HA_PYTHON == /* && -x $HA_PYTHON ]] || exit 2
for peer_tool in sudo unshare ip sipp baresip ffmpeg ffprobe tcpdump tshark capinfos; do
  command -v "$peer_tool" >/dev/null || { printf 'Missing peer dependency: %s\n' "$peer_tool" >&2; exit 2; }
done
peer_evidence=$(realpath -m "$1")
peer_work=$(realpath -m "$2")
for peer_destination in "$peer_evidence" "$peer_work"; do
  [[ $peer_destination != "$peer_repo" && $peer_destination != "$peer_repo/"* ]] || {
    printf '%s\n' 'Peer evidence and scratch must be outside the source checkout' >&2
    exit 2
  }
done
[[ ! -e "$peer_evidence/results.xml" ]] || { printf '%s\n' 'Use a fresh evidence directory' >&2; exit 2; }
umask 077
mkdir -p "$peer_evidence" "$peer_work"
chmod 700 "$peer_evidence" "$peer_work"
[[ $(findmnt -T "$peer_work" -n -o FSTYPE) != tmpfs ]] || {
  printf '%s\n' 'Peer scratch must be on persistent disk' >&2
  exit 2
}
peer_parent_namespace=$(readlink /proc/self/ns/net)
exec sudo -n unshare --net -- env PYTHONHASHSEED="${PYTHONHASHSEED:-0}" "$peer_script" --namespace \
  "$(id -u)" "$peer_parent_namespace" "$HA_PYTHON" "$peer_evidence" "$peer_work"
