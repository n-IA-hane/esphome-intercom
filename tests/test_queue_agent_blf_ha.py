"""Queue keys use one authenticated operator and the existing SIP notifier."""

import asyncio
from xml.etree import ElementTree

import pytest

from .test_dialog_info_ha import lab as lab, blf as blf, subscribe, NS

pytestmark = pytest.mark.ha


def declared_queue(lab, members):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.call_queues import QueueManager
    from custom_components.voip_stack.group_config import normalize_group_data
    from custom_components.voip_stack.store import config_entry

    entry = config_entry(lab.hass)
    group = ConfigSubentry(subentry_type="group", title="Support", unique_id="group:support",
        data=normalize_group_data({"kind": "queue", "name": "Support", "members": members},
                                  identity="support"))
    lab.hass.config_entries.async_add_subentry(entry, group)
    manager = QueueManager(lab.hass, entry)
    lab.runtime.queues = manager
    manager.refresh()
    assert manager.resolve("support").members == tuple(members)
    return manager, group


async def test_queue_blf_login_pause_logout_and_removal_are_real_notifications(lab, blf):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.store import config_entry
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:watcher", name="Watcher", username="watcher", kind=EndpointKind.SIP_ACCOUNT))
    manager, group = declared_queue(lab, ["sip:watcher"])
    methods, notifications, send = blf
    for control in ("login", "pause"):
        result = await methods.handle(subscribe(target=f"queue-{control}+support", call_id=f"watch-{control}"),
                                      ("127.0.0.3", 5060), "UDP", send)
        assert result.status == 200
        result.follow_up_scheduler()
        notify = await asyncio.wait_for(notifications.get(), 1)
        root = ElementTree.fromstring(notify.body)
        states = [item.text for item in root.findall("d:dialog/d:state", NS)]
        assert states == []
    # An independent directory refresh must retain the native queue declaration.
    manager.refresh()
    manager.set_agent_state("support", "sip:watcher", "paused")
    notify = await asyncio.wait_for(notifications.get(), 1)
    assert "queue-pause" in ElementTree.fromstring(notify.body).attrib["entity"]
    assert ElementTree.fromstring(notify.body).find("d:dialog/d:state", NS).text == "confirmed"
    manager.set_agent_state("support", "sip:watcher", "logged_out")
    for _ in range(2):
        notify = await asyncio.wait_for(notifications.get(), 1)
        root = ElementTree.fromstring(notify.body)
        states = [item.text for item in root.findall("d:dialog/d:state", NS)]
        assert states == (["confirmed"] if "queue-login" in root.attrib["entity"] else [])
    lab.hass.config_entries.async_remove_subentry(config_entry(lab.hass), group.subentry_id)
    manager.refresh()
    for _ in range(2):
        notify = await asyncio.wait_for(notifications.get(), 1)
        assert dict(notify.headers)["Subscription-State"] == "terminated;reason=noresource"
    assert not methods.subscriptions and methods.queue_unsubscribe is None


async def test_blf_cannot_watch_queue_membership_of_an_unrelated_operator(lab, blf):
    declared_queue(lab, ["bedroom0"])
    methods, _notifications, send = blf
    result = await methods.handle(subscribe(target="queue-login+support"), ("127.0.0.3", 5060), "UDP", send)
    assert result.status == 404
    assert not methods.subscriptions
