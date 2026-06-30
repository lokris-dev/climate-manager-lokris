"""Config flow + OptionsFlow.

Design philosophy (v0.14): the Config Flow is empty — the integration creates
itself without asking anything. Everything that's configurable lives in the
Options Flow:

  Settings → Devices & Services → Climate Manager → Configure
    ├─ Add a zone
    ├─ Edit a zone
    ├─ Remove a zone
    └─ Global presence (optional)

Per-profile concerns (thresholds, schedule, presence gate, power, fan) are NOT
in this flow — they're managed inline in the Lovelace card §2 "Profils". A
zone's only durable static config here is hardware identity + timing.
"""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITIES,
    CONF_CLIMATE_ENTITY,
    CONF_CONTROL_ENABLED,
    CONF_DUREE_COOLDOWN_MIN,
    CONF_DUREE_STABILISATION_MIN,
    CONF_FROST_DURATION_MIN,
    CONF_FROST_MAX_TEMP,
    CONF_FROST_MIN_TEMP,
    CONF_FROST_PROTECTION_ENABLED,
    CONF_OVERRIDE_DUREE_MIN,
    CONF_PENDULUM_IDLE,
    CONF_PRESENCE_ABSENT_STATES,
    CONF_PRESENCE_ENTITY,
    CONF_TEMPERATURE_SENSORS,
    CONF_WINDOW_SENSORS,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DEFAULT_DUREE_COOLDOWN_MIN,
    DEFAULT_DUREE_STABILISATION_MIN,
    DEFAULT_FROST_DURATION_MIN,
    DEFAULT_FROST_MAX_TEMP,
    DEFAULT_FROST_MIN_TEMP,
    DEFAULT_FROST_PROTECTION_ENABLED,
    DEFAULT_OVERRIDE_DUREE_MIN,
    DEFAULT_PENDULUM_IDLE,
    DOMAIN,
    MAX_DUREE_MIN,
    MAX_OVERRIDE_DUREE_MIN,
    MIN_DUREE_MIN,
    MIN_OVERRIDE_DUREE_MIN,
)

# Domains a user might pick as a global presence proxy. Order matters: most
# common defaults first so the selector picker surfaces them.
PRESENCE_DOMAINS = ["person", "device_tracker", "binary_sensor", "input_boolean", "alarm_control_panel"]

# Sensible default "absent" states across the supported domains. The user can
# override and add custom values via the multi-select.
DEFAULT_ABSENT_STATES = ["armed_away", "not_home", "off"]
KNOWN_ABSENT_STATES = [
    # alarm_control_panel
    "armed_away",
    "armed_vacation",
    "armed_night",
    "armed_home",
    # person / device_tracker
    "not_home",
    "away",
    # binary_sensor occupancy/presence
    "off",
    # input_boolean used as "away" toggle
    "on",
]


