"""The existing Dahua offer seed cannot override explicit or historical RTP meaning."""

from dataclasses import replace

import pytest
from custom_components.voip_stack.peer_media_profile import legacy_pcm_compatibility

from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import HA_TRUNK_AUDIO_FORMATS
from custom_components.voip_stack.core.media.leg import TRUNK_POLICY
from custom_components.voip_stack.core.media.offers import create_complete_offer
from custom_components.voip_stack.core.media.payload_map import PayloadMap
from custom_components.voip_stack.core.sdp_types import RtpPcmFormat

pytestmark = pytest.mark.ha
PCM = ("PCM", 16000, 1)
L16 = ("L16", 16000, 1)


def offer(**options):
    return create_complete_offer(replace(TRUNK_POLICY, compatibility=legacy_pcm_compatibility(True)),
        HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS,
        common=True, capabilities=HA_AUDIO_CAPABILITIES, **options)


def test_default_dahua_pcm_preserves_the_original_issue_115_and_161_wire_number():
    plan = offer()
    assert dict(plan.payloads)[97] == PCM
    assert all(fmt.payload_type != 97 for fmt in plan.formats if fmt.encoding != "PCM")


@pytest.mark.parametrize("explicit", [
    RtpPcmFormat(110, "PCM", 16000, 1, 20),
    RtpPcmFormat(97, "L16", 16000, 1, 20),
])
def test_explicit_caller_mapping_outranks_the_dahua_seed(explicit):
    plan = offer(send_rtp_formats=(explicit,), recv_rtp_formats=(explicit,))
    expected = (explicit.encoding, explicit.sample_rate, explicit.channels)
    assert dict(plan.payloads)[explicit.payload_type] == expected
    if expected != PCM:
        assert dict(plan.payloads)[97] != PCM


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("old", [PCM, L16])
def test_active_and_retired_dialog_meanings_outrank_the_dahua_seed(retired, old):
    prior = PayloadMap(receive={110 if old == PCM else 97: old})
    if retired:
        prior = prior.with_mappings(receive={0: ("PCMU", 8000, 1)}, send={})
    plan = offer(previous=prior)
    numbers = dict(plan.payloads)
    assert numbers[110 if old == PCM else 97] == old
    if old == L16:
        assert numbers[97] != PCM
