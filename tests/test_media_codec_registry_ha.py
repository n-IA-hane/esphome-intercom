"""The codec registry is the only owner of codec facts and agrees with the standards."""

from pathlib import Path
import re

import pytest

from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.core.codec_policy import AUDIO_CODECS
from custom_components.voip_stack.core.media.codecs import (
    CODECS,
    OCTETS_PER_SAMPLE,
    PREFERRED_PAYLOADS,
    STATIC_RTPMAP,
)
from custom_components.voip_stack.core.sdp_types import RtpPcmFormat

pytestmark = pytest.mark.ha

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack"


def test_static_payloads_match_rfc_3551_table_4():
    assert STATIC_RTPMAP == {
        0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1), 9: ("G722", 8000, 1),
        10: ("L16", 44100, 2), 11: ("L16", 44100, 1),
    }


def test_g722_keeps_its_8_khz_clock_and_16_khz_audio():
    """RFC 3551 4.5.2: the RTP clock stays 8000 although the audio is sampled at 16000."""
    fmt = RtpPcmFormat(9, "G722", 8000, 1, 20)
    assert fmt.rtp_timestamp_step == 160
    assert fmt.audio_format.sample_rate == 16000
    assert rtp.audio_payload_size_limit(fmt) == 160


def test_opus_is_two_channels_on_the_wire_and_mono_when_decoded():
    fmt = RtpPcmFormat(98, "OPUS", 48000, 2, 20)
    assert PREFERRED_PAYLOADS[("OPUS", 48000, 2)] == 98
    assert (fmt.audio_format.sample_rate, fmt.audio_format.channels, fmt.bits) == (48000, 1, 0)


def test_sample_widths():
    assert OCTETS_PER_SAMPLE == {"PCMU": 1, "PCMA": 1, "G722": 1, "L16": 2, "L24": 3, "PCM": 2}
    assert [RtpPcmFormat(96, name, 16000, 1, 20).bits for name in ("L16", "L24", "PCM", "PCMA")] == [16, 24, 16, 8]


def test_every_codec_a_policy_can_name_is_in_the_registry():
    assert set(AUDIO_CODECS) == set(CODECS)


def test_no_module_keeps_its_own_copy_of_the_sample_width_table():
    """A second table would let two modules disagree about the size of a packet."""
    copies = []
    for path in ROOT.rglob("*.py"):
        if path.name == "codecs.py" and path.parent.name == "media":
            continue
        text = path.read_text()
        if re.search(r'"PCMA":\s*1\b', text) or re.search(r'"L16":\s*2\b', text):
            copies.append(str(path.relative_to(ROOT)))
    assert not copies, copies