def _zone_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema for adding/editing a zone.

    Intentionally minimal — only the things that don't fit per-profile:
    hardware identity, hard-gate windows, and timing knobs (which are zone-
    wide invariants, not usage-dependent).
    """
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ZONE_NAME, default=defaults.get(CONF_ZONE_NAME, vol.UNDEFINED)
            ): str,
            vol.Required(
                CONF_CLIMATE_ENTITIES,
                default=(
                    defaults.get(CONF_CLIMATE_ENTITIES)
                    or ([defaults[CONF_CLIMATE_ENTITY]] if defaults.get(CONF_CLIMATE_ENTITY) else vol.UNDEFINED)
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Required(
                CONF_TEMPERATURE_SENSORS,
                default=defaults.get(CONF_TEMPERATURE_SENSORS, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature", multiple=True
                )
            ),
            vol.Optional(
                CONF_WINDOW_SENSORS, default=defaults.get(CONF_WINDOW_SENSORS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="binary_sensor",
                    # `opening` covers Ajax/Zigbee window sensors that don't use
                    # the narrower "window" device_class as well as generic
                    # openings (doors with sash detect, etc.).
                    device_class=["window", "opening", "door"],
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_DUREE_STABILISATION_MIN,
                default=defaults.get(
                    CONF_DUREE_STABILISATION_MIN, DEFAULT_DUREE_STABILISATION_MIN
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_DUREE_MIN, max=MAX_DUREE_MIN, step=1,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_DUREE_COOLDOWN_MIN,
                default=defaults.get(CONF_DUREE_COOLDOWN_MIN, DEFAULT_DUREE_COOLDOWN_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_DUREE_MIN, max=MAX_DUREE_MIN, step=1,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_OVERRIDE_DUREE_MIN,
                default=defaults.get(CONF_OVERRIDE_DUREE_MIN, DEFAULT_OVERRIDE_DUREE_MIN),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_OVERRIDE_DUREE_MIN, max=MAX_OVERRIDE_DUREE_MIN, step=1,
                    unit_of_measurement="min",
                )
            ),
        }
    )


def _presence_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Global presence config — informational only.

    Drives the "Maison absente/présente" pill in the card. Per-profile gates
    are configured separately in the card §2.
    """
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_PRESENCE_ENTITY,
                default=defaults.get(CONF_PRESENCE_ENTITY, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=PRESENCE_DOMAINS)
            ),
            vol.Optional(
                CONF_PRESENCE_ABSENT_STATES,
                default=defaults.get(CONF_PRESENCE_ABSENT_STATES, DEFAULT_ABSENT_STATES),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=KNOWN_ABSENT_STATES, multiple=True, custom_value=True
                )
            ),
        }
    )


class DelormejClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-step empty Config Flow. Everything else is in Options."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        # Fork LOKRIS : on pré-remplit les 9 zones réelles + le gating alarme.
        # control_enabled=False → on démarre en OBSERVATION (rien n'est piloté)
        # pour voir le rendu d'abord ; on active le pilotage via l'interrupteur
        # maître quand on est prêt.
        # SEED_SYSTEM : pendulum_idle=True + valeurs frost de référence.
        from .seed import SEED_PRESENCE, SEED_SYSTEM, seed_zones

        return self.async_create_entry(
            title="Climate Manager — LOKRIS",
            data={**SEED_PRESENCE, **SEED_SYSTEM, CONF_CONTROL_ENABLED: False},
            options={CONF_ZONES: seed_zones()},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DelormejClimateOptionsFlow(config_entry)


class DelormejClimateOptionsFlow(OptionsFlow):
    """Manage zones (add/edit/remove) + optional global presence."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._editing_zone_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._entry.options.get(CONF_ZONES, [])
        # Only offer edit/remove if there are zones to act on.
        menu = ["add_zone"]
        if zones:
            menu += ["edit_zone", "remove_zone"]
        menu += ["presence", "system"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    # --- presence (now optional, post-install only) ---

    async def async_step_presence(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="presence",
                data_schema=_presence_schema(defaults=dict(self._entry.data)),
            )
        # Empty presence_entity (user cleared it) → drop the key so the
        # coordinator falls back to "presence unknown".
        clean = {k: v for k, v in user_input.items() if v not in (None, "", [])}
        self.hass.config_entries.async_update_entry(self._entry, data=clean)
        return self.async_create_entry(title="", data=self._entry.options)

    # --- paramètres système (pendule + hors-gel) ---

    async def async_step_system(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options système : pendulum_idle + protection hors-gel.

        Ces paramètres sont stockés dans ConfigEntry.data (niveau système,
        pas par zone) et ne sont pas exposés dans la carte Lovelace — ils
        sont réservés à l'administrateur.
        """
        if user_input is None:
            current = dict(self._entry.data)
            schema = vol.Schema({
                vol.Optional(
                    CONF_PENDULUM_IDLE,
                    default=current.get(CONF_PENDULUM_IDLE, DEFAULT_PENDULUM_IDLE),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_FROST_PROTECTION_ENABLED,
                    default=current.get(CONF_FROST_PROTECTION_ENABLED, DEFAULT_FROST_PROTECTION_ENABLED),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_FROST_MIN_TEMP,
                    default=current.get(CONF_FROST_MIN_TEMP, DEFAULT_FROST_MIN_TEMP),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=15.0, step=0.5, unit_of_measurement="°C")
                ),
                vol.Optional(
                    CONF_FROST_MAX_TEMP,
                    default=current.get(CONF_FROST_MAX_TEMP, DEFAULT_FROST_MAX_TEMP),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=25.0, max=40.0, step=0.5, unit_of_measurement="°C")
                ),
                vol.Optional(
                    CONF_FROST_DURATION_MIN,
                    default=current.get(CONF_FROST_DURATION_MIN, DEFAULT_FROST_DURATION_MIN),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=480, step=10, unit_of_measurement="min")
                ),
            })
            return self.async_show_form(step_id="system", data_schema=schema)

        # Fusionne dans entry.data (préserve les clés existantes comme présence)
        new_data = {**self._entry.data, **user_input}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        return self.async_create_entry(title="", data=self._entry.options)

    # --- add zone ---

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="add_zone", data_schema=_zone_schema())
        zone_id = uuid.uuid4().hex[:8]
        new_zone = {CONF_ZONE_ID: zone_id, **user_input}
        zones = list(self._entry.options.get(CONF_ZONES, []))
        zones.append(new_zone)
        new_options = {**self._entry.options, CONF_ZONES: zones}
        return self.async_create_entry(title="", data=new_options)

    # --- edit zone (two-step: pick, then form) ---

    async def async_step_edit_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._entry.options.get(CONF_ZONES, [])
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is None:
            return self.async_show_form(
                step_id="edit_zone",
                data_schema=vol.Schema(
                    {
                        vol.Required("zone_id"): vol.In(
                            {z[CONF_ZONE_ID]: z[CONF_ZONE_NAME] for z in zones}
                        )
                    }
                ),
            )
        self._editing_zone_id = user_input["zone_id"]
        return await self.async_step_edit_zone_form()

    async def async_step_edit_zone_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._entry.options.get(CONF_ZONES, [])
        zone = next((z for z in zones if z[CONF_ZONE_ID] == self._editing_zone_id), None)
        if zone is None:
            return self.async_abort(reason="unknown_zone")
        if user_input is None:
            return self.async_show_form(
                step_id="edit_zone_form", data_schema=_zone_schema(defaults=zone)
            )
        # Preserve any legacy keys present on the existing zone (seuils, power,
        # fan, schedule, profiles...) so editing the form doesn't accidentally
        # wipe the per-profile cascade or migration-synthesised defaults.
        new_zone = {**zone, **user_input, CONF_ZONE_ID: zone[CONF_ZONE_ID]}
        new_zones = [new_zone if z[CONF_ZONE_ID] == self._editing_zone_id else z for z in zones]
        new_options = {**self._entry.options, CONF_ZONES: new_zones}
        return self.async_create_entry(title="", data=new_options)

    # --- remove zone ---

    async def async_step_remove_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zones = self._entry.options.get(CONF_ZONES, [])
        if not zones:
            return self.async_abort(reason="no_zones")
        if user_input is None:
            return self.async_show_form(
                step_id="remove_zone",
                data_schema=vol.Schema(
                    {
                        vol.Required("zone_id"): vol.In(
                            {z[CONF_ZONE_ID]: z[CONF_ZONE_NAME] for z in zones}
                        )
                    }
                ),
            )
        zid = user_input["zone_id"]
        new_zones = [z for z in zones if z[CONF_ZONE_ID] != zid]
        new_options = {**self._entry.options, CONF_ZONES: new_zones}
        return self.async_create_entry(title="", data=new_options)
