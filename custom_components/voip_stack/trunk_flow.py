"""Named trunks configured and validated through native HA subentry flows."""

from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import BooleanSelector, TextSelector

from .config import normalized_trunk_config
from .core.codec_policy import CodecPolicy
from .config_flow import TrunkFormMixin, _disabled_trunk_data
from .peer_snapshot import async_advertise_host
from .runtime_data import runtime_data, sip_trunk
from .trunk_config import CONF_DEFAULT_TRUNK, entry_default_trunk, entry_trunk_configs, trunk_subentries
from .trunk_policy import validate_static_trunk_sources
from .trunk_runtime import create_trunk_client, trunk_client_config, trunk_has_calls


class TrunkSubentryFlowHandler(TrunkFormMixin, ConfigSubentryFlow):
    def __init__(self):
        self._identity = uuid4().hex
        self._current = None
        self._name = ""
        self._draft = {}
        self._base_input = {"trunk_id": self._identity}
        self._candidate = None
        self._saved = False
        self._make_default = False

    def _current_entry_data(self):
        return self._get_entry(), {**(self._current.data if self._current else {}), **self._draft}

    async def async_step_reconfigure(self, user_input=None):
        self._current = self._get_reconfigure_subentry()
        self._identity = str(self._current.data.get("trunk_id") or self._current.subentry_id)
        self._base_input = {"trunk_id": self._identity,
                            "trunk_registration_id": int(self._current.data.get("trunk_registration_id", 1))}
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        entry, previous = self._current_entry_data()
        records = trunk_subentries(entry)
        suggested_default = not records or entry_default_trunk(entry, entry_trunk_configs(entry)) == self._identity
        errors = {}
        if user_input is not None:
            self._name = str(user_input["name"]).strip()
            if not self._name or len(self._name) > 128:
                errors["name"] = "invalid_name"
            elif any(item.title.casefold() == self._name.casefold() and item is not self._current
                     for item in records):
                errors["name"] = "duplicate_name"
            else:
                self._trunk_register = bool(user_input["trunk_register"])
                self._make_default = bool(user_input.get("make_default", suggested_default))
                if not user_input.get("trunk_enabled", True):
                    return self._store_entry(_disabled_trunk_data({}, previous))
                return await self.async_step_trunk()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("name", default=previous.get("name", "")): TextSelector(),
            vol.Required("trunk_enabled", default=previous.get("trunk_enabled", True)): BooleanSelector(),
            vol.Required("trunk_register", default=previous.get("trunk_register", True)): BooleanSelector(),
            vol.Required("make_default", default=suggested_default): BooleanSelector(),
        }))

    def _store_entry(self, data):
        entry = self._get_entry()
        data = {**normalized_trunk_config(data), "trunk_id": self._identity, "name": self._name}
        runtime = runtime_data(self.hass)
        if self._candidate is not None and runtime is not None and runtime.sip is not None:
            # Entry listeners may reconcile eagerly while HA persists the result.
            # Publish the validated candidate before they can create a second one.
            runtime.sip.adopt_component(
                f"pending_trunk:{self._identity}", self._candidate,
                closer=self._candidate.stop,
            )
        if self._current is not None:
            result = self.async_update_and_abort(entry, self._current, data=data, title=self._name)
        else:
            result = self.async_create_entry(title=self._name, data=data, unique_id=f"trunk:{self._identity}")
        self._saved = True
        return result

    async def _async_store_trunk(self, data):
        self._draft = dict(data)
        cfg = normalized_trunk_config(data)
        try:
            configs = entry_trunk_configs(self._get_entry())
            configs[self._identity] = cfg
            validate_static_trunk_sources(configs)
            old = sip_trunk(self.hass, self._identity)
            parent = CodecPolicy.from_config(self._get_entry().data, prefix="")
            if old is not None and old.config == trunk_client_config(cfg, parent=parent) and old.ready:
                return self._store_entry(data)
            if old is not None and trunk_has_calls(self.hass, self._identity, old):
                raise ValueError("trunk_in_use")
            if old is not None:
                # RFC 5626 keys bindings by instance and reg-id, not Contact URI.
                # Validate on the other flow so unregistering the old client
                # cannot delete the newly accepted registration.
                cfg["trunk_registration_id"] = 2 if old.config.registration_id == 1 else 1
                data["trunk_registration_id"] = cfg["trunk_registration_id"]
            local_ip = await async_advertise_host(self.hass)
            if not local_ip:
                raise ValueError("advertise_ip_unknown")
            self._candidate = create_trunk_client(self.hass, cfg, local_ip=local_ip, trunk_id=self._identity)
            await self._candidate.start()
            if not await self._candidate.wait_for_registration(timeout=15):
                raise ValueError("trunk_registration_failed")
        except (ValueError, OSError, TimeoutError) as err:
            if self._candidate is not None:
                await self._candidate.stop()
                self._candidate = None
            result = await self.async_step_trunk()
            reason = str(err)
            result["errors"] = {"base": reason if reason in {"trunk_in_use", "advertise_ip_unknown", "trunk_registration_failed"}
                                else "trunk_validation_failed"}
            return result
        except BaseException:
            if self._candidate is not None:
                await self._candidate.stop()
                self._candidate = None
            raise
        return self._store_entry(data)

    @callback
    def async_remove(self):
        candidate = self._candidate
        self._candidate = None
        if candidate is not None:
            entry = self.hass.config_entries.async_get_entry(self.handler[0])
            configs = entry_trunk_configs(entry) if entry is not None else {}
            runtime = runtime_data(self.hass)
            parent = CodecPolicy.from_config(entry.data, prefix="") if entry is not None else None
            if (self._saved and runtime is not None and runtime.sip is not None
                    and self._identity in configs and trunk_client_config(configs[self._identity], parent=parent) == candidate.config):
                name = f"pending_trunk:{self._identity}"
                previous = runtime.sip.component(name)
                if previous is candidate or runtime.sip.component(f"trunk:{self._identity}") is candidate:
                    pass
                elif previous is None:
                    runtime.sip.adopt_component(name, candidate, closer=candidate.stop)
                else:
                    self.hass.async_create_task(candidate.stop())
            else:
                if runtime is not None and runtime.sip is not None:
                    runtime.sip.release_component(f"pending_trunk:{self._identity}", candidate)
                self.hass.async_create_task(candidate.stop())
        super().async_remove()
        if self._saved:
            entry = self.hass.config_entries.async_get_entry(self.handler[0])
            if entry is not None:
                from .trunk_runtime import async_reconcile_trunks

                configs = entry_trunk_configs(entry)
                if self._identity in configs and (
                    self._make_default or entry_default_trunk(entry, configs) == self._identity
                ):
                    # Persist an empty selection even for a sole trunk; an
                    # absent key retains the legacy implicit default.
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_DEFAULT_TRUNK: self._identity if self._make_default else ""}
                    )

                # Finish the validated client's ownership handoff after HA has
                # persisted the subentry. The ordinary update listener may run
                # earlier; reconciliation is locked and idempotent.
                self.hass.async_create_task(
                    async_reconcile_trunks(self.hass, entry),
                    "voip-trunk-flow-adoption", eager_start=False,
                )
