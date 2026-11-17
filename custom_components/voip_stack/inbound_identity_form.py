"""Native trunk form fields for explicit called-number extraction."""

from .inbound_identity import DID_SOURCES, normalized_did_config


def did_form_fields(existing):
    """Return schema fields for the existing trunk form, with no extra lifecycle."""
    import voluptuous as vol
    from homeassistant.helpers.selector import (
        ObjectSelector, SelectSelector, SelectSelectorConfig, TextSelector,
    )

    defaults = normalized_did_config(existing)
    return {
        vol.Optional("trunk_did_source", default=defaults["trunk_did_source"]):
            SelectSelector(SelectSelectorConfig(options=list(DID_SOURCES), translation_key="trunk_did_source")),
        vol.Optional("trunk_did_header", default=defaults["trunk_did_header"]): TextSelector(),
        vol.Optional("trunk_did_fixed", default=defaults["trunk_did_fixed"]): TextSelector(),
        vol.Optional("trunk_did_fallbacks", default=defaults["trunk_did_fallbacks"]):
            SelectSelector(SelectSelectorConfig(options=list(DID_SOURCES), multiple=True, translation_key="trunk_did_source")),
        vol.Optional("trunk_did_prefix_rules", default=defaults["trunk_did_prefix_rules"]): ObjectSelector(),
    }


def validate_did_form(user_input, existing):
    """Preserve omitted fields during reconfigure and return one precise error."""
    try:
        user_input.update(normalized_did_config({**existing, **user_input}))
    except ValueError:
        return {"trunk_did_source": "trunk_did_invalid"}
    return {}
