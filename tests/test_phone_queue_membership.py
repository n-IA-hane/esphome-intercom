"""Queue declarations share the existing per-phone configuration owner."""

from types import SimpleNamespace

import pytest

from .test_phone_config import _load


def test_phone_queues_accept_new_names_and_normalize_csv_without_agent_state():
    module = _load("phone_queue_membership")
    endpoint_module = _load("phone_endpoint")
    config = _load("phone_config")
    assert module.normalize_phone_queues(" Support, 700, support, ") == ("Support", "700")
    endpoint = config.endpoint_from_data(SimpleNamespace(data={}, options={}), {
        "endpoint_id": "browser:one", "name": "Desk", "kind": "browser",
        "queues": "Support, 700, support",
    })
    assert endpoint.queues == ("Support", "700")
    assert endpoint_module.PhoneEndpoint(endpoint_id="offline", name="Offline",
        kind="esphome", queues=["Support"]).queues == ("Support",)


@pytest.mark.parametrize("value", ["x" * 33, "x\ny", "x|y", ["x,y"], [1], {"Support": True},
                                   [str(index) * 32 for index in range(8)]])
def test_queue_declarations_reject_unbounded_or_ambiguous_names(value):
    with pytest.raises(ValueError):
        _load("phone_queue_membership").normalize_phone_queues(value)


def test_managed_queue_record_is_serializable_and_never_changes_sip_account():
    managed = _load("managed_phones")
    phone = managed.ManagedPhone(device_id="esp", origin_entry_id="native",
        mac="84c7bb000001", password="test-password", queues="Support, 700")
    assert phone.record()["queues"] == ["Support", "700"]
    assert not hasattr(phone.account(), "queues")
    assert managed.ManagedPhone(**phone.record()) == phone


def test_memberships_include_offline_phones_and_retained_native_config(monkeypatch):
    membership = _load("phone_queue_membership")
    runtime_data = _load("runtime_data")
    endpoint = _load("phone_endpoint").PhoneEndpoint
    entry = SimpleNamespace(subentries={"browser": SimpleNamespace(
        subentry_type="phone", data={"endpoint_id": "browser:one", "queues": ["Support"]})},
        data={"managed_esphome_phones": {"esp": {
            "device_id": "esp", "origin_entry_id": "native", "mac": "84c7bb000001",
            "password": "test-password", "username": "esp_84c7bb000001", "queues": ["700"]}}})
    runtime = SimpleNamespace(endpoints=[
        endpoint(endpoint_id="browser:one", name="Desk", kind="browser", queues="stale"),
        endpoint(endpoint_id="direct", name="Offline direct", kind="esphome", queues="Support")])
    monkeypatch.setattr(runtime_data, "runtime_data", lambda _hass: runtime)
    assert dict(membership.iter_phone_queue_memberships(object(), entry)) == {
        "browser:one": ("Support",), "esphome:esp": ("700",), "direct": ("Support",)}


def test_pending_and_reconnect_snapshots_cannot_restore_removed_memberships():
    module = _load("phone_queue_membership")
    record = {"queues": [], "membership_pending": {"queues": ""},
              "membership_observed": {"queues": "Old"}}
    stale = module.observe_native_membership(record, "queues", "Old", allow_change=True)
    assert stale["queues"] == () and stale["membership_pending"] == {"queues": ""}
    confirmed = module.observe_native_membership(stale, "queues", "")
    assert confirmed["membership_pending"] == {}
    reconnect = module.observe_native_membership(confirmed, "queues", "Old")
    assert reconnect["queues"] == () and reconnect["membership_pending"] == {"queues": ""}
    live = module.observe_native_membership(confirmed, "queues", "New local", allow_change=True)
    assert live["queues"] == ("New local",) and live["membership_pending"] == {}


def test_existing_long_ring_declarations_keep_255_byte_text_contract(monkeypatch):
    membership = _load("phone_queue_membership")
    runtime_data = _load("runtime_data")
    name = "Legacy ring group with a name longer than thirty two bytes"
    entry = SimpleNamespace(subentries={"browser": SimpleNamespace(subentry_type="phone",
        data={"endpoint_id": "browser:one", "ring_group": name})}, data={})
    monkeypatch.setattr(runtime_data, "runtime_data", lambda _hass: None)
    assert membership.membership_value("ring_group", name) == name
    assert dict(membership.iter_phone_memberships(object(), entry, "ring")) == {"browser:one": (name,)}
    assert membership.membership_value("ring_group", "x" * 255) == "x" * 255
    with pytest.raises(ValueError):
        membership.membership_value("ring_group", "x" * 256)
    with pytest.raises(ValueError):
        membership.membership_value("queues", name)
