"""HA service handlers for standard SIP endpoint accounts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .runtime_data import sip_registrar
from .sip_registrar import (
    SipAccount,
    dump_account,
    generate_password,
    normalize_username,
)
from .store import sip_account_dicts, update_sip_accounts

_LOGGER = logging.getLogger(__name__)


def build_account_service_handlers(
    refresh_and_push_phonebook: Callable[[object], Awaitable[None]],
) -> dict[str, Callable[[ServiceCall], Awaitable[Any]]]:
    """Build account service handlers with the phonebook refresh dependency injected."""

    async def create_account(call: ServiceCall) -> dict[str, Any]:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        display_name = str(call.data.get("display_name") or username).strip()
        replace_existing = bool(call.data.get("replace", False))
        accounts = sip_account_dicts(hass)
        if (
            any(
                str(item.get("username") or "").lower() == username.lower()
                for item in accounts
            )
            and not replace_existing
        ):
            raise ServiceValidationError(f"SIP account {username} already exists")
        provided_password = str(call.data.get("password") or "")
        generated_password = not provided_password
        password = generate_password() if generated_password else provided_password
        account = SipAccount(
            username=username,
            display_name=display_name,
            password=password,
            enabled=bool(call.data.get("enabled", True)),
            extension=str(call.data.get("extension") or "").strip(),
            conference_group=str(call.data.get("conference_group") or "").strip(),
            conference_ring=bool(call.data.get("conference_ring", False)),
            ring_group=str(call.data.get("ring_group") or "").strip(),
        )
        accounts = [
            item
            for item in accounts
            if str(item.get("username") or "").lower() != username.lower()
        ]
        accounts.append(dump_account(account))
        try:
            update_sip_accounts(hass, accounts)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        await refresh_and_push_phonebook(hass)
        response: dict[str, Any] = {
            "username": username,
            "display_name": display_name,
            "password_generated": generated_password,
        }
        if generated_password:
            response["password"] = password
        _LOGGER.info(
            "SIP local account created username=%s enabled=%s",
            username,
            account.enabled,
        )
        return response

    async def remove_account(call: ServiceCall) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        accounts = [
            item
            for item in sip_account_dicts(hass)
            if str(item.get("username") or "").lower() != username.lower()
        ]
        update_sip_accounts(hass, accounts)
        registrar = sip_registrar(hass)
        if registrar is not None:
            registrar.remove_registration(username)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("SIP local account removed username=%s", username)

    async def rotate_account_password(call: ServiceCall) -> dict[str, str]:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        password = generate_password()
        found = False
        accounts = []
        for item in sip_account_dicts(hass):
            if str(item.get("username") or "").lower() == username.lower():
                item["password"] = password
                found = True
            accounts.append(item)
        if not found:
            raise ServiceValidationError(f"SIP account {username} does not exist")
        update_sip_accounts(hass, accounts)
        registrar = sip_registrar(hass)
        if registrar is not None:
            registrar.remove_registration(username)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("SIP local account password rotated username=%s", username)
        return {"username": username, "password": password}

    async def set_account_enabled(call: ServiceCall, *, enabled: bool) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        found = False
        accounts = []
        for item in sip_account_dicts(hass):
            if str(item.get("username") or "").lower() == username.lower():
                item["enabled"] = enabled
                found = True
            accounts.append(item)
        if not found:
            raise ServiceValidationError(f"SIP account {username} does not exist")
        update_sip_accounts(hass, accounts)
        if not enabled:
            registrar = sip_registrar(hass)
            if registrar is not None:
                registrar.remove_registration(username)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info(
            "SIP local account %s username=%s",
            "enabled" if enabled else "disabled",
            username,
        )

    def account_response(call: ServiceCall) -> dict[str, list[dict[str, Any]]]:
        accounts = [
            {
                "username": item.get("username", ""),
                "display_name": item.get("display_name", ""),
                "enabled": bool(item.get("enabled", True)),
                "extension": str(item.get("extension") or ""),
                "conference_group": str(item.get("conference_group") or ""),
                "conference_ring": bool(item.get("conference_ring", False)),
                "ring_group": str(item.get("ring_group") or ""),
            }
            for item in sip_account_dicts(call.hass)
        ]
        return {"accounts": accounts}

    async def list_accounts(call: ServiceCall) -> dict[str, list[dict[str, Any]]]:
        return account_response(call)

    async def enable_account(call: ServiceCall) -> None:
        await set_account_enabled(call, enabled=True)

    async def disable_account(call: ServiceCall) -> None:
        await set_account_enabled(call, enabled=False)

    return {
        "create_account": create_account,
        "remove_account": remove_account,
        "rotate_account_password": rotate_account_password,
        "enable_account": enable_account,
        "disable_account": disable_account,
        "list_accounts": list_accounts,
    }
