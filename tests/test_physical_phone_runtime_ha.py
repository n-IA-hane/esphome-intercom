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
def hass_storage():
    """Use genuine Store I/O for the durable ownership contract in this module."""
    return {}


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    """Keep genuine Store writes isolated from the installed test fixture tree."""
    return hass_tmp_config_dir


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


def cached_profile():
    """Replace only the external catalog, keeping the production renderer."""
    return ({"vendor": "fanvil", "capabilities": {"max_lines": 1, "key_slots": [],
        "functions": ["sip_account"], "languages": {}},
        "outputs": [{"path": "{mac}.cfg", "template": "templates/config.jinja2", "format": "cfg"}]},
        {"templates/config.jinja2":
            "sip.username={{ lines[0].username }}\nsip.password={{ lines[0].password }}\n"})


def register_cached_account(lab):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.sip_registrar import SipAccount
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:first", name="First",
        kind=EndpointKind.SIP_ACCOUNT, username="first"))
    lab.registrar.update_accounts([SipAccount("first", "First", "fixture-only-password")])


async def test_discovered_address_is_only_a_hint_and_manual_management_override_can_be_cleared():
    from custom_components.voip_stack.physical_phone import normalize_physical_phone
    from custom_components.voip_stack.physical_phone_discovery import PhoneCandidate

    candidate = PhoneCandidate("", "000413000001", "192.168.10.67", "snom", "snom360")
    seed = candidate.setup_data()
    assert candidate.public()["host"] == candidate.host and "host" not in seed
    assert "host" not in candidate.native_subentry.data
    data = normalize_physical_phone({**seed, "name": "Snom", "profile_id": "snom/360", "lines": [], "keys": []},
        previous=seed)
    assert data["host"] == ""
    manual = normalize_physical_phone({**data, "host": "phone-admin.local"}, previous=data)
    assert manual["host"] == "phone-admin.local"
    assert normalize_physical_phone({**manual, "host": ""}, previous=manual)["host"] == ""
    assert normalize_physical_phone({key: value for key, value in manual.items() if key != "host"},
        previous=manual)["host"] == "phone-admin.local"


async def test_snom_pnp_adoption_delivery_survives_dhcp_hint_change_without_authorizing_new_source(phone_lab, monkeypatch):
    from dataclasses import replace
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.physical_phone import draft_physical_subentry, normalize_physical_phone
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.physical_phone_enrollment import phone_dhcp_host
    from .test_physical_phone_dhcp_ha import native_cache

    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    discovery = lab.runtime.phone_discovery = PhoneDiscovery(lab.hass, lab.entry)
    monkeypatch.setattr(discovery, "_start_flow", AsyncMock())
    original, changed, mac = "192.168.10.67", "192.168.10.68", "000413000001"
    uri = f"sip:MAC%3a{mac}@localhost"
    request = sip.SipMessage(method="SUBSCRIBE", uri=uri, headers=(
        ("From", f"<{uri}>;tag=one"), ("Contact", f"<sip:{original}:5060>"),
        ("Event", 'ua-profile;vendor="snom";model="snom360"')))
    try:
        assert (await discovery.handle(request, (original, 5060), "UDP")).follow_up is None
        candidate = discovery.candidates[mac]
        # DHCP refreshes an address hint after the actual PnP announcement.
        watcher = native_cache(lab)
        watcher.async_process_client(changed, "snom360-000001", mac)
        discovery.observe_dhcp(DhcpServiceInfo(ip=changed, hostname="snom360-000001", macaddress=mac))
        assert candidate.host == changed and candidate.pnp_host == original
        seed = candidate.setup_data()
        data = normalize_physical_phone({**seed, "name": "Snom", "profile_id": "snom/360", "lines": [], "keys": []},
            previous=seed)
        assert data["host"] == ""
        record = draft_physical_subentry(data, previous=candidate.native_subentry, title=data["name"])
        lab.hass.config_entries.async_add_subentry(lab.entry, record)
        manager = lab.runtime.physical_phones = SimpleNamespace(owned=lambda: True, schedule=Mock(),
            async_provisioning_url=AsyncMock(return_value=f"https://ha.example.test/{candidate.token}/{mac}.xml"))
        accepted = await discovery.handle(request, (original, 5060), "UDP")
        assert accepted.status == 200 and accepted.follow_up is not None
        assert candidate.token.encode() in accepted.follow_up.body
        indirect = replace(request, headers=(*request.headers, ("Record-Route", "<sip:router.example.test;lr>")))
        assert (await discovery.handle(indirect, (original, 5060), "UDP")).follow_up is None
        assert phone_dhcp_host(lab.hass, data) == changed
        assert candidate.pnp_host == original
        manager.schedule.reset_mock()
        rejected = await discovery.handle(request, (changed, 5060), "UDP")
        assert rejected.status == 200 and rejected.follow_up is None
        manager.schedule.assert_not_called()
    finally:
        await discovery.async_close()
        lab.runtime.phone_discovery = None
        lab.runtime.physical_phones = None


async def test_dhcp_address_hint_does_not_choose_management_target_or_receive_admin_credentials(phone_lab, monkeypatch):
    from custom_components.voip_stack.physical_phone_enrollment import phone_dhcp_host
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from custom_components.voip_stack.sip_registrar import SipRegistration
    from .test_physical_phone_dhcp_ha import native_cache

    lab = phone_lab
    register_cached_account(lab)
    data = phone(mac="000413000001", profile_id="snom/360", host="", admin_username="phone-admin", admin_password="fixture-admin-secret")
    watcher = native_cache(lab)
    watcher.async_process_client("192.168.10.67", "snom360-000001", data["mac"])
    assert phone_dhcp_host(lab.hass, data) == "192.168.10.67"
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    try:
        with pytest.raises(ValueError, match="phone_apply_target_unverified"):
            await manager._apply_target(data, {"port": 80})
        # Management follows a current authenticated registrar binding, never
        # the stale unauthenticated DHCP hint or a persisted discovery IP.
        contacts = [SipRegistration("first", "sip:first@192.168.10.68", "192.168.10.68", 5060,
            "UDP", float("inf"), instance_id="urn:uuid:phone")]
        monkeypatch.setattr(lab.registrar, "registered_contacts", lambda _username: contacts)
        origin, instance = await manager._apply_target(data, {"port": 80})
        assert origin == "http://192.168.10.68" and instance == "urn:uuid:phone"
        assert data["host"] == "" and not lab.sent and not lab.events
    finally:
        await manager.async_close()


async def test_provisioning_off_keeps_discovery_without_importing_catalog_worker_or_http(phone_lab, monkeypatch):
    import builtins
    from custom_components.voip_stack import physical_phone_runtime as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery

    start = AsyncMock()
    monkeypatch.setattr(PhoneDiscovery, "async_start", start)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name.rsplit(".", 1)[-1] not in {"physical_phone_provisioning", "phone_profile_catalog", "physical_phone_http"}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    discovery = phone_lab.runtime.phone_discovery
    assert discovery is not None and discovery.owned()
    assert phone_lab.runtime.physical_phones is None
    assert not phone_lab.hass.services.has_service("voip_stack", "provision_phone")
    start.assert_awaited_once()
    await discovery.async_close()
    phone_lab.runtime.phone_discovery = None


async def test_disabled_provisioning_account_edit_keeps_bindings_without_catalog(phone_lab, monkeypatch):
    from custom_components.voip_stack.physical_phone import async_prepare_account_bindings, account_physical_bindings
    from custom_components.voip_stack import phone_profile_catalog

    data = phone()
    add(phone_lab, data)
    construct = Mock(side_effect=AssertionError("Disabled account edit constructed a catalog"))
    monkeypatch.setattr(phone_profile_catalog, "PhoneProfileCatalog", construct)
    rows = account_physical_bindings(phone_lab.entry, "sip:first")
    assert await async_prepare_account_bindings(phone_lab.hass, phone_lab.entry, "sip:first", rows,
        account_data={"name": "Renamed account"}) == ()
    with pytest.raises(ValueError, match="phone_provisioning_disabled"):
        await async_prepare_account_bindings(phone_lab.hass, phone_lab.entry, "sip:first", [])
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
    from custom_components.voip_stack.sip_registrar import SipAccount
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
    phone_lab.registrar.update_accounts([SipAccount("first", "First", "fixture-only-password")])
    phone_lab.runtime.endpoints.release_call(endpoint.endpoint_id, "busy")
    profile = {"capabilities": {"max_lines": 1, "key_slots": []},
        "outputs": [{"path": "{mac}.cfg", "format": "cfg"}]}
    manager.catalog.async_profile.return_value = (profile, {})
    async def context(*_args):
        phone_lab.hass.config_entries.async_update_subentry(phone_lab.entry, sub, data={**data, "name": "New revision"})
        return {"physical": {"language_code": ""}}
    manager.async_context = context
    monkeypatch.setattr("custom_components.voip_stack.physical_phone_provisioning.render_phone_bundle", lambda *_: ({"001122334455.cfg": b"safe"}, "revision"))
    await manager.async_prepare(data)
    assert manager.files == {}
    await manager.async_close()


async def test_delivery_authentication_busy_guard_and_no_false_applied_status(cached_physical_config):
    fixture = cached_physical_config
    manager, data = fixture.manager, fixture.data
    revision = manager.public_state(data["phone_id"])["desired_revision"]
    assert await manager.async_file(data["phone_id"], "bad-token", "001122334455.cfg") is None
    assert await manager.async_file(data["phone_id"], "invalid-\u00e8", "001122334455.cfg") is None
    assert await manager.async_file(data["phone_id"], data["provisioning_token"], "source/original") is None
    assert await manager.async_file(data["phone_id"], data["provisioning_token"], "001122334455.cfg") == b"sip.username=first\nsip.password=fixture-only-password\n"
    state = manager.public_state(data["phone_id"])
    assert state["state"] == "downloaded" and state["downloaded_revision"] == revision
    assert "applied" not in state and "fixture-only-password" not in str(state) and data["provisioning_token"] not in str(state)


async def test_http_uses_ha_app_key_and_never_serves_bundle_sources(phone_lab):
    from custom_components.voip_stack.physical_phone_http import PhysicalPhoneView
    manager = SimpleNamespace(async_file=AsyncMock(return_value=b"ok"), states={})
    phone_lab.runtime.physical_phones = manager
    request = SimpleNamespace(app={KEY_HASS: phone_lab.hass},
        transport=SimpleNamespace(get_extra_info=lambda key: ("192.0.2.10", 1234) if key == "peername" else None))
    assert (await PhysicalPhoneView().get(request, "phone", "token", "../source/file")).status == 404
    assert (await PhysicalPhoneView().get(request, "phone", "token", "phone.cfg")).body == b"ok"
    manager.async_file.assert_awaited_once_with("phone", "token", "phone.cfg", peer_host="192.0.2.10")


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
    discovery = PhoneDiscovery(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.phone_discovery = discovery
    discovery._start_flow = AsyncMock()
    result = await discovery.handle(request, ("192.168.1.12", 5060), "UDP")
    await phone_lab.hass.async_block_till_done()
    assert result.status == 200 and result.follow_up is None
    candidate = discovery.candidates["001122334455"]
    assert candidate.token == "" and candidate.native_subentry is None
    assert "provisioning_token" not in candidate.public() and "phone_id" not in candidate.public()
    assert not list(phone_lab.runtime.endpoints.endpoints)
    assert not phone_lab.runtime.sip.calls
    await discovery.async_close()
    assert discovery.candidates == {}


async def test_disabling_provisioning_releases_owner_files_and_cache_but_preserves_associations(cached_physical_config):
    from custom_components.voip_stack.physical_phone_provisioning import async_reconcile_physical_phones
    fixture = cached_physical_config
    phone_lab, data, manager = fixture.lab, fixture.data, fixture.manager
    manager.catalog.async_clear = AsyncMock()
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": False}})
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
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(phone_lab.hass, "http", {})
    data = phone()
    sub = add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    prepare = AsyncMock()
    monkeypatch.setattr(module.PhysicalPhoneProvisioner, "async_prepare", prepare)
    await module.async_reconcile_physical_phones(phone_lab.hass, phone_lab.entry)
    await phone_lab.hass.async_block_till_done()
    manager = phone_lab.runtime.physical_phones
    assert not hasattr(manager, "discovery")
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


async def test_discovery_flags_reconcile_without_replacing_sip_or_creating_a_worker(phone_lab, monkeypatch):
    import asyncio
    from custom_components.voip_stack import physical_phone_runtime as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery

    start = AsyncMock()
    monkeypatch.setattr(PhoneDiscovery, "async_start", start)
    engine = phone_lab.runtime.sip
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    first = phone_lab.runtime.phone_discovery
    pending = asyncio.create_task(asyncio.Event().wait())
    first._tasks.add(pending)
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.phone_discovery is first
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.phone_discovery is None and first._closed and pending.cancelled()
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": True}})
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    second = phone_lab.runtime.phone_discovery
    assert second is not first and second.owned() and not first.owned()
    assert phone_lab.runtime.sip is engine and phone_lab.runtime.physical_phones is None
    await second.async_close()
    phone_lab.runtime.phone_discovery = None


async def test_discovery_start_failure_is_independent_of_provisioning_worker(phone_lab, monkeypatch, caplog):
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack import physical_phone_runtime as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner

    assert await async_setup_component(phone_lab.hass, "http", {})
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("no multicast")))
    monkeypatch.setattr(PhysicalPhoneProvisioner, "async_prepare", AsyncMock())
    engine = phone_lab.runtime.sip
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True}})
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    manager = phone_lab.runtime.physical_phones
    assert manager is not None and manager.owned() and phone_lab.runtime.phone_discovery is None
    assert phone_lab.hass.services.has_service("voip_stack", "provision_phone")
    assert phone_lab.runtime.sip is engine and "Phone discovery unavailable" in caplog.text
    manager.catalog.async_clear = AsyncMock()
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": False, "discovery_enabled": False}})
    await module.async_reconcile_physical_phone_runtime(phone_lab.hass, phone_lab.entry)
    assert phone_lab.runtime.physical_phones is None and manager._closed
    assert not phone_lab.hass.services.has_service("voip_stack", "provision_phone")


