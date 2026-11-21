#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-}
HA_PYTHON=${HA_PYTHON:-}
MODE=fast
KEEP_GOING=0
SEED=
SHARD=
SHARD_MANIFEST=
PARALLEL_SHARDS=
SHARD_RESULTS=
SHARD_WORK=

usage() {
  printf '%s\n' \
    "Usage: scripts/test_suite.sh [fast|software-full|editorial|calls|peer|coverage|ha|js-runtime|browser|fault|mutation] [options]" \
    "" \
    "Options:" \
    "  --keep-going  run independent tests after a failure" \
    "  --seed N      set PYTHONHASHSEED and the Hypothesis seed" \
    "  --shard I/N   software-full only: run deterministic shard I of N" \
    "  --shard-manifest PATH" \
    "                write the full selection and the shard's node IDs" \
    "  --parallel-shards N" \
    "                software-full only: run all N shards at once, each in" \
    "                its own network namespace and temporary directory" \
    "  --shard-results DIR" \
    "                reports of --parallel-shards (default: test-results)" \
    "  --shard-work DIR" \
    "                scratch of --parallel-shards, on persistent disk" \
    "                (default: \${TMPDIR:-/var/tmp}/software-shards)" \
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
    --shard|--shard-manifest|--parallel-shards|--shard-results|--shard-work)
      [[ $# -ge 2 ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
      case "$1" in
        --shard) SHARD=$2 ;;
        --shard-manifest) SHARD_MANIFEST=$2 ;;
        --parallel-shards) PARALLEL_SHARDS=$2 ;;
        --shard-results) SHARD_RESULTS=$2 ;;
        --shard-work) SHARD_WORK=$2 ;;
      esac
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

if [[ -n $SHARD$SHARD_MANIFEST$PARALLEL_SHARDS$SHARD_RESULTS$SHARD_WORK ]]; then
  [[ $MODE == software-full ]] || {
    printf '%s\n' "Shard options apply only to software-full" >&2; exit 2;
  }
  [[ -z $PARALLEL_SHARDS || -z $SHARD$SHARD_MANIFEST ]] || {
    printf '%s\n' "--parallel-shards cannot be combined with --shard" >&2; exit 2;
  }
  [[ -n $PARALLEL_SHARDS || -z $SHARD_RESULTS$SHARD_WORK ]] || {
    printf '%s\n' "--shard-results and --shard-work need --parallel-shards" >&2; exit 2;
  }
  [[ -z $SHARD || $SHARD =~ ^[1-9][0-9]*/[1-9][0-9]*$ ]] || {
    printf '%s\n' "Shard must be INDEX/COUNT" >&2; exit 2;
  }
  [[ -z $PARALLEL_SHARDS || $PARALLEL_SHARDS =~ ^[1-9][0-9]*$ ]] || {
    printf '%s\n' "Parallel shard count must be numeric" >&2; exit 2;
  }
fi

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

resolve_ha_python() {
  if [[ -z $HA_PYTHON ]]; then
    if [[ -x "$ROOT/../ha-voip-lab/.venv/bin/python" ]]; then
      HA_PYTHON="$ROOT/../ha-voip-lab/.venv/bin/python"
    elif "$PYTHON" -c 'import homeassistant' >/dev/null 2>&1; then
      HA_PYTHON=$PYTHON
    fi
  elif [[ $HA_PYTHON != */* ]]; then
    HA_PYTHON=$(command -v "$HA_PYTHON" || true)
  fi
  [[ -n $HA_PYTHON && -x $HA_PYTHON ]] || {
    printf '%s\n' \
      "Home Assistant test Python not found; set HA_PYTHON or install requirements-ha-test.txt" >&2
    exit 2
  }
}

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
ha_tests=(
  tests/test_fanvil_blf_pickup_wire_ha.py
  tests/test_trunk_voicemail_ha.py
  tests/test_assist_core_boundary_ha.py
  tests/test_media_account_authorization_ha.py
  tests/test_queue_select_ha.py
  tests/test_sip_account_control_ha.py
  tests/test_physical_phone_flow_ha.py
  tests/test_physical_phone_contacts_ha.py
  tests/test_physical_phone_runtime_ha.py
  tests/test_physical_phone_entity_ha.py
  tests/test_physical_phone_pnp_ha.py
  tests/test_physical_phone_dhcp_ha.py
  tests/test_physical_phone_enrollment_ha.py
  tests/test_physical_phone_delivery_url_ha.py
  tests/test_paging_authorization_ha.py
  tests/test_owned_cleanup_ha.py
  tests/test_queue_activity_ha.py
  tests/test_native_route_window_ha.py
  tests/test_group_devices_ha.py
  tests/test_group_lifecycle_review_ha.py
  tests/test_queue_agent_blf_ha.py
  tests/test_queue_agent_wire_ha.py
  tests/test_assist_call_tools_ha.py
  tests/test_assist_call_forward_ha.py
  tests/test_inbound_did_ha.py
  tests/test_inbound_did_bridge_ha.py
  tests/test_paging_ha.py
  tests/test_browser_paging_ha.py
  tests/test_browser_paging_source_ha.py
  tests/test_queue_call_ha.py
  tests/test_queue_baresip_ha.py
  tests/test_phone_routing_ha.py
  tests/test_phone_settings_ha.py
  tests/test_phone_policy_runtime_ha.py
  tests/test_phone_state_ha.py
  tests/test_phone_monitor_ha.py
  tests/test_popup_frontend_ha.py
  tests/test_camera_preference_directory_ha.py
  tests/test_esphome_state_discovery_ha.py
  tests/test_local_browser_pickup_video_ha.py
  tests/test_esp_screenshot_homeassistant.py
  tests/test_ldap_directory_ha.py
  tests/test_ldap_provisioning_ha.py
  tests/test_ldap_standard_extensions_ha.py
  tests/test_phone_services_ha.py
  tests/test_phone_services_lifecycle_ha.py
  tests/test_trunk_config_flow.py
  tests/test_trunk_media_config_ha.py
  tests/test_media_nat_demux_ha.py
  tests/test_trunk_catalog_ha.py
  tests/test_trunk_calls_ha.py
  tests/test_trunk_codec_policy_ha.py
  tests/test_contact_profile_ha.py
  tests/test_trunk_access_ha.py
  tests/test_trunk_transfer_access.py
  tests/test_automation_call.py
  tests/test_voicemail_ha.py
  tests/test_mwi_unregister_ha.py
  tests/test_call_ivr_ha.py
  tests/test_voicemail_baresip_ha.py
  tests/test_sip_phone_transfers_ha.py
  tests/test_refer_account_identity_ha.py
  tests/test_registered_trunk_remote_bye_ha.py
  tests/test_trunk_receive_budget_ha.py
  tests/test_audio_splice_ha.py
  tests/test_media_call_lifetime_ha.py
  tests/test_pbx_defaults_ha.py
  tests/test_parking_lot_ha.py
  tests/test_source_video_updates_ha.py
  tests/test_atomic_bridge_media_ha.py
  tests/test_parking_video_lifecycle_ha.py
  tests/test_parking_video_transcode_ha.py
  tests/test_video_originate_unknown_ha.py
  tests/test_logical_transfer_wire_ha.py
  tests/test_call_recording_ha.py
  tests/test_esphome_origination_ha.py
  tests/test_sip_phone_policy_ha.py
  tests/test_call_pickup_ha.py
  tests/test_group_pickup_regressions_ha.py
  tests/test_dialog_info_ha.py
  tests/test_presence_endpoints_ha.py
  tests/test_notify_transport_ha.py
  tests/test_automation_originate_ha.py
  tests/test_announcement_media_ha.py
  tests/test_call_trigger.py
  tests/test_call_deadlines_runtime.py
  tests/test_automation_route_result.py
  tests/test_entity_rename_ha.py
  tests/test_ha_integration_runtime.py
  tests/test_ha_call_transfer.py
  tests/test_card_transfer_ha.py
  tests/test_ha_assist_speech.py
  tests/test_assist_intent_responses_ha.py
  tests/test_local_source_runtime.py
  tests/test_browser_phone_policy_ha.py
  tests/test_browser_phone_owner_ha.py
  tests/test_browser_automation_contact_ha.py
  tests/test_conference_service_ha.py
  tests/test_browser_answer_noanswer_race_ha.py
  tests/test_browser_logical_availability_ha.py
  tests/test_account_consolidation_ha.py
  tests/test_account_settings_transactions_ha.py
  tests/test_account_identity_ha.py
  tests/test_account_client_snapshot_ha.py
  tests/test_authorization_registry_ha.py
  tests/test_native_phone_primitives_ha.py
  tests/test_physical_phone_user_journey_ha.py
  tests/test_physical_phone_apply_ha.py
  tests/test_account_physical_bindings_ha.py
  tests/test_account_dialing_ha.py
  tests/test_canonical_account_calls_ha.py
  tests/test_ha_bindings_ha.py
  tests/test_sip_feature_sync_ha.py
  tests/test_account_logbook_ha.py
  tests/test_shared_account_fork_ha.py
  tests/test_shared_account_policy_ha.py
  tests/test_canonical_account_parking_ha.py
  tests/test_bridge_audio_common_wire_ha.py
  tests/test_bridge_independent_ptime_ha.py
  tests/test_bridge_repacketize_ha.py
  tests/test_bridge_local_tx_capabilities_ha.py
  tests/test_audio_negotiation_matrix_ha.py
  tests/test_media_corpus_ha.py
  tests/test_media_codec_registry_ha.py
  tests/test_media_static_payloads_ha.py
  tests/test_media_negotiator_api_ha.py
  tests/test_media_active_entrypoints_ha.py
  tests/test_media_reduced_binding_offer_ha.py
  tests/test_media_complete_offers_ha.py
  tests/test_codec_policy_rows_ha.py
  tests/test_media_packet_time_ha.py
  tests/test_media_payload_map_ha.py
  tests/test_media_leg_corpus_ha.py
  tests/test_media_shadow_ha.py
  tests/test_media_offer_plan_ha.py
  tests/test_media_renegotiation_rules_ha.py
  tests/test_media_offer_mirror_ha.py
  tests/test_inbound_factory_flow_audio_ha.py
  tests/test_sip_capabilities_ha.py
  tests/test_sip_negotiation_capabilities_runtime.py
  tests/test_ha_generic_negotiation_ha.py
  tests/test_browser_contact_audio_ha.py
  tests/test_udp_outbound_flow_ha.py
  tests/test_contact_audio_routing_ha.py
  tests/test_rtp_offer_reservation_ha.py
  tests/test_delayed_rtp_reservation_ha.py
  tests/test_prebound_media_consumers_ha.py
  tests/test_managed_phones_ha.py
  tests/test_phone_directory_ha.py
  tests/test_phone_service_selectors_ha.py
  tests/test_esphome_provisioning_optional_ha.py
  tests/test_ha_sip_application.py
  tests/test_phone_control_ha.py
  tests/test_call_forwarder_behavior.py
  tests/test_endpoint_dialing.py
  tests/test_outbound_bridge_commit.py
  tests/test_call_projection.py
  tests/test_route_abort.py
  tests/test_invite_router_behavior.py
  tests/test_ring_group_orchestrator_behavior.py
  tests/test_trunk_inbound_router_behavior.py
  tests/test_softphone_video_intent.py
  tests/test_softphone_video_intent_concurrency_ha.py
  tests/test_video_keepalive_runtime.py
  tests/test_video_socket_gap.py
  tests/test_video_transcoder.py
  tests/test_sip_video_relay.py
  tests/test_accept_video_service_ha.py
  tests/test_bridge_video_framerate_wire_ha.py
  tests/test_camera_intent_wire_ha.py
  tests/test_camera_standard_intent_wire_ha.py
  tests/test_imageattr_ws.py
  tests/test_video_transcoder_cadence.py
  tests/test_video_direction_generation_ha.py
  tests/test_cn_negotiation_edges.py
  tests/test_cn_negotiation_ha.py
  tests/test_codec_policy_forms_ha.py
  tests/test_codec_settings_ha.py
  tests/test_counterpart_display_ha.py
  tests/test_dialog_payload_lifetime_ha.py
  tests/test_dialog_subscription_lease_ha.py
  tests/test_directory_consumers_ha.py
  tests/test_dtmf_media_commit_ha.py
  tests/test_large_outbound_invite_ha.py
  tests/test_media_bridge_plan_ha.py
  tests/test_notify_tcp_recovery_ha.py
  tests/test_one_way_answer_ha.py
  tests/test_pbx_route_gate_ha.py
  tests/test_sdp_attribute_isolation_ha.py
  tests/test_trunk_routing.py
)
ha_ignores=()
for test_path in "${ha_tests[@]}"; do
  ha_ignores+=("--ignore=$test_path")
done
common_pytest_args=()
if [[ $KEEP_GOING -eq 1 ]]; then
  common_pytest_args+=(--maxfail=0)
fi
if [[ -n $SEED ]]; then
  [[ $SEED =~ ^[0-9]+$ ]] || { printf '%s\n' "Seed must be numeric" >&2; exit 2; }
  export PYTHONHASHSEED=$SEED
  common_pytest_args+=(--hypothesis-seed "$SEED")
fi

export YAML_PATH_MODE=$path_mode

case "$MODE" in
  fast)
    pytest_args+=("${ha_ignores[@]}")
    pytest_args+=(--ignore=tests/test_documentation_contract.py --ignore=tests/test_text_style.py)
    pytest_args+=(-m "not ha and not js_runtime and not browser_real and not live and not mutation and not slow and not diagnostic_reference")
    ;;
  software-full)
    "$PYTHON" - <<'PY'
import importlib.metadata
import json
from pathlib import Path
import re

expected = re.search(r"@([0-9a-f]{40})", Path('requirements-firmware-test.txt').read_text()).group(1)
try:
    distribution = importlib.metadata.distribution('esphome')
    receipt = json.loads(distribution.read_text('direct_url.json') or '{}')
except importlib.metadata.PackageNotFoundError:
    receipt = {}
if receipt.get('vcs_info', {}).get('commit_id') != expected:
    raise SystemExit('Install requirements-firmware-test.txt in the selected test Python before software-full')
PY
    pytest_args+=("${ha_ignores[@]}")
    pytest_args+=(--ignore=tests/test_documentation_contract.py --ignore=tests/test_text_style.py)
    pytest_args+=(-o tmp_path_retention_policy=failed)
    pytest_args+=(-m "not live and not mutation and not browser_real and not diagnostic_reference")
    ;;
  editorial)
    pytest_args=(tests/test_documentation_contract.py tests/test_text_style.py -q --tb=short)
    ;;
  browser)
    "$PYTHON" - <<'PY'
import importlib.util
import shutil
import sys

missing = [name for name in ('chromium', 'ffmpeg') if shutil.which(name) is None]
missing += [name for name in ('playwright', 'aiohttp') if importlib.util.find_spec(name) is None]
if missing:
    sys.exit('Browser suite requires: ' + ', '.join(missing) +
             '. Install requirements-browser-test.txt, Chromium and FFmpeg.')
PY
    pytest_args=(
      tests/test_frontend_card_browser_layout.py
      tests/test_frontend_card_ux_browser.py
      tests/test_frontend_video_browser.py
      tests/test_frontend_video_runtime.py
      tests/test_frontend_popup_browser.py
      tests/test_provisioning_panel_browser.py
      tests/test_frontend_cold_start.py
      tests/test_video_decoder_recovery_browser.py
      tests/test_browser_audio_startup.py
      tests/test_browser_call_lifecycle.py
      tests/test_browser_video_lifecycle.py
      tests/test_frontend_media_devices_runtime.py
      tests/test_dashboard_card_runtime.py
      tests/test_imageattr_browser.py
      -q --tb=short -m browser_real
    )
    ;;
  peer)
    peer_tests=(
      tests/test_sip_protocol.py
      tests/test_sip_wildix_replay.py
      tests/test_sip_registrar.py
      tests/test_sip_rtp_dtmf.py
      tests/test_sip_uri.py
      tests/test_assist_runtime.py
    )
    printf 'suite=peer yaml_paths=%s process_isolation=1\n' "$path_mode"
    status=0
    for peer_test in "${peer_tests[@]}"; do
      printf 'suite=peer test=%s\n' "$peer_test"
      command=("$PYTHON" -m pytest "$peer_test" -q --tb=short "${common_pytest_args[@]}")
      "${command[@]}" || {
        status=$?
        [[ $KEEP_GOING -eq 1 ]] || exit "$status"
      }
    done
    exit "$status"
    ;;
  coverage)
    resolve_ha_python

    coverage_file="$ROOT/.coverage"
    rm -f "$coverage_file" "$ROOT"/.coverage.*

    coverage_args=(
      --cov=custom_components/voip_stack
      --cov-branch
      --cov-context=test
      --cov-fail-under=0
      --cov-report=
    )
    regular_args=(
      tests
      -q
      --tb=short
      "${ha_ignores[@]}"
      -m "not architecture and not ha and not live and not mutation"
      "${coverage_args[@]}"
    )
    ha_args=(
      "${ha_tests[@]}"
      -q
      --tb=short
      "${coverage_args[@]}"
      --cov-append
    )
    regular_args+=("${common_pytest_args[@]}")
    ha_args+=("${common_pytest_args[@]}")

    printf 'suite=coverage phase=regular yaml_paths=%s\n' "$path_mode"
    COVERAGE_FILE=$coverage_file "$PYTHON" -m pytest "${regular_args[@]}"
    printf 'suite=coverage phase=ha yaml_paths=%s\n' "$path_mode"
    COVERAGE_FILE=$coverage_file "$HA_PYTHON" -m pytest "${ha_args[@]}"
    "$PYTHON" -m coverage report --show-missing --skip-covered
    exec "$PYTHON" -m coverage xml
    ;;
  calls)
    resolve_ha_python
    PYTHON=$HA_PYTHON
    pytest_args=(
      tests/test_sip_account_control_ha.py::test_registered_call_to_literal_name_keeps_the_registered_sip_identity
      tests/test_sip_phone_transfers_ha.py::test_attended_transfer_reuses_destination_and_delivers_duplex_audio
      tests/test_shared_account_fork_ha.py::test_source_cancel_stops_both_shared_contacts_and_releases_all_owners
      tests/test_shared_account_fork_ha.py::test_ha_winner_acknowledges_and_retires_late_sip_200_without_losing_claim
      -q --tb=short
    )
    ;;
  ha)
    resolve_ha_python
    PYTHON=$HA_PYTHON
    pytest_args=(
      "${ha_tests[@]}"
      -q
      --tb=short
    )
    ;;
  js-runtime)
    pytest_args+=("${ha_ignores[@]}")
    pytest_args+=(-m "js_runtime and not browser_real")
    ;;
  fault)
    pytest_args+=("${ha_ignores[@]}")
    pytest_args+=(-m fault)
    ;;
  mutation)
    # Crashing mutants are already recorded as detected. Core files add no
    # evidence and can exhaust a runner before the mutation score is emitted.
    ulimit -c 0
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

pytest_args+=("${common_pytest_args[@]}")
if [[ -n $SHARD$SHARD_MANIFEST ]]; then
  pytest_args+=(-p scripts.software_shards_plugin)
  [[ -z $SHARD ]] || pytest_args+=(--shard "$SHARD")
  [[ -z $SHARD_MANIFEST ]] || pytest_args+=(--shard-manifest "$SHARD_MANIFEST")
fi
printf 'suite=%s yaml_paths=%s fail_fast=%s seed=%s\n' \
  "$MODE" "$path_mode" "$((1 - KEEP_GOING))" "${SEED:-automatic}"
if [[ -n $PARALLEL_SHARDS ]]; then
  # Fixed SIP and RTP ports forbid sharing one network stack between shards.
  exec "$PYTHON" scripts/software_shards.py run --count "$PARALLEL_SHARDS" \
    --results-dir "${SHARD_RESULTS:-test-results}" \
    --work-dir "${SHARD_WORK:-${TMPDIR:-/var/tmp}/software-shards}" \
    -- "$PYTHON" -m pytest "${pytest_args[@]}"
fi
exec "$PYTHON" -m pytest "${pytest_args[@]}"
