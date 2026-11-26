"""Native trunk caller-policy and authenticated peer settings."""

from .outbound_identity import CALLER_ID_POLICIES, identity_text, normalized_outbound_identity_config


def _accounts(entry):
    from .phone_config import phone_subentries

    if entry is None:
        return {}
    return {
        str(row.data.get("username") or ""): row
        for row in phone_subentries(entry)
        if row.data.get("username") and row.data.get("enabled", True)
    }


def trunk_identity_fields(entry, existing):
    """Reuse the trunk form; no alternate phone identity or transport exists."""
    import voluptuous as vol
    from homeassistant.helpers.selector import SelectSelector, TextSelector

    defaults = normalized_outbound_identity_config(existing)
    accounts = _accounts(entry)
    binding = str(existing.get("trunk_inbound_account") or "")
    choices = [{"value": "", "label": "Use the trunk's existing incoming trust policy"}]
    choices.extend({"value": username, "label": f"{row.title} ({username})"} for username, row in accounts.items())
    if binding and binding not in accounts:
        choices.append({"value": binding, "label": f"{binding} (account unavailable)"})
    return {
        vol.Optional("trunk_caller_id_policy", default=defaults["trunk_caller_id_policy"]):
            SelectSelector({"options": list(CALLER_ID_POLICIES), "translation_key": "trunk_caller_id_policy"}),
        vol.Optional("trunk_caller_id", default=defaults["trunk_caller_id"]): TextSelector(),
        vol.Optional("trunk_caller_name", default=defaults["trunk_caller_name"]): TextSelector(),
        vol.Optional("trunk_inbound_account", default=binding):
            SelectSelector({"options": choices, "mode": "dropdown"}),
    }


def validate_trunk_identity_form(user_input, existing, entry, *, trunk_id=""):
    """Preserve omitted fields, and bind only one enabled line per SIP account."""
    from .trunk_config import trunk_subentries

    merged = {**existing, **user_input}
    errors = {}
    try:
        user_input.update(normalized_outbound_identity_config(merged))
    except ValueError:
        errors["trunk_caller_id"] = "trunk_caller_id_invalid"
    try:
        binding = identity_text(merged.get("trunk_inbound_account", ""), "inbound account")
    except ValueError:
        binding = ""
        errors["trunk_inbound_account"] = "trunk_inbound_account_invalid"
    user_input.update(trunk_inbound_account=binding)
    if binding:
        if binding not in _accounts(entry):
            errors["trunk_inbound_account"] = "trunk_inbound_account_invalid"
        elif any(
            str(row.data.get("trunk_id") or row.subentry_id) != trunk_id
            and row.data.get("trunk_enabled", False)
            and row.data.get("trunk_inbound_account") == binding
            for row in trunk_subentries(entry)
        ):
            errors["trunk_inbound_account"] = "trunk_inbound_account_in_use"
    return errors