async def test_disabling_provisioning_preserves_the_independent_discovery(cached_physical_config, monkeypatch):
    from custom_components.voip_stack import physical_phone_runtime as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery

    state = cached_physical_config
    lab = state.lab
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock())
    state.manager.catalog.async_clear = AsyncMock()
    engine = lab.runtime.sip
    await module.async_reconcile_physical_phone_runtime(lab.hass, lab.entry)
    discovery = lab.runtime.phone_discovery
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": False}})
    await module.async_reconcile_physical_phone_runtime(lab.hass, lab.entry)
    assert lab.runtime.phone_discovery is discovery and discovery.owned() and not discovery._closed
    assert lab.runtime.physical_phones is None and state.manager._closed and not state.manager.files
    assert lab.runtime.sip is engine and not lab.hass.services.has_service("voip_stack", "provision_phone")
    await discovery.async_close()
    lab.runtime.phone_discovery = None


async def test_setup_retry_closes_old_discovery_before_the_entry_is_visible(phone_lab, monkeypatch):
    from custom_components import voip_stack
    from custom_components.voip_stack import phone_config
    from custom_components.voip_stack.const import CONF_INITIAL_PHONE_CREATED

    class StopBeforeNewRuntime(Exception):
        pass

    discovery = phone_lab.runtime.phone_discovery = SimpleNamespace(async_close=AsyncMock())
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry,
        data={**phone_lab.entry.data, CONF_INITIAL_PHONE_CREATED: True})
    monkeypatch.setattr(voip_stack, "_runtime_data", lambda _: None)
    monkeypatch.setattr(phone_config, "migrate_browser_accounts", Mock(side_effect=StopBeforeNewRuntime))
    with pytest.raises(StopBeforeNewRuntime):
        await voip_stack.async_setup_entry(phone_lab.hass, phone_lab.entry)
    discovery.async_close.assert_awaited_once()
    assert phone_lab.runtime.phone_discovery is None


@pytest.mark.parametrize("disable_http_server", [True], indirect=True)
async def test_provisioning_opt_in_after_real_http_start_serves_only_current_owner(phone_lab, monkeypatch, socket_enabled, unused_tcp_port, disable_http_server):
    from aiohttp import ClientSession
    from homeassistant.setup import async_setup_component
    from custom_components.voip_stack import physical_phone_provisioning as module
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.phone_profile_catalog import PhoneProfileCatalog
    lab = phone_lab
    data = phone()
    add(lab, data)
    register_cached_account(lab)
    original_sip = lab.runtime.sip
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("no multicast")))
    monkeypatch.setattr(PhoneProfileCatalog, "async_profile", AsyncMock(return_value=cached_profile()))
    assert await async_setup_component(lab.hass, "http", {
        "http": {"server_host": ["127.0.0.1"], "server_port": unused_tcp_port}})
    # HA's server lifecycle permits late views; raw TestServer freezes its router.
    await lab.hass.http.start()
    url = f"http://127.0.0.1:{unused_tcp_port}/api/voip_stack/phone_provisioning/{data['phone_id']}/{data['provisioning_token']}/001122334455.cfg"
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
            async with client.get(url) as response:
                assert response.status == 200 and await response.read() == b"sip.username=first\nsip.password=fixture-only-password\n"
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
    discovery = PhoneDiscovery(phone_lab.hass, phone_lab.entry)
    discovery._start_flow = AsyncMock()
    phone_lab.runtime.phone_discovery = discovery
    application = SipApplicationMethods(phone_lab.hass, phone_lab.registrar)
    request = subscribe()
    accepted = await application.handle(request, ("192.168.1.12", 5060), "UDP")
    assert accepted.status == 200 and accepted.follow_up is None
    phone_lab.runtime.phone_discovery = None
    assert (await application.handle(request, ("192.168.1.12", 5060), "UDP")).status == 403
    await discovery.async_close()
    application.account_unsubscribe()


