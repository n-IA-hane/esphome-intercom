"""Pure SDP destination rewriting preserves hold and fails closed on new media."""

import importlib.util
from pathlib import Path
import sys

import pytest


_PATH = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/core/media_address.py"
_SPEC = importlib.util.spec_from_file_location("media_address_under_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
addressing = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = addressing
_SPEC.loader.exec_module(addressing)
MediaEndpoint = addressing.MediaEndpoint
apply_media_endpoints = addressing.apply_media_endpoints

_AUDIO = MediaEndpoint(41000, "198.51.100.23", 47003)
_VIDEO = MediaEndpoint(42000, "198.51.100.24", 49152)
_RTCP = MediaEndpoint(42001, "198.51.100.25", 61001)


def _body(*media, connection="192.0.2.10"):
    return (f"v=0\r\no=- 123 7 IN IP4 192.0.2.10\r\ns=phone\r\n"
            f"c=IN IP4 {connection}\r\nt=0 0\r\n" + "".join(media))


def _media(kind, port, *, connection=None, direction="sendrecv"):
    result = f"m={kind} {port} RTP/AVP {'96' if kind == 'audio' else '26'}\r\n"
    if connection is not None:
        result += f"c=IN IP4 {connection}\r\n"
    result += "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n" if kind == "audio" else "a=rtpmap:26 JPEG/90000\r\na=rtcp:42001\r\n"
    return result + f"a={direction}\r\n"


@pytest.mark.parametrize("kind,port,endpoint", [("audio", 41000, _AUDIO), ("video", 42000, _VIDEO)])
@pytest.mark.parametrize("scope", ["session", "media"])
def test_zero_connection_hold_is_never_replaced_by_public_address(kind, port, endpoint, scope):
    original = _body(
        _media(kind, port, connection="0.0.0.0" if scope == "media" else None, direction="sendonly"),
        connection="0.0.0.0" if scope == "session" else "192.0.2.10",
    )
    assert apply_media_endpoints(original, {kind: endpoint}) == original


def test_session_zero_is_inherited_except_for_nonzero_media_override():
    original = _body(_media("audio", 41000), _media("video", 42000, connection="192.0.2.20"), connection="0.0.0.0")
    mapped = apply_media_endpoints(original, {"audio": _AUDIO, "video": _VIDEO, "video_rtcp": _RTCP})
    assert "c=IN IP4 0.0.0.0\r\n" in mapped
    assert "m=audio 41000 RTP/AVP 96\r\na=rtpmap:96" in mapped
    assert "m=video 49152 RTP/AVP 26\r\nc=IN IP4 198.51.100.24\r\n" in mapped
    assert "a=rtcp:61001 IN IP4 198.51.100.25\r\n" in mapped
    assert apply_media_endpoints(mapped, {"audio": _AUDIO, "video": _VIDEO, "video_rtcp": _RTCP}) == mapped


def test_media_zero_overrides_nonzero_session_without_holding_other_media():
    audio = _media("audio", 41000, connection="0.0.0.0")
    original = _body(audio, _media("video", 42000))
    mapped = apply_media_endpoints(original, {"audio": _AUDIO, "video": _VIDEO})
    assert audio in mapped
    assert "m=video 49152 RTP/AVP 26\r\nc=IN IP4 198.51.100.24\r\n" in mapped


@pytest.mark.parametrize("missing", ["audio", "video"])
def test_new_active_media_without_mapping_fails_closed(missing):
    original = _body(_media("audio", 41000), _media("video", 42000))
    endpoints = {"audio": _AUDIO, "video": _VIDEO}
    del endpoints[missing]
    with pytest.raises(ValueError, match="NAT binding"):
        apply_media_endpoints(original, endpoints)


def test_rejected_or_zero_connection_video_does_not_require_binding():
    for video in (_media("video", 0), _media("video", 42000, connection="0.0.0.0")):
        original = _body(_media("audio", 41000), video)
        mapped = apply_media_endpoints(original, {"audio": _AUDIO})
        assert video in mapped
        assert "m=audio 47003 RTP/AVP 96\r\nc=IN IP4 198.51.100.23\r\n" in mapped


def test_inactive_with_nonzero_connection_still_needs_binding_for_rtcp():
    original = _body(_media("audio", 41000), _media("video", 42000, direction="inactive"))
    with pytest.raises(ValueError, match="NAT binding"):
        apply_media_endpoints(original, {"audio": _AUDIO})


def test_mapped_new_video_retains_contract_and_is_idempotent():
    original = _body(_media("audio", 41000), _media("video", 42000, direction="recvonly"), _media("video", 0))
    endpoints = {"audio": _AUDIO, "video": _VIDEO, "video_rtcp": _RTCP}
    mapped = apply_media_endpoints(original, endpoints)
    assert "o=- 123 7 IN IP4 192.0.2.10\r\n" in mapped
    assert "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n" in mapped
    assert "a=rtpmap:26 JPEG/90000\r\na=recvonly\r\n" in mapped
    assert _media("video", 0) in mapped
    assert "a=rtcp:61001 IN IP4 198.51.100.25\r\n" in mapped
    assert apply_media_endpoints(mapped, endpoints) == mapped


def test_second_active_section_cannot_reuse_first_sections_binding():
    original = _body(_media("video", 42000), _media("video", 43000))
    with pytest.raises(ValueError, match="NAT binding"):
        apply_media_endpoints(original, {"video": _VIDEO})


def test_direct_path_without_nat_endpoints_remains_byte_identical():
    original = _body(_media("audio", 41000), _media("video", 42000)).replace("\r\n", "\n")
    assert apply_media_endpoints(original, {}) == original
