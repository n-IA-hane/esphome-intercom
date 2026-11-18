"""Provisioning entry URLs reach declared files through the real HA owner."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest
from homeassistant.components.http import KEY_HASS
from homeassistant.helpers.storage import Store

from .test_physical_phone_runtime_ha import (
    add,
    custom_integration_path as custom_integration_path,
    hass_config_dir as hass_config_dir,
    hass_storage as hass_storage,
    phone,
    phone_lab as phone_lab,
    register_cached_account,
)

pytestmark = pytest.mark.ha


def snom_profile(path="{mac}.xml"):
    return ({"id": "snom/360", "vendor": "snom", "model": "360",
             "capabilities": {"max_lines": 1, "key_slots": [],
                              "functions": ["sip_account", "provision"], "languages": {}},
             "outputs": [{"path": path, "template": "templates/config.jinja2", "format": "xml"}]},
            {"templates/config.jinja2":
             '<settings><phone-settings>{% for line in lines %}<user_name idx="{{ line.slot }}">'
             '{{ line.username }}</user_name>{% endfor %}'
             '<setting_server>{{ provisioning.url }}</setting_server></phone-settings></settings>'})


@pytest.mark.parametrize("filename", ["{mac}.xml", "settings/phone-{mac}.xml"])
async def test_manual_snom_url_fetches_declared_xml_and_embeds_same_next_boot_url(phone_lab, filename):
    from custom_components.voip_stack.physical_phone_http import PhysicalPhoneView
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.physical_phone_services import (
        register_physical_phone_services, unregister_physical_phone_services,
    )

    lab = phone_lab
    data = phone(profile_id="snom/360", catalog_commit="b" * 40)
    add(lab, data)
    register_cached_account(lab)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    manager.store = Store(lab.hass, 1, f"voip_stack.physical_phones.{lab.entry.entry_id}", atomic_writes=True)
    manager.catalog.async_profile = AsyncMock(return_value=snom_profile(filename))
    register_physical_phone_services(lab.hass)
    try:
        await manager.async_prepare(data)
        assert manager.public_state(data["phone_id"])["state"] == "ready"
        response = await lab.hass.services.async_call("voip_stack", "get_phone_provisioning_state",
            {"phone_id": data["phone_id"], "include_url": True}, blocking=True, return_response=True)
        url = response["provisioning_url"]
        prefix = "/api/voip_stack/phone_provisioning/" + data["phone_id"] + "/" + data["provisioning_token"] + "/"
        assert urlsplit(url).path == prefix + filename.replace("{mac}", data["mac"])
        relative = urlsplit(url).path.removeprefix(prefix)
        request = SimpleNamespace(app={KEY_HASS: lab.hass})
        assert (await PhysicalPhoneView().get(request, data["phone_id"], "wrong-token", relative)).status == 404
        assert manager.public_state(data["phone_id"])["state"] == "ready"
        result = await PhysicalPhoneView().get(request, data["phone_id"], data["provisioning_token"], relative)
        assert result.status == 200 and result.content_type == "application/xml"
        xml = ElementTree.fromstring(result.body)
        assert xml.findtext("phone-settings/user_name") == "first"
        assert xml.findtext("phone-settings/setting_server") == url
        assert manager.public_state(data["phone_id"])["state"] == "downloaded"
        assert not (await PhysicalPhoneView().get(request, data["phone_id"], data["provisioning_token"], "")).status == 200
        assert all(call.kwargs == {"commit": "b" * 40} for call in manager.catalog.async_profile.call_args_list)
    finally:
        unregister_physical_phone_services(lab.hass)
        await manager.async_close()
        lab.runtime.physical_phones = None


async def test_fanvil_keeps_directory_url_without_fetching_catalog(phone_lab):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner, phone_provisioning_url
    from custom_components.voip_stack.physical_phone_services import async_provisioning_state

    lab = phone_lab
    data = phone()
    add(lab, data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    manager.catalog.async_profile = AsyncMock(side_effect=AssertionError("Fanvil URL must stay a directory"))
    try:
        call = SimpleNamespace(hass=lab.hass, data={"phone_id": data["phone_id"], "include_url": True}, context=None)
        response = await async_provisioning_state(call)
        assert response["provisioning_url"] == phone_provisioning_url(lab.hass, lab.entry, data)
        assert urlsplit(response["provisioning_url"]).path.endswith("/" + data["provisioning_token"] + "/")
        manager.catalog.async_profile.assert_not_awaited()
    finally:
        await manager.async_close()
        lab.runtime.physical_phones = None


async def test_snom_url_does_not_choose_an_arbitrary_xml_output(phone_lab):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner

    lab = phone_lab
    data = phone(profile_id="snom/360")
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    profile, templates = snom_profile()
    profile["outputs"].append({**deepcopy(profile["outputs"][0]), "path": "other.xml"})
    manager.catalog.async_profile = AsyncMock(return_value=(profile, templates))
    try:
        with pytest.raises(ValueError, match="phone_profile_not_ready"):
            await manager.async_provisioning_url(data)
    finally:
        await manager.async_close()
        lab.runtime.physical_phones = None


@pytest.mark.parametrize("legacy", [True, False])
async def test_grandstream_pnp_file_and_dhcp_directory_serve_same_xml(phone_lab, legacy):
    from custom_components.voip_stack.physical_phone_http import PhysicalPhoneView
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner

    lab = phone_lab
    data = phone(profile_id="grandstream/grp2614", catalog_commit="b" * 40)
    add(lab, data)
    register_cached_account(lab)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    profile = {"id": data["profile_id"], "vendor": "grandstream", "capabilities": {
        "max_lines": 1, "functions": ["sip_account"], "languages": {}, "key_slots": []},
        "outputs": [{"path": "{mac}.xml", "format": "xml", "template": "templates/grp.jinja2"}]}
    if not legacy:
        profile["outputs"].append({**profile["outputs"][0], "path": "cfg{mac}.xml"})
        profile["capabilities"]["delivery"] = {"pnp": "cfg{mac}.xml"}
    manager = lab.runtime.physical_phones = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    manager.store = Store(lab.hass, 1, f"voip_stack.physical_phones.{lab.entry.entry_id}", atomic_writes=True)
    manager.catalog.async_profile = AsyncMock(return_value=(profile, {"templates/grp.jinja2":
        '<gs_provision><config><P35>{{ lines[0].username }}</P35></config></gs_provision>'}))
    try:
        url = await manager.async_provisioning_url(data, method="pnp")
        setup = await manager.async_setup_urls(data)
        assert set(setup) == {"provisioning_url"}
        directory = setup["provisioning_url"]
        assert directory.endswith("/")
        direct = data["mac"] + ".xml"
        boot = "cfg" + direct
        assert url == directory + (direct if legacy else boot)
        request = SimpleNamespace(app={KEY_HASS: lab.hass})
        for filename in (direct, boot):
            result = await PhysicalPhoneView().get(request, data["phone_id"], data["provisioning_token"], filename)
            assert result.status == 200 and result.content_type == "application/xml"
            assert ElementTree.fromstring(result.body).findtext("./config/P35") == "first"
        for other in ("cfg.xml", "cfggrp2614.xml", "cfg001122334499.xml", ""):
            assert (await PhysicalPhoneView().get(request, data["phone_id"], data["provisioning_token"], other)).status == 404
    finally:
        await manager.async_close()
        lab.runtime.physical_phones = None


async def test_method_declares_output_when_other_xml_and_cfg_outputs_are_available(phone_lab):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.physical_phone_services import async_provisioning_state

    lab = phone_lab
    data = phone(profile_id="example/multiple", catalog_commit="a" * 40)
    add(lab, data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    manager = lab.runtime.physical_phones = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    profile, templates = snom_profile()
    profile.update(id=data["profile_id"], vendor="example")
    profile["outputs"].extend([{"path": "config/{mac}.cfg", "format": "xml", "template": "templates/config.jinja2"},
                               {"path": "other.xml", "format": "xml", "template": "templates/other.jinja2"}])
    profile["capabilities"]["delivery"] = {"manual": "config/{mac}.cfg"}
    manager.catalog.async_profile = AsyncMock(return_value=(profile, templates))
    try:
        args = SimpleNamespace(hass=lab.hass, data={"phone_id": data["phone_id"], "include_url": True}, context=None)
        state = await async_provisioning_state(args)
        assert state["dhcp_provisioning_url"].endswith("/")
        assert state["provisioning_url"] == state["dhcp_provisioning_url"] + "config/" + data["mac"] + ".cfg"
        manager.catalog.async_profile.assert_awaited_once_with(data["profile_id"], commit="a" * 40)
    finally:
        await manager.async_close()
        lab.runtime.physical_phones = None
