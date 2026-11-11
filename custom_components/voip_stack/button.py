"""Administrative physical-phone actions through existing authorized services."""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .physical_phone_entity import PhysicalPhoneEntity, async_setup_physical_phone_entities


class PhysicalPhoneProvisionButton(PhysicalPhoneEntity, ButtonEntity):
    """Ask the current provisioning owner to prepare this inventory revision."""

    _attr_translation_key = "physical_phone_provision"
    _attr_icon = "mdi:cloud-download"
    _attr_entity_category = EntityCategory.CONFIG
    service = "provision_phone"

    def __init__(self, hass, entry, phone_id):
        super().__init__(hass, entry, phone_id, self.service)

    async def async_press(self) -> None:
        await self.hass.services.async_call(DOMAIN, self.service, {"phone_id": self.phone_id},
            blocking=True, context=self._context)


class PhysicalPhoneRotateButton(PhysicalPhoneProvisionButton):
    """Rotate this phone's delivery credential without exposing it in state."""

    _attr_translation_key = "physical_phone_rotate"
    _attr_icon = "mdi:key-change"
    service = "rotate_phone_provisioning_token"


async def async_setup_entry(hass, entry, async_add_entities):
    await async_setup_physical_phone_entities(hass, entry, async_add_entities,
        lambda _hass, _entry, identity: [PhysicalPhoneProvisionButton(_hass, _entry, identity),
                                       PhysicalPhoneRotateButton(_hass, _entry, identity)])
