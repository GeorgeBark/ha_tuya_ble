"""The Tuya BLE integration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
import time
from typing import Callable

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
from . import util
from .lock_activity import get_lock_activity, scoped_activity_context
from .util import is_lock_event_recent
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)


def is_recent_unlock_report(device: TuyaBLEDevice, datapoint: TuyaBLEDataPoint) -> bool:
    """Filter replayed timestamped unlock reports through a mapping callback."""
    if device.product_id not in util.TIMESTAMPED_UNLOCK_REPORTS:
        return True
    auto_lock_time = device.datapoints[36]
    delay = int(auto_lock_time.value) if auto_lock_time else 5
    return is_lock_event_recent(datapoint.timestamp, delay, time.time())


@dataclass
class TuyaBLEEventMapping:
    """Model a DP, description and default values"""

    dp_id: int
    description: EventEntityDescription
    force_add: bool = True
    event_types: list[str] = field(default_factory=list)
    event_types_by_dp: dict[int, str] = field(default_factory=dict)
    report_filter: Callable[[TuyaBLEDevice, TuyaBLEDataPoint], bool] | None = None


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
        }
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLEEventMapping]:
    mappings = []
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            mappings = product_mapping
        elif category.mapping is not None:
            mappings = category.mapping

    if (
        device.category in util.LOCK_CATEGORIES
        and device.product_id in util.UNLOCK_ATTRIBUTION
    ):
        mappings = [
            *mappings,
            TuyaBLEEventMapping(
                dp_id=12,
                description=EventEntityDescription(key="unlocked"),
                event_types=[method for method, _ in util.UNLOCK_METHODS.values()],
                event_types_by_dp={
                    dp_id: method for dp_id, (method, _) in util.UNLOCK_METHODS.items()
                },
                report_filter=is_recent_unlock_report,
            ),
        ]
    return mappings


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
            if self._mapping.report_filter and not self._mapping.report_filter(
                self._device, dp
            ):
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


class TuyaBLEUnlockEvent(TuyaBLEEvent):
    """Opt-in unlock events with HA acknowledgement filtering and availability."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEEventMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping)
        self._last_available = self.available
        self._unlock_report_filter = mapping.report_filter
        self._mapping = replace(mapping, report_filter=self._filter_unlock_report)

    async def async_added_to_hass(self) -> None:
        """Publish attributed events after the lock and Last unlock sensor."""
        await super().async_added_to_hass()
        if resolver := get_lock_activity(self._coordinator):
            self.async_on_remove(
                resolver.register_writer("unlocked", self._handle_coordinator_update)
            )

    def _filter_unlock_report(
        self, device: TuyaBLEDevice, datapoint: TuyaBLEDataPoint
    ) -> bool:
        """Do not expose an HA command acknowledgement as a second unlock."""
        if datapoint.id == 19 and (
            time.monotonic() < getattr(device, "_lock_activity_pending_until", 0.0)
            or (
                self._coordinator.last_updates is not None
                and self._coordinator.last_updates
                is getattr(device, "_lock_activity_ha_command_updates", None)
            )
        ):
            return False
        return self._unlock_report_filter is None or self._unlock_report_filter(
            device, datapoint
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh availability without inventing or replacing a restored event."""
        resolver = get_lock_activity(self._coordinator)
        if resolver and resolver.publish_ordered_update("unlocked"):
            return
        available = self.available
        if available != self._last_available:
            self._last_available = available
            self.async_write_ha_state()
        if resolver and resolver.has_mapping:
            dp = resolver.latest_report
            if dp is None or not self._filter_unlock_report(self._device, dp):
                return
            method = util.UNLOCK_METHODS[dp.id][0]
            activity = resolver.activities.get(dp.id)
            self._trigger_event(
                method,
                {
                    "method": method,
                    "user_id": dp.value,
                    **(activity.identity_data if activity else {}),
                },
            )
            with scoped_activity_context(self, activity.context if activity else None):
                self.async_write_ha_state()
            return
        super()._handle_coordinator_update()


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
            event_class = (
                TuyaBLEUnlockEvent
                if data.device.category in util.LOCK_CATEGORIES
                and data.device.product_id in util.UNLOCK_ATTRIBUTION
                and mapping.description.key == "unlocked"
                else TuyaBLEEvent
            )
            entities.append(
                event_class(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
