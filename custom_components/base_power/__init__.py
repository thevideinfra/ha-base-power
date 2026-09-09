"""The Base Power integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .api import BasePowerApiClient
from .auth import BasePowerAuth
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_CLIENT_TOKEN,
    CONF_SESSION_ID,
    CONF_SESSION_JWT,
    CONF_SERVICE_LOCATION_ID,
)
from .coordinator import BasePowerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Base Power from a config entry."""
    session = async_get_clientsession(hass)

    # Clerk auth needs its own cookie jar -- HA's shared session interferes.
    # async_create_clientsession ties the session to HA's shutdown, so it is
    # cleaned up even if we never reach async_unload_entry.
    clerk_session = async_create_clientsession(hass)

    auth = BasePowerAuth(
        session=clerk_session,
        client_token=entry.data[CONF_CLIENT_TOKEN],
        session_id=entry.data[CONF_SESSION_ID],
        session_jwt=entry.data.get(CONF_SESSION_JWT),
    )

    api_client = BasePowerApiClient(session=session)

    coordinator = BasePowerCoordinator(
        hass=hass,
        api_client=api_client,
        auth=auth,
        service_location_id=entry.data[CONF_SERVICE_LOCATION_ID],
        config_entry=entry,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Setup will be retried (or a reauth flow started); close the session we
        # opened so repeated retries don't pile up unclosed sessions.
        await clerk_session.close()
        raise

    # Keep the session reachable for unload without mixing non-coordinator
    # values into hass.data[DOMAIN], which every platform reads as coordinators.
    coordinator.clerk_session = clerk_session

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BasePowerCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coordinator is not None and coordinator.clerk_session is not None:
            await coordinator.clerk_session.close()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok
