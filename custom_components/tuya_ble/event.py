"""The Tuya BLE integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .dappsdq4 import is_dappsdq4_event_recent
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLEEventMapping:
    """Model a DP, description and default values"""

    dp_id: int
    description: EventEntityDescription
    force_add: bool = True
    event_types: list[str] = field(default_factory=list)
    event_types_by_dp: dict[int, str] = field(default_factory=dict)


@dataclass
class TuyaBLECategoryEventMapping:
    """Models a dict of products and their mappings"""

    products: dict[str, list[TuyaBLEEventMapping]] | None = None
    mapping: list[TuyaBLEEventMapping] | None = None


mapping: dict[str, TuyaBLECategoryEventMapping] = {
    "wxkg": TuyaBLECategoryEventMapping(
        products={
            **dict.fromkeys(
                ["kpzc6pm8", "ja5osu5g"],
                [
                    TuyaBLEEventMapping(
                        dp_id=1,
                        description=EventEntityDescription(
                            key="event",
                            device_class=EventDeviceClass.BUTTON,
                        ),
                        event_types=["single_click", "double_click", "long_press"],
                    )
                ],
            )
        }
    ),
    "jtmspro": TuyaBLECategoryEventMapping(
        products={
            **dict.fromkeys(
                ["stugc8dl", "xicdxood", "yfqp0shy", "djrqe0q6"],
                [
                    TuyaBLEEventMapping(
                        dp_id=24,
                        description=EventEntityDescription(
                            key="doorbell",
                            device_class=EventDeviceClass.DOORBELL,
                        ),
                        event_types=["ring"],
                    )
                ],
            ),
            "dappsdq4": [
                TuyaBLEEventMapping(
                    dp_id=12,
                    description=EventEntityDescription(key="unlocked"),
                    event_types=[
                        "fingerprint",
                        "password",
                        "dynamic_password",
                        "card",
                        "ble",
                        "temporary_password",
                        "phone_remote",
                    ],
                    event_types_by_dp={
                        12: "fingerprint",
                        13: "password",
                        14: "dynamic_password",
                        15: "card",
                        19: "ble",
                        55: "temporary_password",
                        62: "phone_remote",
                    },
                ),
            ],
        }
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLEEventMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
        if category.mapping is not None:
            return category.mapping

    return []


class TuyaBLEEvent(TuyaBLEEntity, EventEntity):
    """Representation of a Tuya BLE event entity."""

    platform = Platform.EVENT

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEEventMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping
        self._attr_event_types = mapping.event_types

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not getattr(self._coordinator, "last_updates", None):
            return

        # Check if our DP was in the updates list
        dp = next(
            (
                update
                for update in self._coordinator.last_updates
                if update.id == self._mapping.dp_id
                or update.id in self._mapping.event_types_by_dp
            ),
            None,
        )
        if dp is None:
            return

        if method := self._mapping.event_types_by_dp.get(dp.id):
            if self._device.product_id == "dappsdq4":
                auto_lock_time = self._device.datapoints[36]
                delay = int(auto_lock_time.value) if auto_lock_time else 5
                if not is_dappsdq4_event_recent(dp.timestamp, delay, time.time()):
                    return
            self._trigger_event(method, {"method": method, "user_id": dp.value})
            self.async_write_ha_state()
            return

        # Let's see what is the value of the DP
        val = dp.value

        if isinstance(val, bool):
            if val and len(self._attr_event_types) > 0:
                event_type = self._attr_event_types[0]
            else:
                event_type = None
        elif isinstance(val, int):
            if 0 <= val < len(self._attr_event_types):
                event_type = self._attr_event_types[val]
            else:
                event_type = None
        elif isinstance(val, str):
            if val in self._attr_event_types:
                event_type = val
            else:
                event_type = None
        else:
            event_type = None

        if event_type is not None:
            self._trigger_event(event_type)
            self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE event entities."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLEEvent] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(mapping.dp_id):
            entities.append(
                TuyaBLEEvent(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
