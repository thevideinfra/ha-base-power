"""Data coordinator for Base Power integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any, TypeVar

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util

from .api import BasePowerApiClient
from .auth import BasePowerAuth, AuthenticationError
from .const import (
    SCAN_INTERVAL_SECONDS,
    BATTERY_CAPACITY_PER_UNIT_KWH,
    DEFAULT_BATTERY_COUNT,
    CONF_WIFI_SSID,
    CONF_BATTERY_COUNT,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class BasePowerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate data fetching from Base Power API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: BasePowerApiClient,
        auth: BasePowerAuth,
        service_location_id: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Base Power",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api_client
        self.auth = auth
        self.service_location_id = service_location_id
        self._config_entry = config_entry
        # Owned by __init__.py; closed on unload.
        self.clerk_session: aiohttp.ClientSession | None = None
        self._max_backup_seconds: int = 0  # Track max for % calibration
        self._prev_soc: float | None = None
        self._last_charging: bool | None = None
        self._prev_soc_ts: int | None = None
        self._last_wifi_ssid: str | None = None

        # FIX: last-known-good caches for the "optional" endpoints. Several of
        # these RPCs (GridStatus, UsageEnergy, UsageCycles, BillingMetadata)
        # have been observed returning a genuinely empty gRPC-Web frame on a
        # given poll even though they've returned real data on a previous poll
        # (or will again on a later one). Previously any empty poll reset the
        # sensor straight back to the all-None placeholder, which made sensors
        # like Total Home Energy / Self-Sufficiency / Daily Total (Grid) /
        # Bill Amount flap to "unknown" far more than the underlying data
        # actually warranted. We now only overwrite the cache when a fetch
        # actually returns something, and fall back to the last good value
        # otherwise -- the sensors then just hold their last real reading
        # until the next successful poll, same as any other polled sensor
        # that misses a beat.
        self._last_grid: dict[str, Any] = {
            "available": False, "grid_is_up": True, "battery_soc_percent": None,
            "battery_remaining_seconds": 0, "current_power_amps": None, "hourly_usage": [],
            "soc_timestamp": None,
        }
        self._last_wifi: dict[str, Any] = {"ssid": None, "signal": None, "connected": False}
        self._last_energy: dict[str, Any] = {
            "grid_to_home_kwh": None, "solar_to_home_kwh": None, "battery_to_home_kwh": None,
        }
        self._last_billing: dict[str, Any] = {"amount_cents": None, "due_date": None}
        self._last_cycles: dict[str, Any] = {"asset_id": None}

    async def _fetch_optional(self, name: str, coro: Awaitable[_T]) -> _T | None:
        """Run a non-essential fetch, returning None if it fails.

        Auth failures are deliberately re-raised: they mean the integration as
        a whole needs reauthenticating, not that this one endpoint is missing.
        Swallowing them here would leave the sensors serving cached values
        forever instead of prompting the user to sign in again.
        """
        try:
            return await coro
        except AuthenticationError:
            raise
        except Exception as err:
            _LOGGER.debug("%s fetch failed: %s", name, err)
            return None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Base Power API."""
        try:
            # Refresh JWT
            jwt = await self.auth.async_ensure_valid_token()
            self.api.set_jwt(jwt)

            # Fetch core data (required)
            dashboard = await self.api.get_dashboard_root(self.service_location_id)
            usage = await self.api.get_recent_usage(self.service_location_id)

            # Fetch optional data (non-fatal if they fail). Each block only
            # promotes the fetch into the "last good" cache when it actually
            # contains data, so a single empty/failed poll doesn't wipe out
            # sensors that were previously populated.
            fetched_grid = await self._fetch_optional(
                "GridStatus", self.api.get_grid_status(self.service_location_id)
            )
            if fetched_grid and fetched_grid.get("available"):
                self._last_grid = fetched_grid
            grid = self._last_grid

            fetched_wifi = await self._fetch_optional(
                "WifiMetrics", self.api.get_wifi_metrics(self.service_location_id)
            )
            if fetched_wifi is not None:
                scan: dict[str, int | None] = fetched_wifi.get("scan", {})
                preferred_ssid: str | None = self._config_entry.options.get(CONF_WIFI_SSID)
                if preferred_ssid and preferred_ssid in scan:
                    # User configured a specific SSID — report its signal from the scan
                    fetched_wifi["ssid"] = preferred_ssid
                    fetched_wifi["signal"] = scan[preferred_ssid]
                    fetched_wifi["connected"] = True
                    self._last_wifi_ssid = preferred_ssid
                    self._last_wifi = fetched_wifi
                elif scan:
                    # No preference set — use strongest visible network with sticky fallback
                    best_ssid = max(scan, key=lambda s: scan[s] or 0)
                    fetched_wifi["ssid"] = best_ssid
                    fetched_wifi["signal"] = scan[best_ssid]
                    fetched_wifi["connected"] = True
                    if not self._last_wifi_ssid:
                        self._last_wifi_ssid = best_ssid
                    self._last_wifi = fetched_wifi
                elif self._last_wifi_ssid:
                    fetched_wifi["ssid"] = self._last_wifi_ssid
                    self._last_wifi = fetched_wifi
            wifi = self._last_wifi

            fetched_energy = await self._fetch_optional(
                "UsageEnergy", self.api.get_usage_energy(self.service_location_id)
            )
            if fetched_energy and any(v is not None for v in fetched_energy.values()):
                self._last_energy = fetched_energy
            energy = self._last_energy

            fetched_billing = await self._fetch_optional(
                "BillingMetadata", self.api.get_billing_metadata(self.service_location_id)
            )
            if fetched_billing and any(v is not None for v in fetched_billing.values()):
                self._last_billing = fetched_billing
            billing = self._last_billing

            fetched_cycles = await self._fetch_optional(
                "UsageCycles", self.api.get_usage_cycles(self.service_location_id)
            )
            if fetched_cycles and any(v is not None for v in fetched_cycles.values()):
                self._last_cycles = fetched_cycles
            cycles = self._last_cycles

            # Apply user-configured battery count (default 1; overrides API detection)
            dashboard["battery_count"] = self._config_entry.options.get(
                CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
            )

            # Derive additional values
            derived = self._derive_values(dashboard, usage)

            # Battery percentage: prefer SoC from grid status, fall back to calibration
            derived["battery_percent_source"] = None
            if grid.get("battery_soc_percent") is not None:
                derived["battery_percent"] = grid["battery_soc_percent"]
                derived["battery_percent_source"] = "grid_status_telemetry"
            else:
                backup_seconds = dashboard.get("backup_seconds", 0)
                if backup_seconds > self._max_backup_seconds:
                    self._max_backup_seconds = backup_seconds
                if self._max_backup_seconds > 0 and backup_seconds > 0:
                    derived["battery_percent"] = round(
                        min((backup_seconds / self._max_backup_seconds) * 100, 100.0), 1
                    )
                    derived["battery_percent_source"] = "estimated_from_backup_time"
                    # NOTE: this estimate is calibrated against the highest
                    # backup_seconds value seen since this coordinator
                    # started (i.e. since the last HA/integration restart).
                    # That means the very first sample after every restart
                    # is mathematically guaranteed to read 100%, regardless
                    # of true state of charge, until a lower backup_seconds
                    # sample is observed to anchor a real range. Treat this
                    # value with skepticism for the first few polls after a
                    # restart -- battery_percent_source tells you which path
                    # produced the number.

            # Estimated backup hours: (SoC% × capacity_kWh) ÷ avg_home_power_kW
            battery_percent = derived.get("battery_percent")
            # Use the full 24h window, not today-so-far: just after midnight
            # a partial day is a poor estimate of average household load.
            intervals = derived.get("window_intervals", 0)
            window_total = derived.get("window_total_kwh", 0.0)
            capacity_kwh = derived.get("capacity_kwh", 0.0)
            if (
                battery_percent is not None
                and battery_percent > 0
                and intervals > 0
                and window_total > 0
                and capacity_kwh > 0
            ):
                avg_power_kw = window_total / (intervals / 4)
                battery_energy_kwh = battery_percent / 100 * capacity_kwh
                derived["estimated_backup_hours"] = round(battery_energy_kwh / avg_power_kw, 1)
            else:
                derived["estimated_backup_hours"] = None

            # Battery charging/discharging: infer from SoC delta between polls.
            # At/near 100% SoC the delta can never go positive again (nothing
            # left to charge into), so a stable reading there means "full,
            # not charging" rather than a genuine unknown -- report False
            # instead of leaving it ambiguous, since that's the state most
            # owners will see most of the time.
            FULL_SOC_THRESHOLD = 99.5
            SOC_DELTA_THRESHOLD = 0.5
            SOC_FLAT_EPSILON = 0.05
            charging: bool | None
            soc_ts = grid.get("soc_timestamp")
            if battery_percent is None:
                charging = None
            elif soc_ts is not None and soc_ts == self._prev_soc_ts:
                # Same 15-minute telemetry sample we already judged. We poll
                # every 5 minutes, so most polls see no new data -- that is not
                # information, so hold the previous verdict rather than
                # flapping the sensor to "unknown".
                charging = self._last_charging
            elif self._prev_soc is None:
                self._prev_soc = battery_percent
                charging = (
                    False if battery_percent >= FULL_SOC_THRESHOLD else None
                )
            else:
                delta = battery_percent - self._prev_soc
                if delta > SOC_DELTA_THRESHOLD:
                    charging = True
                    self._prev_soc = battery_percent
                elif delta < -SOC_DELTA_THRESHOLD:
                    charging = False
                    self._prev_soc = battery_percent
                elif battery_percent >= FULL_SOC_THRESHOLD:
                    charging = False
                elif abs(delta) < SOC_FLAT_EPSILON:
                    # New sample, SoC genuinely unchanged: the battery is
                    # sitting idle, so "not charging" is the honest answer.
                    charging = False
                else:
                    # Moved, but by less than the threshold -- too small to
                    # call a direction from one sample. Hold the last verdict
                    # rather than oscillating, and deliberately do NOT
                    # re-anchor _prev_soc: keeping the old anchor lets a slow
                    # charge accumulate past the threshold and be detected
                    # instead of never registering at all.
                    charging = self._last_charging
            derived["battery_charging"] = charging
            self._last_charging = charging
            if soc_ts is not None:
                self._prev_soc_ts = soc_ts

            # Self-sufficiency and total home consumption from energy data
            grid_kwh = energy.get("grid_to_home_kwh") or 0.0
            solar_kwh = energy.get("solar_to_home_kwh") or 0.0
            battery_kwh = energy.get("battery_to_home_kwh") or 0.0
            total_home = grid_kwh + solar_kwh + battery_kwh
            derived["total_home_kwh"] = round(total_home, 3) if total_home > 0 else None
            if total_home > 0:
                derived["self_sufficiency_percent"] = round(
                    (solar_kwh + battery_kwh) / total_home * 100, 1
                )
            else:
                derived["self_sufficiency_percent"] = None

            # has_solar: trust API field or fall back to solar production > 0
            if not dashboard.get("has_solar") and solar_kwh > 0:
                dashboard["has_solar"] = True

            return {
                "dashboard": dashboard,
                "usage": usage,
                "grid": grid,
                "wifi": wifi,
                "energy": energy,
                "billing": billing,
                "cycles": cycles,
                "derived": derived,
            }
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Base Power API: {err}") from err

    @staticmethod
    def _derive_values(
        dashboard: dict[str, Any], usage: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Derive additional sensor values from raw data."""
        battery_count = dashboard.get("battery_count", DEFAULT_BATTERY_COUNT)
        capacity_kwh = battery_count * BATTERY_CAPACITY_PER_UNIT_KWH

        # MobileGetRecentUsage returns a ROLLING 24-hour window (96 x 15-min
        # intervals), not "today". Summing the whole window gives a value that
        # slides rather than accumulates: as each poll drops the oldest
        # interval and adds a new one, the sum dips whenever the departing
        # interval was larger. Reported through a TOTAL_INCREASING sensor those
        # dips are recorded as negative deltas (HA only treats a drop as a
        # meter reset below 90% of the previous value), which drove the Energy
        # Dashboard's grid total negative. So the "daily" figures below are
        # computed over today's intervals only, which really does accumulate
        # and really does reset at midnight.
        day_start = dt_util.start_of_local_day()
        day_start_ts = day_start.timestamp()

        derived: dict[str, Any] = {
            "battery_percent": None,
            "battery_count": battery_count,
            "capacity_kwh": capacity_kwh,
            "current_power_watts": 0,
            "current_kwh": 0.0,
            "daily_peak_kwh": 0.0,
            "daily_low_kwh": 0.0,
            "daily_total_kwh": 0.0,
            "intervals_today": 0,
            # Exposed so the energy sensor can report an explicit last_reset
            # instead of making HA infer resets from value drops.
            "day_start": day_start,
            # Whole-window figures: a 24h average is steadier than a partial
            # day's, so the backup estimate keeps using these.
            "window_total_kwh": 0.0,
            "window_intervals": 0,
        }

        # Usage-derived values
        if usage:
            values = [p["kwh"] for p in usage]
            # Pick the newest point by timestamp rather than trusting the
            # response to be in ascending time order.
            latest = max(usage, key=lambda p: p.get("timestamp", 0))
            derived["current_kwh"] = latest["kwh"]
            derived["current_power_watts"] = round(latest["kwh"] * 4000)
            derived["window_total_kwh"] = round(sum(values), 2)
            derived["window_intervals"] = len(values)

            today = [
                p["kwh"] for p in usage if p.get("timestamp", 0) >= day_start_ts
            ]
            if today:
                derived["daily_peak_kwh"] = max(today)
                derived["daily_low_kwh"] = min(today)
                derived["daily_total_kwh"] = round(sum(today), 2)
                derived["intervals_today"] = len(today)

        return derived
