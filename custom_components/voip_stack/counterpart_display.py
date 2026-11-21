"""Choose counterpart labels without changing any routable call identity."""


def counterpart_display(value="", *, fallback="", name="", endpoint=None, entry=None, directory=None, updated=False):
    """Prefer connected identity, resolving only known user/number echoes.

    An explicit new party must never inherit the requested party's name.
    Existing owners provide aliases and directory facts; no identity is stored.
    """
    def label(value):
        value = str(value or "").strip()
        if value.lower().startswith(("sip:", "sips:")):
            value = value.split(":", 1)[1].split(";", 1)[0].split("@", 1)[0]
        return value

    value = label(value) or label(fallback)
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
