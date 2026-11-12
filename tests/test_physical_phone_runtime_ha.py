"""Opt-in inventory, delivery ownership and genuine SIP discovery contracts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.http import KEY_HASS
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr

from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.fixture
def disable_http_server(request):
    """Keep HA's default HTTP mocks except for the real lifecycle oracle."""
    if getattr(request, "param", False):
        yield
    else:
        with patch("homeassistant.components.http.HomeAssistantHTTP.async_bind"), patch("homeassistant.components.http.HomeAssistantHTTP.start"):
            yield


def phone(**overrides):
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    return normalize_physical_phone({"name": "Desk", "mac": "00:11:22:33:44:55", "profile_id": "fanvil/x3u-pro",
        "lines": [{"slot": "1", "endpoint_id": "sip:first"}], "keys": [], **overrides})


def add(lab, data):
    sub = ConfigSubentry(subentry_type="physical_phone", title=data["name"], unique_id=data["phone_id"], data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, sub)
    return sub


async def test_default_off_does_not_create_catalog_or_services_or_discovery(phone_lab, monkeypatch):
    from custom_components.voip_stack import physical_phone_provisioning as module
    construct = Mock(side_effect=AssertionError("Opt-out performed IO"))
    monkeypatch.setattr(module, "PhoneProfileCatalog", construct)
    await module.async_reconcile_physical_phones(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.physical_phones is None
    assert not phone_lab.hass.services.has_service("voip_stack", "provision_phone")
    construct.assert_not_called()


async def test_physical_device_has_own_identity_and_preserves_logical_endpoint_device(phone_lab):
    from custom_components.voip_stack.physical_phone import sync_physical_phone_devices, physical_phone_for_device
    data = phone()
    add(phone_lab, data)
    sync_physical_phone_devices(phone_lab.hass, phone_lab.entry)
    devices = dr.async_get(phone_lab.hass)
    device = next((item for item in dr.async_entries_for_config_entry(devices, phone_lab.entry.entry_id) if ("voip_stack", "physical_phone:" + data["phone_id"]) in item.identifiers), None)
    assert device is not None and device.id != phone_lab.device.id
    assert physical_phone_for_device(phone_lab.hass, phone_lab.entry, device.id)["phone_id"] == data["phone_id"]
    assert devices.async_get(phone_lab.device.id) is not None


async def test_inventory_edit_keeps_identity_token_and_binding_ownership():
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    before = phone()
    after = normalize_physical_phone({**before, "name": "Changed", "provisioning_token": "attacker"}, previous=before)
    assert after["phone_id"] == before["phone_id"]
    assert after["provisioning_token"] == before["provisioning_token"] != "attacker"
    assert after["lines"] == before["lines"]


async def test_prepare_waits_for_idle_and_drops_stale_async_result(phone_lab, monkeypatch):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    data = phone()
    sub = add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    endpoint = PhoneEndpoint(endpoint_id="sip:first", name="First", kind=EndpointKind.SIP_ACCOUNT, username="first", active_call_id="busy")
    phone_lab.runtime.endpoints.register(endpoint)
    manager = PhysicalPhoneProvisioner(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.physical_phones = manager
    manager.catalog.async_profile = AsyncMock()
    await manager.async_prepare(data)
    assert manager.states[data["phone_id"]]["state"] == "waiting_idle"
    manager.catalog.async_profile.assert_not_awaited()
    phone_lab.runtime.endpoints.release_call(endpoint.endpoint_id, "busy")
    profile = {"outputs": [{"path": "{mac}.cfg", "format": "cfg"}]}
    manager.catalog.async_profile.return_value = (profile, {})
    async def context(*_args):
        phone_lab.hass.config_entries.async_update_subentry(phone_lab.entry, sub, data={**data, "name": "New revision"})
        return {"physical": {"language_code": ""}}
    manager.async_context = context
    monkeypatch.setattr("custom_components.voip_stack.physical_phone_provisioning.render_phone_bundle", lambda *_: ({"001122334455.cfg": b"safe"}, "revision"))
    await manager.async_prepare(data)
    assert manager.files == {}
    await manager.async_close()


async def test_delivery_authentication_busy_guard_and_no_false_applied_status(phone_lab):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    data = phone()
    add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    manager = PhysicalPhoneProvisioner(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.physical_phones = manager
    manager.store = Mock()
    manager.files[data["phone_id"]] = {"001122334455.cfg": b"private-password"}
    manager.states[data["phone_id"]] = {"desired_revision": "one", "state": "ready"}
    assert await manager.async_file(data["phone_id"], "bad-token", "001122334455.cfg") is None
    assert await manager.async_file(data["phone_id"], "invalid-\u00e8", "001122334455.cfg") is None
    assert await manager.async_file(data["phone_id"], data["provisioning_token"], "source/original") is None
    assert await manager.async_file(data["phone_id"], data["provisioning_token"], "001122334455.cfg") == b"private-password"
    state = manager.public_state(data["phone_id"])
    assert state["state"] == "downloaded" and state["downloaded_revision"] == "one"
    assert "applied" not in state and "private-password" not in str(state) and data["provisioning_token"] not in str(state)
    await manager.async_close()


async def test_http_uses_ha_app_key_and_never_serves_bundle_sources(phone_lab):
    from custom_components.voip_stack.physical_phone_http import PhysicalPhoneView
    manager = SimpleNamespace(async_file=AsyncMock(return_value=b"ok"), states={})
    phone_lab.runtime.physical_phones = manager
    request = SimpleNamespace(app={KEY_HASS: phone_lab.hass})
    assert (await PhysicalPhoneView().get(request, "phone", "token", "../source/file")).status == 404
    assert (await PhysicalPhoneView().get(request, "phone", "token", "phone.cfg")).body == b"ok"
    manager.async_file.assert_awaited_once_with("phone", "token", "phone.cfg")


def subscribe():
    from custom_components.voip_stack.core import sip
    return sip.parse_message(sip.build_request("SUBSCRIBE", "sip:001122334455@224.0.1.75", [
        ("Via", "SIP/2.0/UDP 192.168.1.12:5060;branch=z9hG4bK-one;rport"),
        ("From", "<sip:001122334455@192.168.1.12>;tag=one"),
        ("To", "<sip:provision@224.0.1.75>"), ("Contact", "<sip:001122334455@192.168.1.12>"),
        ("Call-ID", "discovery-one"), ("CSeq", "1 SUBSCRIBE"), ("Event", 'ua-profile;vendor="Fanvil";model="X3U Pro"')]))


async def test_pnp_is_only_candidate_and_never_claims_an_authenticated_endpoint(phone_lab):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery, pnp_identity
    request = subscribe()
    assert pnp_identity(request, ("192.168.1.12", 5060)) == ("001122334455", "Fanvil", "X3U Pro")
    with pytest.raises(ValueError):
        pnp_identity(request, ("8.8.8.8", 5060))
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "http://192.168.1.200:8123", "allow_insecure_http": True}})
    owner = SimpleNamespace(hass=phone_lab.hass, entry=phone_lab.entry, owned=lambda: True)
    discovery = PhoneDiscovery(owner)
    discovery._start_flow = AsyncMock()
    result = await discovery.handle(request, ("192.168.1.12", 5060), "UDP")
    await phone_lab.hass.async_block_till_done()
    assert result.status == 200 and result.follow_up.method == "NOTIFY"
    assert result.follow_up.content_type == "application/url"
    candidate = discovery.candidates["001122334455"]
    assert candidate.token.encode() in result.follow_up.body and "token" not in candidate.public()
    assert not list(phone_lab.runtime.endpoints.endpoints)
    assert not phone_lab.runtime.sip.calls
    await discovery.async_close()
    assert discovery.candidates == {}


