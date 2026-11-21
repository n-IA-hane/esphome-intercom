"""Codec allowlists applied to one SIP leg, without changing RTP mappings."""

from dataclasses import dataclass

AUDIO_CODECS = ("OPUS", "G722", "PCMA", "PCMU", "L16", "L24", "PCM")
VIDEO_CODECS = ("H264", "VP8", "JPEG", "H263", "H263P", "H265")


def normalize_codec_rows(value, supported):
    """Preserve one canonical ordered form, including disabled choices."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("Select codecs from the supported list")
    rows, seen = [], set()
    for item in value:
        legacy = isinstance(item, str)
        if legacy:
            codec, enabled = item.strip().upper(), True
        elif isinstance(item, dict) and isinstance(item.get("codec"), str) and isinstance(item.get("enabled", True), bool):
            codec, enabled = item["codec"].strip().upper(), item.get("enabled", True)
        else:
            raise ValueError("Select codecs from the supported list")
        if codec not in supported:
            raise ValueError("Select codecs from the supported list")
        if codec in seen:
            if legacy:
                continue
            raise ValueError("Select each codec only once")
        seen.add(codec)
        rows.append({"codec": codec, "enabled": enabled})
    return tuple(rows)


def normalize_codecs(value, supported, *, allow_empty=False):
    result = tuple(row["codec"] for row in normalize_codec_rows(value, supported) if row["enabled"])
    if (not result and not allow_empty) or any(item not in supported for item in result):
        raise ValueError("Select at least one supported audio codec" if not allow_empty else "Select supported video codecs")
    return result


def available_audio_codecs():
    """Names with the current backend's RTP implementation in both directions."""
    from .codec_capabilities import common_sip_codecs
    optional = common_sip_codecs()
    return tuple(name for name in AUDIO_CODECS if name not in {"OPUS", "G722"} or name in optional)


def available_video_codecs():
    """Video encodings the RTP relay can carry; conversion is checked per route."""
    return VIDEO_CODECS


@dataclass(frozen=True, slots=True)
class CodecPolicy:
    """None preserves existing capabilities; an empty video list disables video."""

    audio: tuple[str, ...] | None = None
    video: tuple[str, ...] | None = None
    audio_ordered: bool = True
    video_ordered: bool = True

    def audio_formats(self, formats):
        chosen = tuple(item for item in formats if self.audio is None or item.encoding.upper() in self.audio)
        return tuple(sorted(chosen, key=lambda item: self.audio.index(item.encoding.upper()))) if self.audio is not None and self.audio_ordered else chosen

    def video_formats(self, formats):
        chosen = tuple(item for item in formats if self.video is None or item.encoding.upper() in self.video)
        return tuple(sorted(chosen, key=lambda item: self.video.index(item.encoding.upper()))) if self.video is not None and self.video_ordered else chosen

    @classmethod
    def from_config(cls, config, parent=None, *, prefix="trunk_"):
        values = {}
        for media, supported in (("audio", AUDIO_CODECS), ("video", VIDEO_CODECS)):
            raw = config.get(prefix + media + "_codecs")
            if raw is None:
                values[media] = getattr(parent, media) if parent is not None else supported
                values[media + "_ordered"] = getattr(parent, media + "_ordered") if parent is not None else False
            else:
                values[media] = normalize_codecs(raw, supported, allow_empty=media == "video")
                values[media + "_ordered"] = True
        return cls(**values)
