"""One production negotiator serves every adapter and keeps historical evidence frozen."""

import ast
import hashlib
from pathlib import Path
import re

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.core.media import negotiator

pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]


def test_production_has_no_engine_selector_or_shadow_adapter():
    retired = {"engine_mode", "set_engine_mode", "shadow_answer", "shadow_inbound_offer"}
    for name in (
        "sip_client.py", "sip_listener.py", "endpoint_routing.py", "local_call_media.py",
        "core/sdp_composer.py", "core/sdp_audio.py",
    ):
        path = ROOT / "custom_components/voip_stack" / name
        tree = ast.parse(path.read_text())
        assert not {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} & retired, name
        assert not any(isinstance(node, ast.Constant) and node.value == "VOIP_STACK_MEDIA_ENGINE"
                       for node in ast.walk(tree)), name
    assert not (ROOT / "custom_components/voip_stack/core/media/shadow.py").exists()


@pytest.mark.parametrize("retired_value", ["legacy", "shadow", "active"])
def test_retired_environment_values_cannot_select_a_second_offer_writer(monkeypatch, retired_value):
    monkeypatch.setenv("VOIP_STACK_MEDIA_ENGINE", retired_value)
    calls = []
    create_offer = negotiator.create_offer

    def record(*args, **kwargs):
        calls.append(True)
        return create_offer(*args, **kwargs)

    monkeypatch.setattr(negotiator, "create_offer", record)
    formats = [AudioFormat(8000, "s16le", 1, 20)]
    body = sdp.build_offer_directional("192.0.2.1", "192.0.2.1", 40000, formats, formats,
        include_common_codecs=True, codec_policy=CodecPolicy(audio=("PCMA",)))
    assert calls == [True]
    assert re.search(r"^m=audio 40000 RTP/AVP 8 ", body, re.MULTILINE)
    assert "a=rtpmap:8 PCMA/8000/1\r\n" in body
    assert "a=ptime:20\r\n" in body


def test_frozen_corpus_reference_and_captured_descriptions_are_byte_exact():
    corpus = ROOT / "tests/media_corpus"
    assert hashlib.sha256((corpus / "golden/legacy.json").read_bytes()).hexdigest() == (
        "feb4a49a3c842faa31074aa2aba96b6a3c8e928fb59cd4caf88e4fb84f2d0222"
    )
    descriptions = sorted(corpus.rglob("*.sdp"))
    assert len(descriptions) == 96
    digest = hashlib.sha256()
    for path in descriptions:
        digest.update(path.relative_to(corpus).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    assert digest.hexdigest() == "863037b395c75eea7f9685c4bc17c026ac131621f30a52e871a813249610d16f"