async def test_untrusted_mac_announcement_cannot_redisclose_existing_download_credential(phone_lab):
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    data = phone()
    add(phone_lab, data)
    phone_lab.hass.config_entries.async_update_entry(phone_lab.entry, data={**phone_lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    discovery = PhoneDiscovery(phone_lab.hass, phone_lab.entry)
    phone_lab.runtime.phone_discovery = discovery
    result = await discovery.handle(subscribe(), ("192.168.1.99", 5060), "UDP")
    assert result.status == 200 and result.follow_up is None
    await discovery.async_close()


async def test_ignored_discovery_metadata_is_never_the_pbx_configuration_owner(hass):
    from pytest_homeassistant.common import MockConfigEntry
    from custom_components.voip_stack.store import config_entry
    ignored = MockConfigEntry(domain="voip_stack", source="ignore", unique_id="physical_phone:001122334455", data={})
    ignored.add_to_hass(hass)
    pbx = MockConfigEntry(domain="voip_stack", source="user", data={"sip_port": 5060})
    pbx.add_to_hass(hass)
    assert config_entry(hass) is pbx


@pytest.fixture
async def cached_physical_config(phone_lab):
    """Prepare genuine renderer output before testing a later authorization change."""
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    from homeassistant.helpers.storage import Store

    lab = phone_lab
    data = phone()
    subentry = add(lab, data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://ha.example.test"}})
    register_cached_account(lab)
    manager = PhysicalPhoneProvisioner(lab.hass, lab.entry)
    lab.runtime.physical_phones = manager
    manager.store = Store(lab.hass, 1, "voip_stack.physical_phones." + lab.entry.entry_id, atomic_writes=True)
    manager.store.async_save = AsyncMock(wraps=manager.store.async_save)
    manager.catalog.async_profile = AsyncMock(return_value=cached_profile())
    await manager.async_prepare(data)
    filename = "001122334455.cfg"
    initial = await manager.async_file(data["phone_id"], data["provisioning_token"], filename)
    assert b"sip.username=first" in initial
    try:
        yield SimpleNamespace(lab=lab, manager=manager, data=data, subentry=subentry, filename=filename)
    finally:
        await manager.async_close()


@pytest.fixture
async def dependency_config(cached_physical_config):
    state = cached_physical_config
    profile, templates = cached_profile()
    profile["capabilities"]["functions"].extend(["provision", "ldap"])
    templates["templates/config.jinja2"] += (
        "provision={{ provisioning.url }}\nldap_port={{ directory.ldap.port }}\n")
    state.manager.catalog.async_profile.return_value = profile, templates
    state.data = {**state.data, "catalog_commit": "b" * 40}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    state.profile, state.templates = profile, templates
    return state


async def test_automatic_provisioning_url_error_reaches_sensor_and_recovers_without_exposing_secrets(cached_physical_config):
    from custom_components.voip_stack.physical_phone_entity import PhysicalPhoneProvisioningSensor
    from homeassistant.helpers.translation import async_get_translations

    state = cached_physical_config
    hass, manager = state.lab.hass, state.manager
    hass.config.internal_url = "https://ha.example.test"
    hass.config.external_url = None
    hass.config.api = None
    hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "", "allow_insecure_http": False}})
    profile, templates = cached_profile()
    profile["capabilities"]["functions"].append("provision")
    templates["templates/config.jinja2"] += "provision={{ provisioning.url }}\n"
    manager.catalog.async_profile.return_value = profile, templates
    state.data = {**state.data, "catalog_commit": "b" * 40}
    hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    await manager.async_reconcile()
    await hass.async_block_till_done()
    assert not manager.public_state(state.data["phone_id"]).get("error")

    # HA's automatic URL can disappear after the integration was configured.
    hass.config.internal_url = "http://ha.example.test:8123"
    await manager.async_reconcile()
    await hass.async_block_till_done()
    sensor = PhysicalPhoneProvisioningSensor(hass, state.lab.entry, state.data["phone_id"])
    assert sensor.native_value == "error"
    attributes = sensor.extra_state_attributes
    assert attributes["error"] == "phone_provisioning_url_unavailable"
    assert not any(secret in str(attributes) for secret in (
        "fixture-only-password", state.data["provisioning_token"], "ha.example.test"))
    assert await manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    translations = await async_get_translations(hass, "it", "entity", {"voip_stack"})
    key = "component.voip_stack.entity.sensor.physical_phone_provisioning.state_attributes.error.state.phone_provisioning_url_unavailable"
    assert "HTTPS" in translations[key] and "rete locale" in translations[key]

    hass.config.internal_url = "https://corrected.example.test"
    await manager.async_reconcile()
    await hass.async_block_till_done()
    assert sensor.native_value == "ready" and "error" not in sensor.extra_state_attributes
    output = await manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
    assert b"https://corrected.example.test/api/voip_stack/phone_provisioning/" in output


async def test_implicit_tls_directory_is_not_rendered_as_plain_ldap(dependency_config):
    state = dependency_config
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_services": {"enabled": True, "tls": True}})
    with pytest.raises(ValueError, match="phone_ldap_mode_unsupported"):
        await state.manager.async_context(state.data, state.profile)
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.manager.public_state(state.data["phone_id"])["error"] == "phone_ldap_mode_unsupported"
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_services": {"enabled": True, "tls": False}})
    await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert not state.manager.public_state(state.data["phone_id"])["error"]
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)


@pytest.fixture
async def timezone_config(cached_physical_config):
    state = cached_physical_config
    profile, templates = cached_profile()
    profile["capabilities"]["time_zones"] = {
        "Europe/Rome": {"code": 4, "name": "UTC+1", "dst_type": 1, "dst_location": 13},
        "UTC": {"code": 0, "name": "UTC", "dst_type": 0, "dst_location": 0},
    }
    templates["templates/config.jinja2"] += (
        "{% if clock %}zone={{ physical.time_zone }} code={{ clock.code }} dst={{ clock.dst_type }}\n{% endif %}")
    state.lab.hass.config.time_zone = "Europe/Rome"
    state.manager.catalog.async_profile.return_value = profile, templates
    state.data = {**state.data, "catalog_commit": "d" * 40}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    context = await state.manager.async_context(state.data, profile)
    assert context["physical"]["time_zone"] == "Europe/Rome"
    await state.manager.async_prepare(state.data)
    return state


async def test_ha_timezone_update_event_invalidates_and_regenerates_only_supported_clock(timezone_config):
    from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
    state = timezone_config
    before = state.manager.files[state.data["phone_id"]]
    count = state.manager.catalog.async_profile.await_count
    unsubscribe = state.lab.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, state.manager._language_changed)
    try:
        await state.lab.hass.config.async_update(time_zone="UTC")
        await state.lab.hass.async_block_till_done()
        body = await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
        assert b"zone=UTC code=0 dst=0" in body and b"Europe/Rome" not in body
        assert state.manager.files[state.data["phone_id"]] is not before
        assert state.manager.catalog.async_profile.await_count == count + 1
        state.lab.hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE, {"unrelated": True})
        await state.lab.hass.async_block_till_done()
        assert state.manager.catalog.async_profile.await_count == count + 1
    finally:
        unsubscribe()


async def test_timezone_change_during_render_discards_stale_clock(timezone_config, monkeypatch):
    import asyncio
    from dataclasses import replace
    from custom_components.voip_stack.physical_phone_provisioning import render_phone_bundle
    state = timezone_config
    state.lab.runtime.endpoints.upsert(replace(state.lab.runtime.endpoints.get("sip:first"), name="Changed First"))
    entered, resume = asyncio.Event(), asyncio.Event()
    original = state.lab.hass.async_add_executor_job

    async def held(target, *args):
        if target is render_phone_bundle:
            entered.set()
            await resume.wait()
        return await original(target, *args)

    monkeypatch.setattr(state.lab.hass, "async_add_executor_job", held)
    before = state.manager.files[state.data["phone_id"]]
    task = state.lab.hass.async_create_task(state.manager.async_prepare(state.data))
    try:
        await entered.wait()
        state.lab.hass.config.time_zone = "UTC"
    finally:
        resume.set()
        await task
    assert state.manager.files[state.data["phone_id"]] is before
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()
    assert b"zone=UTC code=0 dst=0" in await state.manager.async_file(
        state.data["phone_id"], state.data["provisioning_token"], state.filename)


