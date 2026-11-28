"""Select VoIP storage within Home Assistant's configured media directories."""

from pathlib import Path


def archive_path(hass) -> Path | None:
    """Use local media when available, otherwise the first configured root."""
    directories = hass.config.media_dirs
    directory = directories.get("local") or next(iter(directories.values()), None)
    return Path(directory) / "voip_stack" if directory else None


def debug_capture_path(hass) -> Path | None:
    """Keep opt-in diagnostics beside recordings in HA's native media source."""
    root = archive_path(hass)
    return root / "debug" if root is not None else None