async def test_disabling_provisioning_releases_owner_files_and_cache_but_preserves_associations(phone_lab):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner, async_reconcile_physical_phones
    data = phone()
    add(phone_lab, data)
    manager = PhysicalPhoneProvisioner(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.physical_phones = manager
    manager.files[data["phone_id"]] = {"config": b"password"}
    manager.catalog.async_clear = AsyncMock()
    await async_reconcile_physical_phones(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.physical_phones is None and manager.files == {}
    manager.catalog.async_clear.assert_awaited_once()
    assert any(sub.data["phone_id"] == data["phone_id"] for sub in phone_lab.entry.subentries.values())


async def test_context_supports_profiles_without_language_and_preserves_supported_locale_on_ha_change(phone_lab, monkeypatch):
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sip_registrar import SipAccount
    data = phone()
    add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    phone_lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:first", name="First", kind=EndpointKind.SIP_ACCOUNT, username="first"))
    phone_lab.registrar.update_accounts([SipAccount("first", "First", "secret")])
    manager = PhysicalPhoneProvisioner(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.physical_phones = manager
    bare = {"capabilities": {"max_lines": 1, "languages": {}}}
    context = await manager.async_context(data, bare)
    assert context["physical"]["language"] == ""
    assert context["lines"][0]["password"] == "secret"
    phone_lab.hass.config.language = "zh-Hans"
    manager.states[data["phone_id"]] = {"language_code": "it"}
    supported = {"capabilities": {"max_lines": 1, "languages": {"it": "Italian", "en": "English"}}}
    context = await manager.async_context(data, supported)
    assert context["physical"]["language"] == "Italian"
    await manager.async_close()


async def test_start_reconcile_and_disable_cancel_one_worker_and_keep_ordinary_sip_owner(phone_lab, monkeypatch, socket_enabled):
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(phone_lab.hass, "http", {})
    data = phone()
    sub = add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("no multicast")))
    prepare = AsyncMock()
    monkeypatch.setattr(module.PhysicalPhoneProvisioner, "async_prepare", prepare)
    await module.async_reconcile_physical_phones(phone_lab.hass, phone_lab.entry)
    await phone_lab.hass.async_block_till_done()
    manager = phone_lab.runtime.physical_phones
    assert manager.discovery is None and manager.discovery_error == "phone_discovery_unavailable"
    assert prepare.await_count == 1
    await manager.async_reconcile()
    await phone_lab.hass.async_block_till_done()
    assert prepare.await_count == 1
    phone_lab.hass.config_entries.async_update_subentry(phone_lab.entry, sub, data={**data, "name": "New name"})
    await manager.async_reconcile()
    await phone_lab.hass.async_block_till_done()
    assert prepare.await_count == 2
    manager.catalog.async_clear = AsyncMock()
    original = phone_lab.runtime.sip
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data, "phone_provisioning": {"enabled": False}})
    await module.async_reconcile_physical_phones(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.sip is original and phone_lab.runtime.physical_phones is None
    assert not phone_lab.hass.services.has_service("voip_stack", "provision_phone")


@pytest.mark.parametrize("disable_http_server", [True], indirect=True)
async def test_provisioning_opt_in_after_real_http_start_serves_only_current_owner(phone_lab, monkeypatch, socket_enabled, unused_tcp_port, disable_http_server):
    from aiohttp import ClientSession
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    lab = phone_lab
    data = phone()
    add(lab, data)
    original_sip = lab.runtime.sip
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("no multicast")))
    monkeypatch.setattr(module.PhysicalPhoneProvisioner, "async_prepare", AsyncMock())
    assert await async_setup_component(lab.hass, "http", {
        "http": {"server_host": ["127.0.0.1"], "server_port": unused_tcp_port}})
    # HA's server lifecycle permits late views; raw TestServer freezes its router.
    await lab.hass.http.start()
    url = f"http://127.0.0.1:{unused_tcp_port}/api/voip_stack/phone_provisioning/{data['phone_id']}/{data['provisioning_token']}/phone.cfg"
    try:
        async with ClientSession() as client:
            async with client.get(url) as response:
                assert response.status == 404
            assert lab.runtime.physical_phones is None
            lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
                "phone_provisioning": {"enabled": True}})
            await module.async_reconcile_physical_phones(lab.hass, lab.entry)
            await lab.hass.async_block_till_done()
            manager = lab.runtime.physical_phones
            manager.files[data["phone_id"]] = {"phone.cfg": b"fixture-config"}
            async with client.get(url) as response:
                assert response.status == 200 and await response.read() == b"fixture-config"
            lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
                "phone_provisioning": {"enabled": False}})
            await module.async_reconcile_physical_phones(lab.hass, lab.entry)
            async with client.get(url) as response:
                assert response.status == 404
            assert lab.runtime.sip is original_sip and lab.runtime.physical_phones is None
            assert not manager.files and not manager._unsubscribers
    finally:
        if lab.runtime.physical_phones is not None:
            await lab.runtime.physical_phones.async_close()
            lab.runtime.physical_phones = None
        await lab.hass.http.stop()