@pytest.mark.parametrize("declared", [False, True])
async def test_legacy_or_unmapped_timezone_preserves_existing_projection(cached_physical_config, declared):
    state = cached_physical_config
    profile, templates = cached_profile()
    if declared:
        profile["capabilities"]["time_zones"] = {"Europe/Rome": {"code": 4, "name": "UTC+1"}}
    state.lab.hass.config.language = "it"
    state.lab.hass.config.time_zone = "Pacific/Auckland"
    state.data = {**state.data, "catalog_commit": "e" * 40}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    state.manager.catalog.async_profile.return_value = profile, templates
    assert (await state.manager.async_context(state.data, profile))["physical"]["time_zone"] == ""
    await state.manager.async_prepare(state.data)
    before = state.manager.files[state.data["phone_id"]]
    state.lab.hass.config.time_zone = "Asia/Tokyo"
    state.manager._language_changed(SimpleNamespace(data={"time_zone": "Asia/Tokyo"}))
    await state.lab.hass.async_block_till_done()
    assert state.manager.files[state.data["phone_id"]] is before
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) == (
        b"sip.username=first\nsip.password=fixture-only-password\n")


@pytest.mark.parametrize("dependency", ["url", "ldap_port"])
async def test_shared_configuration_callback_regenerates_unchanged_phone(dependency_config, dependency):
    from custom_components.voip_stack.physical_phone_provisioning import async_reconcile_physical_phones
    state = dependency_config
    data = dict(state.lab.entry.data)
    if dependency == "url":
        data["phone_provisioning"] = {**data["phone_provisioning"], "base_url": "https://new.example.test"}
        expected = b"provision=https://new.example.test/"
    else:
        data["phone_services"] = {"enabled": True, "ldap_port": 2389}
        expected = b"ldap_port=2389"
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data=data)
    await async_reconcile_physical_phones(state.lab.hass, state.lab.entry)
    await state.lab.hass.async_block_till_done()
    delivered = await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
    assert expected in delivered


@pytest.mark.parametrize("dependency", ["url", "ldap_port"])
async def test_shared_change_invalidates_delivery_before_worker(dependency_config, dependency):
    state = dependency_config
    data = dict(state.lab.entry.data)
    if dependency == "url":
        data["phone_provisioning"] = {**data["phone_provisioning"], "base_url": "https://new.example.test"}
    else:
        data["phone_services"] = {"enabled": True, "ldap_port": 2389}
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data=data)
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()


async def test_global_change_while_render_waits_drops_stale_bytes(dependency_config, monkeypatch):
    import asyncio
    from dataclasses import replace
    from custom_components.voip_stack.physical_phone_provisioning import render_phone_bundle
    state = dependency_config
    state.lab.runtime.endpoints.upsert(replace(state.lab.runtime.endpoints.get("sip:first"), name="Changed First"))
    entered, resume = asyncio.Event(), asyncio.Event()
    original = state.lab.hass.async_add_executor_job

    async def held(target, *args):
        if target is render_phone_bundle:
            entered.set()
            await resume.wait()
        return await original(target, *args)

    monkeypatch.setattr(state.lab.hass, "async_add_executor_job", held)
    previous = state.manager.files[state.data["phone_id"]]
    task = state.lab.hass.async_create_task(state.manager.async_prepare(state.data))
    try:
        await entered.wait()
        state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
            "phone_services": {"enabled": True, "ldap_port": 2389}})
    finally:
        resume.set()
        await task
    assert state.manager.files[state.data["phone_id"]] is previous
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()


async def test_dependency_snapshot_is_immutable_secret_free_and_coalesces_identical_inputs(dependency_config):
    from dataclasses import FrozenInstanceError
    state = dependency_config
    prepared = state.manager.files[state.data["phone_id"]]
    assert "fixture-only-password" not in str(prepared.dependencies.context)
    with pytest.raises(FrozenInstanceError):
        prepared.dependencies.profile = "replacement"
    saves = state.manager.store.async_save.await_count
    for _ in range(3):
        await state.manager.async_reconcile()
    await state.lab.hass.async_block_till_done()
    assert state.manager.files[state.data["phone_id"]] is prepared
    assert state.manager.store.async_save.await_count == saves


async def test_unchanged_callback_skips_catalog_but_global_change_still_renders(dependency_config):
    from custom_components.voip_stack.physical_phone_provisioning import async_reconcile_physical_phones
    state = dependency_config
    count = state.manager.catalog.async_profile.await_count
    state.manager._language_changed(SimpleNamespace(data={"unrelated": True}))
    state.manager.schedule()
    await state.lab.hass.async_block_till_done()
    assert state.manager.catalog.async_profile.await_count == count
    state.lab.hass.config_entries.async_update_entry(state.lab.entry, data={**state.lab.entry.data,
        "phone_provisioning": {"enabled": True, "base_url": "https://next.example.test"}})
    await async_reconcile_physical_phones(state.lab.hass, state.lab.entry)
    await state.lab.hass.async_block_till_done()
    assert state.manager.catalog.async_profile.await_count == count + 1
    body = await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
    assert b"provision=https://next.example.test/" in body


