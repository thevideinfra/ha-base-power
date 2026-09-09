"""Sensor entities for Base Power integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
    PERCENTAGE,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_SERVICE_LOCATION_ID, MANUFACTURER, MODEL
from .coordinator import BasePowerCoordinator

BATTERY_STATUS_MAP = {
    0: "Unknown",
    1: "Not Installed",
    2: "Installed",
    3: "In Service",
    4: "Offline",
}


def _backup_hours(data: dict[str, Any]) -> float | None:
    """Prefer the derived estimate, fall back to the API's own figure."""
    estimated = data.get("derived", {}).get("estimated_backup_hours")
    if estimated is not None:
        return estimated
    return data.get("dashboard", {}).get("backup_hours")


def _battery_status(data: dict[str, Any]) -> str:
    """Map the numeric battery status to a label."""
    status = data.get("dashboard", {}).get("battery_status", 0)
    return BATTERY_STATUS_MAP.get(status, f"Unknown ({status})")


def _bill_amount(data: dict[str, Any]) -> float | None:
    """Convert the billed amount from cents to dollars."""
    cents = data.get("billing", {}).get("amount_cents")
    return round(cents / 100, 2) if cents is not None else None


def _from(section: str, key: str) -> Callable[[dict[str, Any]], Any]:
    """Build a value function reading one key out of one section."""
    return lambda data: data.get(section, {}).get(key)


@dataclass(frozen=True, kw_only=True)
class BasePowerSensorEntityDescription(SensorEntityDescription):
    """Describes a Base Power sensor.

    `key` is also the unique_id suffix, so it must not change for an existing
    sensor -- doing so orphans the entity and loses its history.
    """

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[BasePowerSensorEntityDescription, ...] = (
    BasePowerSensorEntityDescription(
        key="backup_hours",
        name="Battery Backup Time",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-clock",
        value_fn=_backup_hours,
    ),
    BasePowerSensorEntityDescription(
        key="battery_percent",
        name="Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_from("derived", "battery_percent"),
    ),
    BasePowerSensorEntityDescription(
        key="battery_count",
        name="Battery Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-multiple",
        value_fn=_from("derived", "battery_count"),
    ),
    BasePowerSensorEntityDescription(
        key="capacity",
        name="System Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-high",
        value_fn=_from("derived", "capacity_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="current_power",
        name="Current Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
        value_fn=_from("derived", "current_power_watts"),
    ),
    BasePowerSensorEntityDescription(
        key="current_energy",
        name="Current Interval Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
        value_fn=_from("derived", "current_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="daily_peak",
        name="Daily Peak",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-up-bold",
        value_fn=_from("derived", "daily_peak_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="daily_low",
        name="Daily Low",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-down-bold",
        value_fn=_from("derived", "daily_low_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="daily_total",
        name="Daily Total Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:sigma",
        value_fn=_from("derived", "daily_total_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="intervals",
        name="Intervals Today",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=_from("derived", "intervals_today"),
    ),
    BasePowerSensorEntityDescription(
        key="battery_status",
        name="Battery Status",
        icon="mdi:battery-heart-variant",
        value_fn=_battery_status,
    ),
    BasePowerSensorEntityDescription(
        key="wifi_signal",
        name="WiFi Signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_from("wifi", "signal"),
    ),
    BasePowerSensorEntityDescription(
        key="wifi_ssid",
        name="WiFi SSID",
        icon="mdi:wifi-settings",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_from("wifi", "ssid"),
    ),
    BasePowerSensorEntityDescription(
        key="grid_to_home",
        name="Grid to Home Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower",
        value_fn=_from("energy", "grid_to_home_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="solar_to_home",
        name="Solar to Home Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:solar-power-variant",
        value_fn=_from("energy", "solar_to_home_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="battery_to_home",
        name="Battery to Home Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=_from("energy", "battery_to_home_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="bill_amount",
        name="Bill Amount",
        native_unit_of_measurement="$",
        icon="mdi:currency-usd",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bill_amount,
    ),
    BasePowerSensorEntityDescription(
        key="bill_due_date",
        name="Bill Due Date",
        icon="mdi:calendar-clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_from("billing", "due_date"),
    ),
    BasePowerSensorEntityDescription(
        key="asset_id",
        name="Asset ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_from("cycles", "asset_id"),
    ),
    BasePowerSensorEntityDescription(
        key="grid_power_amps",
        name="Grid Power",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:current-ac",
        value_fn=_from("grid", "current_power_amps"),
    ),
    BasePowerSensorEntityDescription(
        key="daily_total_grid",
        name="Daily Total (Grid)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=_from("grid", "daily_total_kwh_grid"),
    ),
    BasePowerSensorEntityDescription(
        key="daily_avg_grid",
        name="Daily Average Grid Interval",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-bell-curve",
        value_fn=_from("grid", "daily_avg_kwh_grid"),
    ),
    BasePowerSensorEntityDescription(
        key="total_home_energy",
        name="Total Home Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:home-lightning-bolt",
        value_fn=_from("derived", "total_home_kwh"),
    ),
    BasePowerSensorEntityDescription(
        key="self_sufficiency",
        name="Self-Sufficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:leaf",
        value_fn=_from("derived", "self_sufficiency_percent"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Base Power sensors from a config entry."""
    coordinator: BasePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BasePowerSensor(coordinator, entry, description) for description in SENSORS
    )


class BasePowerSensor(CoordinatorEntity[BasePowerCoordinator], SensorEntity):
    """A Base Power sensor, described by a BasePowerSensorEntityDescription."""

    _attr_has_entity_name = True
    entity_description: BasePowerSensorEntityDescription

    def __init__(
        self,
        coordinator: BasePowerCoordinator,
        entry: ConfigEntry,
        description: BasePowerSensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
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
    def native_value(self) -> Any:
        """Return the sensor value."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
