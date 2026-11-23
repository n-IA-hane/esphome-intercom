"""Read effective SIP codec settings from the existing configuration owners."""

from .core.codec_policy import CodecPolicy


def effective_codec_policy(hass, *, endpoint_id="", fallback_endpoint_id="", trunk_id=None):
    """Inherit each medium from the parent entry without storing a copy."""
    entry = None
    if getattr(hass, "config_entries", None) is not None:
        from .store import config_entry

        entry = config_entry(hass)
    parent = CodecPolicy.from_config(entry.data if entry is not None else {}, prefix="")
    if trunk_id is not None:
        from .config import trunk_config

        return CodecPolicy.from_config(trunk_config(hass, trunk_id=trunk_id or None), parent)
    if entry is not None:
        from .phone_config import phone_subentry_by_endpoint_id

        for identity in dict.fromkeys((fallback_endpoint_id, endpoint_id)):
            if identity:
                account = phone_subentry_by_endpoint_id(entry, identity)
                if account is not None:
                    parent = CodecPolicy.from_config(account.data, parent, prefix="")
    return parent
