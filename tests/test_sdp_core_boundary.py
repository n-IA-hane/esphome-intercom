"""The SDP API accepts neutral policy without importing an integration adapter."""

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


CORE = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/core"


class SdpCoreBoundaryTest(unittest.TestCase):
    def _run_core(self, body: str) -> None:
        # Load only the real core directory, as a consumer independent of HA.
        # No integration parent exists, so outward imports cannot be hidden.
        bootstrap = """
import importlib
import sys
import types
package = types.ModuleType("standalone_core")
package.__path__ = [sys.argv[1]]
sys.modules[package.__name__] = package
sdp = importlib.import_module("standalone_core.sdp")
"""
        result = subprocess.run(
            [sys.executable, "-c", bootstrap + textwrap.dedent(body), str(CORE)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sdp_imports_without_any_home_assistant_or_contact_adapter(self):
        self._run_core("""
            assert not any(name.startswith("homeassistant") for name in sys.modules)
            assert not any(name.endswith("peer_media_profile") for name in sys.modules)
            assert sdp.parse_sdp("v=0\\r\\no=- 1 1 IN IP4 192.0.2.1\\r\\ns=Call\\r\\n"
                "c=IN IP4 192.0.2.1\\r\\nt=0 0\\r\\nm=audio 40000 RTP/AVP 0\\r\\n")["payload_order"] == [0]
        """)

    def test_public_sdp_wrappers_preserve_a_neutral_optional_mapping_policy(self):
        self._run_core("""
            from standalone_core.audio_capabilities import HA_AUDIO_CAPABILITIES
            from standalone_core.audio_format import AudioFormat
            from standalone_core.codec_policy import CodecPolicy
            from standalone_core.media.capability import MediaCompatibility

            compatibility = MediaCompatibility(
                optional_mappings=(("PCM", 16000, 1),),
                payload_preferences=((("PCM", 16000, 1), 112),),
            )
            preferred = [AudioFormat(16000, frame_ms=20)]
            codec_policy = CodecPolicy(audio=("PCM",))
            offer = sdp.build_offer_directional(
                "192.0.2.1", "192.0.2.1", 40000, preferred, preferred,
                include_common_codecs=True, compatibility=compatibility,
                codec_policy=codec_policy, local_capabilities=HA_AUDIO_CAPABILITIES,
            )
            assert not sdp.offered_pcm_formats(offer)
            mappings = sdp.offered_pcm_formats(
                offer, optional_mappings=compatibility.optional_mappings)
            assert [(fmt.payload_type, fmt.encoding, fmt.sample_rate) for fmt in mappings] == [(112, "PCM", 16000)]

            answer = offer.replace("192.0.2.1", "192.0.2.2").replace("40000", "41000")
            options = dict(compatibility=compatibility, codec_policy=codec_policy,
                local_capabilities=HA_AUDIO_CAPABILITIES)
            selected = sdp.negotiate_directional(answer, preferred, preferred, **options)
            assert selected is not None
            assert selected.send.encoding == selected.recv.encoding == "PCM"
            assert selected.send.payload_type == selected.recv.payload_type == 112
            applied = sdp.negotiate_answer_directional(answer, preferred, preferred,
                local_offer_sdp=offer, **options)
            assert applied is not None
            assert applied.send.payload_type == applied.recv.payload_type == 112
            assert not any(name.endswith("peer_media_profile") for name in sys.modules)
        """)
