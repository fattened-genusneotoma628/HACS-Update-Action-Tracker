"""The Update Action Tracker integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CARD_PATH,
    CARD_URL,
    CARD_URL_VERSIONED,
    DOMAIN,
    PLATFORMS,
    SERVICE_UPDATE_AND_ACTION,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Retry configuration for Lovelace resource registration
LOVELACE_REGISTER_MAX_RETRIES = 3
LOVELACE_REGISTER_RETRY_DELAY = 2  # seconds

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("version"): cv.string,
    }
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration level items (runs once regardless of config entries)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Update Action Tracker from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    # Forward setup to the todo platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the Lovelace card
    await _async_register_static_path(hass)

    # Defer Lovelace resource registration until HA is fully started
    async def _on_homeassistant_started(_event: Any) -> None:
        await _async_register_lovelace_resource(hass)

    hass.bus.async_listen_once("homeassistant_started", _on_homeassistant_started)

    # Register the update_and_action service
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_AND_ACTION):

        async def handle_update_and_action(call: ServiceCall) -> None:
            """Handle the update_and_action service call."""
            entity_id: str = call.data["entity_id"]
            version: str | None = call.data.get("version")

            # Get the update entity state for metadata
            state = hass.states.get(entity_id)
            if state is None:
                raise ValueError(f"Entity {entity_id} not found")

            friendly_name = state.attributes.get("friendly_name", entity_id)
            latest_version = version or state.attributes.get(
                "latest_version", "unknown"
            )
            release_url = state.attributes.get("release_url", "")

            # Fetch release notes from the update entity object
            release_notes = await _async_get_release_notes(hass, entity_id)

            # Perform the update via the update.install service
            service_data: dict[str, Any] = {"entity_id": entity_id}
            if version:
                service_data["version"] = version
            await hass.services.async_call(
                "update",
                "install",
                service_data,
                blocking=True,
            )

            # Add a to-do item with the release notes
            todo_entity = hass.data[DOMAIN].get("todo_entity")
            if todo_entity is None:
                _LOGGER.error(
                    "Todo entity not available - cannot create action item"
                )
                return

            # Build description with release notes, date, and release link
            today = date.today().isoformat()
            description_parts = []
            description_parts.append(f"Updated: {today}")
            if release_url:
                description_parts.append(f"Release: {release_url}")
            description_parts.append(f"Entity: {entity_id}")
            if release_notes:
                description_parts.append(f"\n---\n{release_notes}")
            description = "\n".join(description_parts)

            item = TodoItem(
                summary=f"{friendly_name} updated to {latest_version} ({today})",
                status=TodoItemStatus.NEEDS_ACTION,
                description=description,
            )
            await todo_entity.async_create_todo_item(item)

            _LOGGER.info(
                "Updated %s to %s and added action item to todo list",
                friendly_name,
                latest_version,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_AND_ACTION,
            handle_update_and_action,
            schema=SERVICE_SCHEMA,
        )

    return True


async def _async_get_release_notes(
    hass: HomeAssistant, entity_id: str
) -> str | None:
    """Fetch release notes from an update entity."""
    entity_comp = hass.data.get("entity_components", {}).get("update")
    if entity_comp is None:
        _LOGGER.debug("Update entity component not available")
        return None

    entity = entity_comp.get_entity(entity_id)
    if entity is None:
        _LOGGER.debug("Entity %s not found in update component", entity_id)
        return None

    if hasattr(entity, "async_release_notes"):
        try:
            return await entity.async_release_notes()
        except Exception:
            _LOGGER.debug(
                "Failed to fetch release notes for %s", entity_id, exc_info=True
            )
    return None


async def _async_register_static_path(hass: HomeAssistant) -> None:
    """Register the static path to serve the card JS file."""
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
        )
        _LOGGER.debug("Registered static path: %s -> %s", CARD_URL, CARD_PATH)
    except RuntimeError:
        # Path already registered (happens on integration reload)
        pass


async def _async_register_lovelace_resource(
    hass: HomeAssistant,
    retry_count: int = 0,
) -> None:
    """Register the card as a Lovelace resource with retry logic."""
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        if retry_count < LOVELACE_REGISTER_MAX_RETRIES:
            _LOGGER.debug(
                "Lovelace not initialized yet, retrying in %ds (%d/%d)",
                LOVELACE_REGISTER_RETRY_DELAY,
                retry_count + 1,
                LOVELACE_REGISTER_MAX_RETRIES,
            )
            await asyncio.sleep(LOVELACE_REGISTER_RETRY_DELAY)
            return await _async_register_lovelace_resource(hass, retry_count + 1)
        _LOGGER.warning(
            "Could not auto-register card: Lovelace not initialized after %d "
            "retries. Add resource manually: Settings > Dashboards > Resources "
            "> %s (module)",
            LOVELACE_REGISTER_MAX_RETRIES,
            CARD_URL,
        )
        return

    # Get resources collection
    resources = getattr(lovelace_data, "resources", None)
    if resources is None and hasattr(lovelace_data, "get"):
        resources = lovelace_data.get("resources")

    if resources is None:
        if retry_count < LOVELACE_REGISTER_MAX_RETRIES:
            _LOGGER.debug(
                "Lovelace resources not available yet, retrying in %ds (%d/%d)",
                LOVELACE_REGISTER_RETRY_DELAY,
                retry_count + 1,
                LOVELACE_REGISTER_MAX_RETRIES,
            )
            await asyncio.sleep(LOVELACE_REGISTER_RETRY_DELAY)
            return await _async_register_lovelace_resource(hass, retry_count + 1)
        _LOGGER.warning(
            "Could not auto-register card: Lovelace in YAML mode. "
            "Add to configuration.yaml: lovelace: resources: "
            "[{url: %s, type: module}]",
            CARD_URL,
        )
        return

    if not hasattr(resources, "async_create_item") or not hasattr(
        resources, "async_items"
    ):
        _LOGGER.warning(
            "Could not auto-register card: Lovelace resources API not "
            "available. Add resource manually: Settings > Dashboards > "
            "Resources > %s (module)",
            CARD_URL,
        )
        return

    # Namespace: our base URL without query params
    namespace = CARD_URL

    # Check for existing resource
    existing_resource = None
    for resource in resources.async_items():
        url = resource.get("url", "")
        if url.startswith(namespace):
            existing_resource = resource
            break

    if existing_resource:
        if existing_resource.get("url") != CARD_URL_VERSIONED:
            try:
                await resources.async_update_item(
                    existing_resource["id"],
                    {"url": CARD_URL_VERSIONED, "res_type": "module"},
                )
                _LOGGER.info(
                    "Updated Update Action Tracker card resource to v%s", VERSION
                )
            except Exception:
                _LOGGER.warning(
                    "Failed to update Lovelace resource", exc_info=True
                )
        else:
            _LOGGER.debug("Card already registered with current version")
        return

    # Create new resource
    try:
        await resources.async_create_item(
            {"url": CARD_URL_VERSIONED, "res_type": "module"}
        )
        _LOGGER.info(
            "Registered Update Action Tracker card as Lovelace resource (v%s)",
            VERSION,
        )
    except Exception:
        _LOGGER.warning("Failed to register Lovelace resource", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop("todo_entity", None)
        hass.data[DOMAIN].pop("entry_id", None)

        # Remove service if this is the last entry
        if len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
            if hass.services.has_service(DOMAIN, SERVICE_UPDATE_AND_ACTION):
                hass.services.async_remove(DOMAIN, SERVICE_UPDATE_AND_ACTION)

    return unload_ok
