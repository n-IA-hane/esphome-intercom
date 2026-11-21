"""Choose counterpart labels without changing any routable call identity."""

from .core import sip


def connected_display_name(client):
    """Adapt the SIP owner's existing private display fact in one place.

    SipCallClient keeps the accepted To/UPDATE display separately from the
    fallback connected user. Its dialog exposes the URI, but not this name.
    """
    display = str(getattr(client, "_dialog_remote_display_name", "") or "")
    if display and display == getattr(client, "_pending_target_display", ""):
        dialog = getattr(client, "dialog", None)
        try:
            user = sip.parse_sip_uri(getattr(dialog, "remote_uri", "")).user
        except sip.SipError:
            user = ""
        if display == user:
            # A peer echoing our requested SIP user supplied no separate name.
            return ""
    return display


def counterpart_display(value="", *, fallback="", name="", display_name="", endpoint=None, entry=None, directory=None, updated=False):
    """Prefer connected identity, resolving only known user/number echoes.

    An explicit new party must never inherit the requested party's name.
    Existing owners provide aliases and directory facts; no identity is stored.
    """
    def label(value):
        value = str(value or "").strip()
        if value.lower().startswith(("sip:", "sips:")):
            try:
                return sip.parse_sip_uri(value).user
            except sip.SipError:
                return value
        return value

    raw_value = str(value or fallback or "").strip()
    value = label(value) or label(fallback)
    display_name = str(display_name or "").strip()
    numeric_echo = display_name.isdecimal() and display_name == label(fallback)
    if display_name == raw_value and display_name and (updated or not numeric_echo):
        return display_name
    if updated or not value or value.casefold() in {"anonymous", "restricted", "unavailable"}:
        return value
    if directory is not None:
        if resolved_name := directory.caller_display_name(value):
            return resolved_name
    aliases = {label(fallback)}
    if endpoint is not None:
        aliases.update(label(getattr(endpoint, key, "")) for key in (
            "endpoint_id", "username", "extension", "sip_uri_user"))
    if entry is not None:
        aliases.update(label(getattr(entry, key, "")) for key in (
            "id", "extension", "number", "sip_uri"))
    if value in aliases:
        return (str(getattr(entry, "name", "") or "")
                or str(getattr(endpoint, "name", "") or "") or label(name) or value)
    return value