@pytest.fixture
async def managed_slot_config(cached_physical_config):
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.sip_registrar import SipAccount
    state = cached_physical_config
    state.lab.registrar.update_accounts([SipAccount("first", "First", "fixture-only-password"),
        SipAccount("second", "Second", "second-fixture-password")])
    state.lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:second", name="Second",
        kind=EndpointKind.SIP_ACCOUNT, username="second"))
    data = {**state.data, "lines": [*state.data["lines"], {"slot": "2", "endpoint_id": "sip:second"}],
            "keys": [{"slot": "1", "line": "1", "function": "speed_dial", "target": "00123", "label": "Desk"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=data)
    profile, _ = cached_profile()
    profile["capabilities"].update(max_lines=2, clear_keys=True, disable_lines=True,
        functions=["sip_account", "speed_dial"], key_slots=[{"slot": str(slot), "functions": ["speed_dial"],
            "max_value_bytes": 31, "max_label_bytes": 31} for slot in range(1, 4)])
    templates = {"templates/config.jinja2": """{% for line in lines %}line{{ line.slot }}.enabled=1
{% endfor %}{% for slot in retired['lines'] %}line{{ slot }}.enabled=0
{% endfor %}{% for key in keys %}key{{ key.slot }}={{ key.dial_target }}
{% endfor %}{% for slot in retired['keys'] %}key{{ slot }}=
{% endfor %}"""}
    state.manager.catalog.async_profile.return_value = profile, templates
    await state.manager.async_prepare(data)
    state.data, state.profile, state.templates = data, profile, templates
    return state


async def test_retired_slots_clear_only_owned_values_and_remain_owned_after_download(managed_slot_config):
    state = managed_slot_config
    removed = {**state.data, "keys": [], "lines": state.data["lines"][:1]}
    assert state.manager.validate_inventory_change(removed, state.profile) == {"keys": ["1"], "lines": ["2"]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=removed)
    await state.manager.async_prepare(removed)
    first = await state.manager.async_file(removed["phone_id"], removed["provisioning_token"], state.filename)
    assert b"key1=\n" in first and b"line2.enabled=0" in first
    assert b"key2=" not in first and b"key3=" not in first
    await state.manager.async_prepare(removed)
    assert await state.manager.async_file(removed["phone_id"], removed["provisioning_token"], state.filename) == first
    footprint = state.manager.states[removed["phone_id"]]["managed_slots"]
    assert footprint == {"mac": state.data["mac"], "keys": ["1"],
                         "lines": {"1": ["sip:first"], "2": ["sip:second"]}}


async def test_retired_line_busy_guard_includes_previous_endpoint(managed_slot_config):
    state = managed_slot_config
    removed = {**state.data, "keys": [], "lines": state.data["lines"][:1]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=removed)
    state.lab.runtime.endpoints.claim_call("sip:second", "old-owned-call")
    await state.manager.async_prepare(removed)
    assert state.manager.public_state(removed["phone_id"])["state"] == "waiting_idle"
    assert await state.manager.async_file(removed["phone_id"], removed["provisioning_token"], state.filename) is None
    state.lab.runtime.endpoints.release_call("sip:second", "old-owned-call")
    await state.manager.async_prepare(removed)
    assert b"line2.enabled=0" in await state.manager.async_file(removed["phone_id"], removed["provisioning_token"], state.filename)


@pytest.mark.parametrize("capability,error", [("clear_keys", "phone_clear_keys_unsupported"),
                                            ("disable_lines", "phone_disable_lines_unsupported")])
async def test_unsupported_slot_retirement_is_rejected_before_inventory_commit(managed_slot_config, capability, error):
    from copy import deepcopy
    state = managed_slot_config
    profile = deepcopy(state.profile)
    profile["capabilities"][capability] = False
    removed = {**state.data, "keys": [], "lines": state.data["lines"][:1]}
    with pytest.raises(ValueError, match=error):
        state.manager.validate_inventory_change(removed, profile)


async def test_footprint_write_must_finish_before_new_file_is_published(managed_slot_config):
    import asyncio
    state = managed_slot_config
    changed = {**state.data, "keys": [*state.data["keys"],
        {"slot": "2", "line": "1", "function": "speed_dial", "target": "00456", "label": "New"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)
    entered, resume = asyncio.Event(), asyncio.Event()
    original_save = state.manager.store.async_save

    async def save(_data):
        entered.set()
        await resume.wait()
        await original_save(_data)

    state.manager.store.async_save = AsyncMock(side_effect=save)
    before = state.manager.files[changed["phone_id"]]
    task = state.lab.hass.async_create_task(state.manager.async_prepare(changed))
    try:
        await entered.wait()
        assert state.manager.files[changed["phone_id"]] is before
    finally:
        resume.set()
        await task
    assert state.manager.files[changed["phone_id"]] is not before
    assert b"key2=00456" in await state.manager.async_file(changed["phone_id"], changed["provisioning_token"], state.filename)


async def test_retirement_survives_real_store_restart_without_inventing_unknown_slots(managed_slot_config, monkeypatch):
    from homeassistant.helpers.storage import Store
    from custom_components.voip_stack.physical_phone_discovery import PhoneDiscovery
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    state = managed_slot_config
    await state.manager.async_close()
    manager = PhysicalPhoneProvisioner(state.lab.hass, state.lab.entry)
    state.lab.runtime.physical_phones = manager
    manager.store = Store(state.lab.hass, 1, "voip_stack.physical_phones." + state.lab.entry.entry_id)
    manager.catalog.async_profile = AsyncMock(return_value=(state.profile, state.templates))
    await manager.async_prepare(state.data)
    durable = await manager.store.async_load()
    assert durable[state.data["phone_id"]]["managed_slots"]["keys"] == ["1"]
    removed = {**state.data, "keys": [], "lines": state.data["lines"][:1]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=removed)
    await manager.async_prepare(removed)
    await manager.async_close()
    restored = PhysicalPhoneProvisioner(state.lab.hass, state.lab.entry)
    state.lab.runtime.physical_phones = restored
    restored.catalog.async_profile = AsyncMock(return_value=(state.profile, state.templates))
    monkeypatch.setattr(PhoneDiscovery, "async_start", AsyncMock(side_effect=OSError("fixture no multicast")))
    try:
        await restored.async_start()
        await state.lab.hass.async_block_till_done()
        body = await restored.async_file(removed["phone_id"], removed["provisioning_token"], state.filename)
        assert b"key1=\n" in body and b"line2.enabled=0" in body
        assert b"key2=" not in body and b"key3=" not in body
        assert restored.busy(removed) is False
        state.lab.runtime.endpoints.claim_call("sip:second", "restored-old-line")
        assert await restored.async_file(removed["phone_id"], removed["provisioning_token"], state.filename) is None
        state.lab.runtime.endpoints.release_call("sip:second", "restored-old-line")
    finally:
        await restored.async_close()


async def test_failed_footprint_write_cannot_claim_or_publish_new_slot(managed_slot_config):
    state = managed_slot_config
    changed = {**state.data, "keys": [*state.data["keys"],
        {"slot": "2", "line": "1", "function": "speed_dial", "target": "00456", "label": "New"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)
    before = state.manager.files[changed["phone_id"]]
    state.manager.store.async_save.side_effect = OSError("fixture disk failure")
    try:
        with pytest.raises(OSError, match="fixture disk failure"):
            await state.manager.async_prepare(changed)
        assert state.manager.files[changed["phone_id"]] is before
        assert state.manager.states[changed["phone_id"]]["managed_slots"]["keys"] == ["1"]
    finally:
        state.manager.store.async_save.side_effect = None


async def test_real_store_write_error_cannot_publish_unpersisted_ownership(managed_slot_config, monkeypatch):
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    state = managed_slot_config
    await state.manager.async_close()
    manager = PhysicalPhoneProvisioner(state.lab.hass, state.lab.entry)
    state.lab.runtime.physical_phones = manager
    manager.store = Store(state.lab.hass, 1, "voip_stack.physical_phones." + state.lab.entry.entry_id)
    manager.catalog.async_profile = AsyncMock(return_value=(state.profile, state.templates))
    await manager.async_prepare(state.data)
    before = manager.files[state.data["phone_id"]]
    changed = {**state.data, "keys": [*state.data["keys"],
        {"slot": "2", "line": "1", "function": "speed_dial", "target": "00456", "label": "New"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)

    def failed_write(*_args):
        raise WriteError("fixture actual Store write failure")

    monkeypatch.setattr(manager.store._io, "_write", failed_write)
    try:
        try:
            await manager.async_prepare(changed)
        except (ValueError, OSError):
            pass
        assert manager.files[changed["phone_id"]] is before
    finally:
        await manager.async_close()


async def test_cancel_during_real_executor_write_settles_before_rollback(managed_slot_config, monkeypatch):
    import asyncio
    import json
    from pathlib import Path
    from threading import Event
    state = managed_slot_config
    changed = {**state.data, "keys": [*state.data["keys"],
        {"slot": "2", "line": "1", "function": "speed_dial", "target": "00456", "label": "New"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)
    entered, release, writes = asyncio.Event(), Event(), []
    original = state.manager.store._io._write

    def held_write(mode, payload):
        writes.append(True)
        if len(writes) == 1:
            state.lab.hass.loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("fixture executor write was not released")
        original(mode, payload)

    monkeypatch.setattr(state.manager.store._io, "_write", held_write)
    before = state.manager.files[changed["phone_id"]]
    task = state.lab.hass.async_create_task(state.manager.async_prepare(changed))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert len(writes) == 1 and not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(writes) >= 2
    assert state.manager.files[changed["phone_id"]] is before
    saved = await state.lab.hass.async_add_executor_job(
        lambda: json.loads(Path(state.manager.store.path).read_text()))
    assert saved["data"][changed["phone_id"]]["managed_slots"]["keys"] == ["1"]


@pytest.mark.parametrize("abort", ["owner_change", "cancel"])
async def test_aborted_footprint_commit_rolls_back_unpublished_claim(managed_slot_config, abort):
    import asyncio
    from copy import deepcopy
    state = managed_slot_config
    changed = {**state.data, "keys": [*state.data["keys"],
        {"slot": "2", "line": "1", "function": "speed_dial", "target": "00456", "label": "New"}]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)
    entered, resume, writes = asyncio.Event(), asyncio.Event(), []
    original_save = state.manager.store.async_save

    async def save(data):
        writes.append(deepcopy(data))
        if len(writes) == 1:
            entered.set()
            await resume.wait()
        await original_save(data)

    state.manager.store.async_save = AsyncMock(side_effect=save)
    before = state.manager.files[changed["phone_id"]]
    task = state.lab.hass.async_create_task(state.manager.async_prepare(changed))
    try:
        await entered.wait()
        if abort == "owner_change":
            state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
        else:
            task.cancel()
    finally:
        resume.set()
        if abort == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    assert state.manager.files[changed["phone_id"]] is before
    assert state.manager.states[changed["phone_id"]]["managed_slots"]["keys"] == ["1"]
    assert writes[-1][changed["phone_id"]]["managed_slots"]["keys"] == ["1"]


async def test_applied_ldap_port_and_epoch_are_delivery_dependencies(dependency_config):
    state = dependency_config
    config = {"enabled": True, "ldap_port": 4389, "base_dn": "ou=phonebook,dc=voip",
              "tls": False, "allow_anonymous": False}
    # This is the existing ESP/LDAP controller contract, not another server.
    state.lab.runtime.phone_provisioning = SimpleNamespace(_directory_config=config,
        _directory_epoch="fixture-first-listener", async_close=AsyncMock())
    await state.manager.async_prepare(state.data)
    body = await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename)
    assert b"ldap_port=4389" in body
    state.lab.runtime.phone_provisioning._directory_epoch = "fixture-next-listener"
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()
    state.lab.runtime.phone_provisioning._directory_config = None
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()


@pytest.mark.parametrize("change", ["source_ip", "language", "line_display_name"])
async def test_actual_render_context_change_invalidates_delivery(dependency_config, monkeypatch, change):
    from dataclasses import replace
    from homeassistant.components import network
    state = dependency_config
    profile = state.profile
    profile["capabilities"]["languages"] = {"en": "en", "it": "it"}
    state.data = {**state.data, "catalog_commit": "c" * 40}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=state.data)
    await state.manager.async_prepare(state.data)
    if change == "source_ip":
        state.lab.runtime.transport_config["advertise_host"] = ""
        monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.9"))
    elif change == "language":
        state.lab.hass.config.language = "it" if state.lab.hass.config.language != "it" else "en"
    else:
        state.lab.runtime.endpoints.upsert(replace(state.lab.runtime.endpoints.get("sip:first"), name="New display"))
    assert await state.manager.async_file(state.data["phone_id"], state.data["provisioning_token"], state.filename) is None
    await state.lab.hass.async_block_till_done()


@pytest.mark.parametrize("change", ["mac", "smaller_profile"])
async def test_owned_slots_cannot_be_lost_by_mac_or_profile_edit(managed_slot_config, change):
    from copy import deepcopy
    state = managed_slot_config
    proposed, profile = deepcopy(state.data), deepcopy(state.profile)
    if change == "mac":
        proposed["mac"] = "001122334466"
        error = "phone_owned_mac_change_unsupported"
    else:
        profile["capabilities"]["key_slots"] = []
        error = "phone_retired_slot_unsupported"
    with pytest.raises(ValueError, match=error):
        state.manager.validate_inventory_change(proposed, profile)


async def test_restarted_inventory_cannot_transfer_retired_slots_to_another_mac(managed_slot_config):
    from copy import deepcopy
    from custom_components.voip_stack.physical_phone_provisioning import PhysicalPhoneProvisioner
    state = managed_slot_config
    saved = deepcopy(state.manager.states)
    await state.manager.async_close()
    changed = {**state.data, "mac": "001122334466", "keys": [], "lines": state.data["lines"][:1]}
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry, data=changed)
    restored = PhysicalPhoneProvisioner(state.lab.hass, state.lab.entry)
    restored.states = saved
    restored.store = SimpleNamespace(async_save=AsyncMock())
    restored.catalog.async_profile = AsyncMock(return_value=(state.profile, state.templates))
    state.lab.runtime.physical_phones = restored
    try:
        with pytest.raises(ValueError, match="phone_owned_mac_change_unsupported"):
            await restored.async_prepare(changed)
        assert not restored.files
    finally:
        await restored.async_close()


async def test_unchanged_cached_file_becomes_available_after_busy_ends(cached_physical_config):
    state = cached_physical_config
    prepared = state.manager.files[state.data["phone_id"]]
    state.lab.runtime.endpoints.claim_call("sip:first", "busy-current-line")
    await state.manager.async_prepare(state.data)
    assert state.manager.public_state(state.data["phone_id"])["state"] == "waiting_idle"
    state.lab.runtime.endpoints.release_call("sip:first", "busy-current-line")
    await state.manager.async_prepare(state.data)
    assert state.manager.files[state.data["phone_id"]] is prepared
    assert state.manager.public_state(state.data["phone_id"])["state"] == "downloaded"


async def test_disabled_phone_denies_cached_delivery_before_reconcile_worker(cached_physical_config):
    state = cached_physical_config
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry,
        data={**state.data, "enabled": False})
    # No reconcile/worker turn: authorization must reflect the current inventory.
    assert await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename) is None


async def test_transient_profile_failure_keeps_valid_file_without_hiding_error(cached_physical_config, monkeypatch):
    from homeassistant.components import network
    state = cached_physical_config
    before = state.manager.public_state(state.data["phone_id"])
    # A transient source-IP failure records a genuine error while the cached
    # context is otherwise unchanged. Recovery must retry even identical input.
    state.lab.runtime.transport_config["advertise_host"] = ""
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(side_effect=OSError("fixture temporary route failure")))
    state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    assert state.manager.public_state(state.data["phone_id"])["state"] == "error"
    monkeypatch.setattr(network, "async_get_source_ip", AsyncMock(return_value="127.0.0.1"))
    count = state.manager.catalog.async_profile.await_count
    state.manager.catalog.async_profile.side_effect = OSError("fixture catalog temporarily unavailable")
    state.manager.schedule({state.data["phone_id"]})
    await state.lab.hass.async_block_till_done()
    assert state.manager.catalog.async_profile.await_count == count + 1
    assert state.manager.public_state(state.data["phone_id"])["state"] == "error"
    delivered = await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename)
    assert delivered == b"sip.username=first\nsip.password=fixture-only-password\n"
    after = state.manager.public_state(state.data["phone_id"])
    assert after["state"] == "error" and after["error"] == "OSError"
    assert after["desired_revision"] == before["desired_revision"]
    assert after["downloaded_revision"] == before["downloaded_revision"]


async def test_revoked_bound_account_cannot_serve_last_known_good_credentials(cached_physical_config):
    from custom_components.voip_stack.sip_registrar import SipAccount
    state = cached_physical_config
    state.lab.registrar.update_accounts([SipAccount("first", "First",
        "fixture-only-password", enabled=False)])
    # This is an owner revocation, not a transient template/network failure.
    state.manager._account_changed({"first"})
    await state.lab.hass.async_block_till_done()
    assert state.manager.public_state(state.data["phone_id"])["state"] == "error"
    assert await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename) is None


async def test_password_rotation_during_render_cannot_publish_old_credentials(cached_physical_config, monkeypatch):
    import asyncio
    from dataclasses import replace
    from custom_components.voip_stack.physical_phone_provisioning import render_phone_bundle
    from custom_components.voip_stack.sip_registrar import SipAccount

    state = cached_physical_config
    state.lab.runtime.endpoints.upsert(replace(state.lab.runtime.endpoints.get("sip:first"), name="Renamed First"))
    started, resume = asyncio.Event(), asyncio.Event()
    original_executor = state.lab.hass.async_add_executor_job

    async def held_executor(target, *args):
        if target is render_phone_bundle:
            started.set()
            await resume.wait()
        return await original_executor(target, *args)

    monkeypatch.setattr(state.lab.hass, "async_add_executor_job", held_executor)
    prepare = state.lab.hass.async_create_task(state.manager.async_prepare(state.data))
    try:
        await started.wait()
        state.lab.registrar.update_accounts([SipAccount("first", "First", "rotated-fixture-password")])
    finally:
        resume.set()
        await prepare
    assert await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename) is None
    await state.manager.async_prepare(state.data)
    delivered = await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename)
    assert delivered == b"sip.username=first\nsip.password=rotated-fixture-password\n"
    assert b"fixture-only-password" not in delivered
    current = state.manager.public_state(state.data["phone_id"])
    assert current["state"] == "downloaded" and current["error"] == ""


