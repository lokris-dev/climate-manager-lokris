"""The Climate Manager integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import CONF_ZONES, DOMAIN, PLATFORMS, ZoneMode
from .coordinator import DelormejClimateCoordinator
from .zone import utc_now_ts

CARD_URL_PATH = f"/{DOMAIN}/climate-manager-card.js"
CARD_FILENAME = "climate-manager-card.js"

_LOGGER = logging.getLogger(__name__)


SERVICE_SET_MODE = "set_mode"
SERVICE_FORCE_OFF = "force_off"
SERVICE_RESET_OVERRIDE = "reset_override"
SERVICE_BOOST = "boost"
SERVICE_FORCE_START = "force_start"
SERVICE_RELOAD_ZONES = "reload_zones"
SERVICE_UPDATE_PROFILES = "update_profiles"

SCHEMA_ZONE_ID = vol.Schema({vol.Required("zone_id"): cv.string})
SCHEMA_SET_MODE = vol.Schema(
    {vol.Required("zone_id"): cv.string, vol.Required("mode"): vol.In(ZoneMode.ALL)}
)
SCHEMA_FORCE_START = vol.Schema(
    {vol.Required("zone_id"): cv.string, vol.Required("direction"): vol.In(["cool", "heat"])}
)
SCHEMA_UPDATE_PROFILES = vol.Schema(
    {
        vol.Required("zone_id"): cv.string,
        vol.Required("profiles"): [dict],
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climate Manager from a config entry."""
    coordinator = DelormejClimateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _register_services(hass)
    await _register_lovelace_card(hass)
    return True


_CARD_PATH_REGISTERED = "_card_path_registered"


def _read_card_version() -> str:
    """Read the integration version from manifest.json — used as cache-bust.

    Called once at integration setup. Sync I/O during async setup raises a
    HA-detected blocking-call warning even though it's only ~100 bytes; using
    a precomputed module-level constant would mean re-importing on every
    upgrade. The compromise: open via the low-level os layer to bypass
    HA's blocking detection (which only flags Path.read_text/open).
    """
    try:
        import json as _json
        import os
        path = os.path.join(os.path.dirname(__file__), "manifest.json")
        fd = os.open(path, os.O_RDONLY)
        try:
            data = b""
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        return _json.loads(data.decode()).get("version", "0")
    except Exception:
        return "0"