async def test_existing_sip_application_admits_only_opted_in_discovery_before_account_auth(phone_lab):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.sip_application import SipApplicationMethods
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    owner = SimpleNamespace(hass=phone_lab.hass, entry=phone_lab.entry, owned=lambda: True)
    owner.discovery = PhoneDiscovery(owner)
    owner.discovery._start_flow = AsyncMock()
    phone_lab.runtime.physical_phones = owner
    application = SipApplicationMethods(phone_lab.hass, phone_lab.registrar)
    request = subscribe()
    accepted = await application.handle(request, ("192.168.1.12", 5060), "UDP")
    assert accepted.status == 200 and accepted.follow_up.method == "NOTIFY"
    phone_lab.runtime.physical_phones = None
    assert (await application.handle(request, ("192.168.1.12", 5060), "UDP")).status == 403
    await owner.discovery.async_close()
    application.account_unsubscribe()


async def test_untrusted_mac_announcement_cannot_redisclose_existing_download_credential(phone_lab):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    data = phone()
    add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    owner = SimpleNamespace(hass=phone_lab.hass, entry=phone_lab.entry, owned=lambda: True, schedule=Mock())
    discovery = PhoneDiscovery(owner)
    result = await discovery.handle(subscribe(), ("192.168.1.99", 5060), "UDP")
    assert result.status == 200 and result.follow_up is None
    await discovery.async_close()


async def test_ignored_discovery_metadata_is_never_the_pbx_configuration_owner(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.voip_stack.store import config_entry
    ignored = MockConfigEntry(domain="voip_stack", source="ignore", unique_id="physical_phone:001122334455", data={})
    ignored.add_to_hass(hass)
    pbx = MockConfigEntry(domain="voip_stack", source="user", data={"sip_port": 5060})
    pbx.add_to_hass(hass)
    assert config_entry(hass) is pbx