async def test_changed_binding_cannot_serve_previous_enabled_account_credentials(cached_physical_config):
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.sip_registrar import SipAccount

    state = cached_physical_config
    state.lab.registrar.update_accounts([
        SipAccount("first", "First", "fixture-only-password"),
        SipAccount("second", "Second", "second-fixture-password"),
    ])
    state.lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:second", name="Second",
        kind=EndpointKind.SIP_ACCOUNT, username="second"))
    state.lab.hass.config_entries.async_update_subentry(state.lab.entry, state.subentry,
        data={**state.data, "lines": [{"slot": "1", "endpoint_id": "sip:second"}]})
    assert state.lab.registrar.accounts["first"].enabled
    assert state.lab.registrar.accounts["second"].enabled
    assert await state.manager.async_file(state.data["phone_id"],
        state.data["provisioning_token"], state.filename) is None


@pytest.mark.parametrize("permanent", [False, True], ids=["reload-preserves", "remove-forgets"])
async def test_entry_store_lifetime_reuses_owners_and_cancels_delayed_writes(
        cached_physical_config, monkeypatch, permanent):
    from datetime import timedelta
    from pathlib import Path
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant.common import async_fire_time_changed_exact
    from custom_components import voip_stack
    from custom_components.voip_stack.call_queues import QueueManager
    from custom_components.voip_stack.ha_bindings import HaCardBindings

    state = cached_physical_config
    lab = state.lab
    bindings = HaCardBindings(lab.hass, lab.entry, lab.runtime, lambda _changed: None)
    lab.runtime.ha_bindings = bindings
    await bindings.async_start()
    queue_store = Store(lab.hass, 1, f"voip_stack.queues.{lab.entry.entry_id}")
    queues = lab.runtime.queues = QueueManager(lab.hass, lab.entry, store=queue_store)
    originals = (bindings._store, state.manager.store, queue_store)
    discovery = lab.runtime.phone_discovery = SimpleNamespace(async_close=AsyncMock())
    # Real Store timers must not resurrect the deleted files after removal.
    for store in originals:
        store.async_delay_save(lambda: {"pending_fixture": True}, 60)
    before = dict(lab.entry.data)
    monkeypatch.setattr(lab.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True))
    monkeypatch.setattr(voip_stack, "_async_apply_assist_intents", AsyncMock())
    monkeypatch.setattr("custom_components.voip_stack.browser_phone_runtime._async_shutdown_all", AsyncMock())
    async def stop_sip(_hass):
        assert lab.runtime.phone_discovery is None
        discovery.async_close.assert_awaited_once()
    monkeypatch.setattr(voip_stack, "_async_stop_sip_endpoint", stop_sip)
    assert await voip_stack.async_unload_entry(lab.hass, lab.entry)
    assert lab.runtime.entry_stores == originals
    assert bindings._closed and state.manager._closed and queues.closed
    discovery.async_close.assert_awaited_once()
    assert lab.runtime.phone_discovery is None
    assert all(Path(store.path).is_file() for store in originals)
    assert dict(lab.entry.data) == before
    if permanent:
        await voip_stack.async_remove_entry(lab.hass, lab.entry)
        assert lab.runtime.entry_stores == ()
        assert all(not Path(store.path).exists() for store in originals)
    async_fire_time_changed_exact(lab.hass, dt_util.utcnow() + timedelta(seconds=65))
    await lab.hass.async_block_till_done()
    if permanent:
        assert all(not Path(store.path).exists() for store in originals)
    else:
        for store in originals:
            assert await Store(lab.hass, 1, store.key).async_load() is not None