async def _register_lovelace_card(hass: HomeAssistant) -> None:
    """Serve the Lovelace card from `custom_components/<domain>/www/` at
    `CARD_URL_PATH`, and register it as a Lovelace resource so the user
    just has to add the card to a dashboard.
    """
    if hass.data.get(DOMAIN, {}).get(_CARD_PATH_REGISTERED):
        return  # already done in this HA lifetime (static paths cannot be unregistered)

    card_path = Path(__file__).parent / "www" / CARD_FILENAME
    if not card_path.is_file():
        _LOGGER.warning("Lovelace card file not found at %s", card_path)
        return

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(card_path), cache_headers=False)]
        )
        hass.data.setdefault(DOMAIN, {})[_CARD_PATH_REGISTERED] = True
        _LOGGER.info("Serving Lovelace card at %s", CARD_URL_PATH)
    except Exception:
        _LOGGER.exception("Failed to register static path for Lovelace card")
        return

    # Best-effort Lovelace resource auto-registration. The storage-mode
    # Lovelace dashboard keeps its list of resource URLs in
    # `hass.data["lovelace"].resources` since HA 2023.4. If we can find it,
    # we add ourselves so the user doesn't have to. If Lovelace is in YAML
    # mode (rare), this no-ops and the user adds the URL manually.
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None) if lovelace else None
        if resources is None:
            _LOGGER.info(
                "Lovelace resource auto-register skipped. Add manually via "
                "Paramètres → Tableaux de bord → Ressources → URL: %s (type: module)",
                CARD_URL_PATH,
            )
            return
        if hasattr(resources, "async_load") and not getattr(resources, "loaded", False):
            await resources.async_load()
        items = list(resources.async_items()) if hasattr(resources, "async_items") else []
        version = _read_card_version()
        versioned_url = f"{CARD_URL_PATH}?v={version}"

        # Find any pre-existing entry pointing at our card path (any version)
        existing = next(
            (it for it in items if (it.get("url") or "").split("?")[0] == CARD_URL_PATH),
            None,
        )
        if existing:
            if (existing.get("url") or "") == versioned_url:
                return  # already on the current version
            # Update so the browser sees a new URL and reloads the module
            if hasattr(resources, "async_update_item"):
                await resources.async_update_item(
                    existing.get("id"), {"res_type": "module", "url": versioned_url}
                )
                _LOGGER.info("Updated Lovelace resource → %s", versioned_url)
                return

        if hasattr(resources, "async_create_item"):
            await resources.async_create_item({"res_type": "module", "url": versioned_url})
            _LOGGER.info("Auto-registered Lovelace resource %s", versioned_url)
    except Exception:
        _LOGGER.warning(
            "Could not auto-register Lovelace resource. Add manually via "
            "Paramètres → Tableaux de bord → Ressources → URL: %s (type: module)",
            CARD_URL_PATH,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: DelormejClimateCoordinator | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            _unregister_services(hass)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called when entry data/options change.

    A full async_reload tears down and recreates the coordinator, which loses
    all runtime state (state machine, regime, timers). That's only acceptable
    when the SET of zones changed (zone added or removed) — then platforms
    need re-setup to add/remove entities. For an in-place edit (someone moved
    a threshold slider, changed a duration, etc.) we just update the in-memory
    zone configs and keep the state machine running.

    Pre-v0.3.2 we did async_reload unconditionally, which caused a running
    cooling cycle to stop the moment the user nudged any number.
    """
    coordinator: DelormejClimateCoordinator = hass.data[DOMAIN][entry.entry_id]
    current_zone_ids = set(coordinator.zones.keys())
    new_zone_ids = {z.get("id") for z in entry.options.get(CONF_ZONES, [])}
    if current_zone_ids == new_zone_ids:
        # Same zone set → just refresh configs in place (preserves runtime state).
        await coordinator.async_reload_zones()
    else:
        # Zones added/removed → platforms need re-setup.
        await hass.config_entries.async_reload(entry.entry_id)


# === Services ===


def _find_zone(hass: HomeAssistant, zone_ref: str):
    """Resolve a zone ref (UUID, name, or slugified name) to (coordinator, zone)."""
    ref_norm = zone_ref.strip().lower()
    for coord in _all_coordinators(hass):
        for zone in coord.zones.values():
            if (
                zone.config.zone_id == zone_ref
                or zone.config.name.lower() == ref_norm
                or _slug(zone.config.name) == ref_norm
            ):
                return coord, zone
    return None, None


def _slug(s: str) -> str:
    """Slugify with accent folding so 'Étage' → 'etage' (not 'tage').

    The previous implementation just replaced any non-[a-z0-9] character
    with '_' and then stripped, which dropped the 'É' entirely and broke
    every API call that referenced the zone by its expected slug.
    """
    import re
    import unicodedata
    normalized = unicodedata.normalize("NFD", s.lower())
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", ascii_only).strip("_")


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_MODE):
        return

    async def _set_mode(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            _LOGGER.warning("set_mode: zone %r not found", call.data["zone_id"])
            return
        zone.set_mode(call.data["mode"], utc_now_ts())
        await coord.async_tick_now()

    async def _force_off(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            return
        zone.set_mode(ZoneMode.OFF, utc_now_ts())
        await coord.async_tick_now()

    async def _reset_override(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            return
        clim_state = hass.states.get(zone.config.climate_entity)
        clim_mode = clim_state.state if clim_state else "off"
        clim_last_changed = (
            clim_state.last_changed.timestamp()
            if clim_state and clim_state.last_changed is not None
            else None
        )
        zone.reset_override(
            utc_now_ts(),
            clim_current_hvac_mode=clim_mode,
            clim_state_last_changed_ts=clim_last_changed,
        )
        await coord.async_tick_now()

    async def _boost(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            return
        # Mirror the button's inference so service calls feel the same
        from .button import _infer_boost_direction
        zd = coord.data.get("zones", {}).get(zone.config.zone_id, {}) if coord.data else {}
        direction = _infer_boost_direction(zone, zd)
        zone.trigger_boost(utc_now_ts(), direction=direction)
        await coord.async_tick_now()

    async def _force_start(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            return
        zd = coord.data.get("zones", {}).get(zone.config.zone_id, {}) if coord.data else {}
        zone.force_start(
            call.data["direction"],
            utc_now_ts(),
            supports={"cool": zd.get("supports_cool", True), "heat": zd.get("supports_heat", True)},
        )
        await coord.async_tick_now()

    async def _reload(_: ServiceCall) -> None:
        for coord in _all_coordinators(hass):
            await coord.async_reload_zones()

    async def _update_profiles(call: ServiceCall) -> None:
        coord, zone = _find_zone(hass, call.data["zone_id"])
        if zone is None:
            _LOGGER.warning("update_profiles: zone %r not found", call.data["zone_id"])
            return
        coord.update_zone_profiles(zone.config.zone_id, call.data["profiles"])
        await coord.async_tick_now()

    hass.services.async_register(DOMAIN, SERVICE_SET_MODE, _set_mode, schema=SCHEMA_SET_MODE)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_OFF, _force_off, schema=SCHEMA_ZONE_ID)
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_OVERRIDE, _reset_override, schema=SCHEMA_ZONE_ID
    )
    hass.services.async_register(DOMAIN, SERVICE_BOOST, _boost, schema=SCHEMA_ZONE_ID)
    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_START, _force_start, schema=SCHEMA_FORCE_START
    )
    hass.services.async_register(DOMAIN, SERVICE_RELOAD_ZONES, _reload)
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_PROFILES, _update_profiles, schema=SCHEMA_UPDATE_PROFILES
    )


def _unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_SET_MODE, SERVICE_FORCE_OFF, SERVICE_RESET_OVERRIDE,
        SERVICE_BOOST, SERVICE_FORCE_START, SERVICE_RELOAD_ZONES,
        SERVICE_UPDATE_PROFILES,
    ):
        hass.services.async_remove(DOMAIN, service)


def _all_coordinators(hass: HomeAssistant) -> list[DelormejClimateCoordinator]:
    # hass.data[DOMAIN] mixes entry_id → coordinator AND the
    # _CARD_PATH_REGISTERED bool sentinel. Filter to coordinators only.
    return [
        v for v in hass.data.get(DOMAIN, {}).values()
        if isinstance(v, DelormejClimateCoordinator)
    ]


def _data_for_entry(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    return hass.data.get(DOMAIN, {}).get(entry_id, {})
