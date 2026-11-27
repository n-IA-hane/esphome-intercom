"""Explicit policy fixture for registrar tests that do not exercise HA storage."""


def configured_registrar_policy(**overrides):
    """Supply the form's initial policy at the test setup boundary."""
    from custom_components.voip_stack.sip_registration_policy import (
        RegistrarPolicy,
        registrar_policy_defaults,
    )

    return RegistrarPolicy.from_mapping({
        **registrar_policy_defaults(),
        **{"registrar_" + key: value for key, value in overrides.items()},
    })
