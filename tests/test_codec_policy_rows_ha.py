"""One ordered codec policy, compatible with existing saved string lists."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core.codec_policy import CodecPolicy, normalize_codec_rows

pytestmark = pytest.mark.ha


def test_disabled_rows_keep_their_order_and_are_not_advertised():
    rows = [{"codec": "PCMA", "enabled": True}, {"codec": "OPUS", "enabled": False},
            {"codec": "L16", "enabled": True}]
    assert normalize_codec_rows(rows, ("PCMA", "OPUS", "L16")) == tuple(rows)
    policy = CodecPolicy.from_config({"audio_codecs": rows}, prefix="")
    assert policy.audio == ("PCMA", "L16")
    formats = [SimpleNamespace(encoding=name) for name in ("L16", "OPUS", "PCMA")]
    assert [item.encoding for item in policy.audio_formats(formats)] == ["PCMA", "L16"]


def test_absent_overrides_inherit_each_media_independently():
    parent = CodecPolicy(audio=("G722", "L16"), video=("H264",))
    policy = CodecPolicy.from_config({"video_codecs": []}, parent, prefix="")
    assert policy.audio == parent.audio
    assert policy.video == ()
    assert CodecPolicy.from_config({"audio_codecs": None}, parent, prefix="").audio == parent.audio


def test_legacy_lists_keep_their_order_and_default_policy_does_not_reorder():
    policy = CodecPolicy.from_config({"trunk_audio_codecs": ["PCMU", "G722", "PCMU"]})
    assert policy.audio == ("PCMU", "G722")
    assert policy.audio_ordered
    default = CodecPolicy.from_config({})
    formats = [SimpleNamespace(encoding=name) for name in ("L16", "G722")]
    assert default.audio_formats(formats) == tuple(formats)


@pytest.mark.parametrize("rows", [
    [{"codec": "PCMA", "enabled": "false"}],
    [{"codec": "unknown", "enabled": False}],
    [{"codec": "PCMA"}, {"codec": "PCMA"}],
    [{"codec": "PCMA", "enabled": False}],
])
def test_invalid_or_unusable_audio_policy_is_rejected(rows):
    with pytest.raises(ValueError):
        CodecPolicy.from_config({"audio_codecs": rows}, prefix="")
