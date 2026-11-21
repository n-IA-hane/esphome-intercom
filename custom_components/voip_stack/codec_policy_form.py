"""Native HA forms for the existing codec policy storage contract."""

import voluptuous as vol

from homeassistant.data_entry_flow import Optional as visible_optional
from homeassistant.helpers.selector import BooleanSelector, ObjectSelector, SelectSelector

from .core.codec_policy import (
    AUDIO_CODECS, VIDEO_CODECS, available_audio_codecs, available_video_codecs,
    normalize_codec_rows, normalize_codecs,
)


def codec_form_fields(current, *, prefix="", inherit=False, parent=None):
    """Display codec rows in their saved order using HA's native row editor."""
    fields = {}
    for media, supported, available in (("audio", AUDIO_CODECS, available_audio_codecs()),
                                         ("video", VIDEO_CODECS, available_video_codecs())):
        key = prefix + media + "_codecs"
        inherit_key = key + "_inherit"
        raw = current.get(key)
        inherited = raw is None
        if raw is None:
            raw = (parent or {}).get(media + "_codecs")
        rows = list(normalize_codec_rows(raw if raw is not None else available, supported))
        # Retain a saved supported choice even if its optional implementation
        # is temporarily unavailable on this installation.
        options = list(dict.fromkeys((*available, *(row["codec"] for row in rows))))

        class CompatibleCodecSelector(ObjectSelector):
            def __call__(self, value, supported=supported):
                try:
                    canonical = list(normalize_codec_rows(value, supported))
                except ValueError as error:
                    raise vol.Invalid("codecs_invalid") from error
                return super().__call__(canonical)

        editor = CompatibleCodecSelector({"multiple": True, "label_field": "codec",
            "translation_key": "codec_order", "fields": {
                "codec": {"required": True, "selector": SelectSelector({"options": options,
                    "translation_key": "trunk_" + media + "_codecs"})},
                "enabled": {"required": True, "default": True, "selector": BooleanSelector()},
            }})
        if inherit:
            # Suggested values populate the editor without injecting a new
            # inherit switch into older callers that submit explicit lists.
            fields[vol.Optional(inherit_key, description={"suggested_value": inherited})] = BooleanSelector()
            fields[visible_optional(key, description={"suggested_value": rows},
                visible={"field": inherit_key, "operator": "not_eq", "value": True})] = editor
        else:
            fields[vol.Required(key, default=rows)] = editor
    return fields


def normalize_codec_form(data, user_input, current, *, prefix="", inherit=False,
                         error_key="codecs_invalid"):
    """Write canonical rows while keeping inherit controls out of storage."""
    errors, inherited = {}, set()
    for media, supported in (("audio", AUDIO_CODECS), ("video", VIDEO_CODECS)):
        key = prefix + media + "_codecs"
        inherit_key = key + "_inherit"
        use_parent = inherit and user_input.get(inherit_key) is True
        raw = user_input.get(key, current.get(key))
        data.pop(inherit_key, None)
        if use_parent or raw is None:
            data.pop(key, None)
            inherited.add(key)
            continue
        try:
            rows = list(normalize_codec_rows(raw, supported))
            normalize_codecs(rows, supported, allow_empty=media == "video")
        except ValueError:
            errors[key] = error_key
        else:
            data[key] = rows
    return errors, inherited
