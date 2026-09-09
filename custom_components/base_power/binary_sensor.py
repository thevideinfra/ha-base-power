"""Binary sensor entities for Base Power integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_SERVICE_LOCATION_ID, MANUFACTURER, MODEL
from .coordinator import BasePowerCoordinator


def _has_solar(data: dict[str, Any]) -> bool | None:
    """Return True if the system reports solar."""
    return data.get("dashboard", {}).get("has_solar", False)


def _battery_connected(data: dict[str, Any]) -> bool | None:
    """Return True if the battery appears to be online.

    Prefer the WiFi telemetry, which is what actually reflects connectivity:
    the coordinator sets wifi["connected"] from whether the battery returned a
    scan this cycle. Fall back to the old backup_seconds heuristic only when we
    have no WiFi telemetry at all, since MobileGetWifiMetrics returns nothing
    on some accounts and a hard switch would pin this sensor to "disconnected"
    forever there.

    (backup_seconds > 0 is a poor connectivity signal on its own: a fully
    discharged but perfectly online battery reads as disconnected.)
    """
    wifi = data.get("wifi", {})
    if wifi.get("ssid") is not None:
        return bool(wifi.get("connected", False))
    return data.get("dashboard", {}).get("backup_seconds", 0) > 0


def _grid_is_up(data: dict[str, Any]) -> bool | None:
    """Return True if grid power is available (no outage).

    Only reports a value when GridStatus actually returned data this cycle.
    This used to default to True whenever the RPC returned no telemetry, which
    is the wrong failure mode for outage detection: a real outage coinciding
    with a failed telemetry call would show a false "on". Unknown is honest.
    """
    grid = data.get("grid", {})
    if not grid.get("available", False):
        return None
    return grid.get("grid_is_up", True)


@dataclass(frozen=True, kw_only=True)
class BasePowerBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Base Power binary sensor.

    `key` is also the unique_id suffix, so it must not change for an existing
    sensor -- doing so orphans the entity and loses its history.
    """

    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSORS: tuple[BasePowerBinarySensorEntityDescription, ...] = (
    BasePowerBinarySensorEntityDescription(
        key="has_solar",
        name="Solar Connected",
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:solar-power",
        value_fn=_has_solar,
    ),
    BasePowerBinarySensorEntityDescription(
        key="battery_connected",
        name="Battery Connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:wifi",
        value_fn=_battery_connected,
    ),
    BasePowerBinarySensorEntityDescription(
        key="grid_power",
        # Named "Grid Status" to avoid colliding with the "Grid Power" amps
        # sensor; the key stays grid_power so existing entities are preserved.
        name="Grid Status",
        device_class=BinarySensorDeviceClass.POWER,
        icon="mdi:transmission-tower",
        value_fn=_grid_is_up,
    ),
    BasePowerBinarySensorEntityDescription(
        key="battery_charging",
        name="Battery Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        icon="mdi:battery-charging",
        value_fn=lambda data: data.get("derived", {}).get("battery_charging"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Base Power binary sensors from a config entry."""
    coordinator: BasePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BasePowerBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSORS
    )


class BasePowerBinarySensor(
    CoordinatorEntity[BasePowerCoordinator], BinarySensorEntity
):
    """A Base Power binary sensor, described by an entity description."""

    _attr_has_entity_name = True
    entity_description: BasePowerBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: BasePowerCoordinator,
        entry: ConfigEntry,
        description: BasePowerBinarySensorEntityDescription,
    ) -> None:
        """Initialize binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        service_location_id = entry.data[CONF_SERVICE_LOCATION_ID]
        self._attr_unique_id = f"{service_location_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, service_location_id)},
            "name": f"{MANUFACTURER} {service_location_id}",
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
