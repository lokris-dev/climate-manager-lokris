"""DataUpdateCoordinator: orchestrates zones, reads HA state, applies commands."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_SWING_MODE,
    ATTR_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_CONTROL_ENABLED,
    CONF_FROST_DURATION_MIN,
    CONF_FROST_MAX_TEMP,
    CONF_FROST_MIN_TEMP,
    CONF_FROST_PROTECTION_ENABLED,
    CONF_PENDULUM_IDLE,
    CONF_PRESENCE_ABSENT_STATES,
    CONF_PRESENCE_ENTITY,
    CONF_ZONES,
    DEFAULT_FROST_DURATION_MIN,
    DEFAULT_FROST_MAX_TEMP,
    DEFAULT_FROST_MIN_TEMP,
    DEFAULT_FROST_PROTECTION_ENABLED,
    DEFAULT_PENDULUM_IDLE,
    DEFAULT_POWER,
    DEFAULT_SETPOINT_STEP,
    DOMAIN,
    OVERRIDE_DEBOUNCE_SECONDS,
    SETPOINT_NOOP_DELTA,
    UPDATE_INTERVAL_SECONDS,
    ZoneMode,
    ZoneState,
)
from .context_tracker import ContextTracker
from .zone import (
    Command,
    Profile,
    Zone,
    ZoneConfig,
    ZoneInputs,
    ZoneRuntimeState,
    _apply_target_temp,
    utc_now_ts,
)

_LOGGER = logging.getLogger(__name__)


class DelormejClimateCoordinator(DataUpdateCoordinator):
    """Owns Zone state machines, ticks them, applies commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._context_tracker = ContextTracker()
        self._zones: dict[str, Zone] = {}
        self._unsub_state_listener = None
        # Listener dédié sur l'entité présence/alarme : sert à détecter le front
        # de désarmement (reset quotidien des zones).
        self._unsub_alarm_listener = None
        # Per-entity debounced override decisions. Each entry holds the original
        # old_state (from the first event in a flap burst), the latest new_state,
        # and the asyncio TimerHandle that will fire _resolve_pending_override.
        self._pending_overrides: dict[str, dict[str, Any]] = {}
        # Runtime persistence — one Store per entry, keyed by zone_id.
        # The completed cycle history used to be the only persisted payload; now
        # we also persist in-progress state (RUNNING/STABILIZING/COOLDOWN and
        # timestamps) so HA restarts are idempotent.
        self._runtime_store: Store = Store(
            hass, 2, f"{DOMAIN}_runtime_{entry.entry_id}"
        )
        # Backward-compat reader for pre-v0.17.3 history-only payloads.
        self._legacy_cycle_store: Store = Store(
            hass, 1, f"{DOMAIN}_cycles_{entry.entry_id}"
        )
        self._last_runtime_payload: dict[str, Any] | None = None
        # État du cycle hors-gel (§2). Persisté dans le runtime store.
        self._frost_start_ts: float | None = None
        self._frost_direction: str | None = None  # 'heat' | 'cool'
        self._rebuild_zones()

    # === Public API for platforms ===

    @property
    def zones(self) -> dict[str, Zone]:
        return self._zones

    def zone(self, zone_id: str) -> Zone | None:
        return self._zones.get(zone_id)

    def control_enabled(self) -> bool:
        """Pilotage actif ? Quand False (mode observation), on ne tick pas et on
        n'envoie aucune commande aux clims — on ne fait que lire pour la carte."""
        return bool(self.entry.data.get(CONF_CONTROL_ENABLED, True))

    async def async_set_control_enabled(self, enabled: bool) -> None:
        """Active/désactive le pilotage global (interrupteur maître).

        Met à jour ConfigEntry.data ; le listener d'update relance les zones
        (en préservant l'état runtime) puis un refresh. Au passage à True, les
        zones restent dans leur mode courant (souvent OFF après un seed
        d'observation) → rien ne démarre tant qu'on ne met pas une zone en
        Marche (ou qu'on ne déclenche pas reset_daily)."""
        if self.control_enabled() == enabled:
            return
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_CONTROL_ENABLED: enabled}
        )
        await self.async_request_refresh()

    # Champs "pilotage" portés à la fois par la config et par les profils. Quand
    # l'un d'eux change (intensité collègue, seuils admin), on le propage aux
    # profils en mémoire pour un effet immédiat (modèle boulot = 1 profil/zone).
    _DRIVER_FIELDS = frozenset(
        {
            "power",
            "fan_intensity",
            "seuil_debut_chauffage",
            "seuil_fin_chauffage",
            "seuil_debut_refroidissement",
            "seuil_fin_refroidissement",
        }
    )

    def update_zone_config(self, zone_id: str, **kwargs: Any) -> None:
        """Update a zone's static config (e.g. thresholds from number entities)."""
        zone = self._zones.get(zone_id)
        if not zone:
            return
        for k, v in kwargs.items():
            if hasattr(zone.config, k):
                setattr(zone.config, k, v)
        # Propage les champs de pilotage aux profils en mémoire (effet immédiat).
        for p in zone.config.profiles:
            for k in self._DRIVER_FIELDS & set(kwargs):
                setattr(p, k, getattr(zone.config, k))
        self._persist_zone_config(zone_id, **kwargs)
        self.async_set_updated_data(self._build_coordinator_data())

    def reset_all_zones_to_default(self) -> None:
        """Reset quotidien (déclenché au désarmement) : chaque zone repasse ON +
        profil par défaut (Normal) et tout override de la veille est purgé."""
        now = utc_now_ts()
        for zone in self._zones.values():
            zone.daily_reset(now, default_power=DEFAULT_POWER)
        self._persist_all_zones_power(DEFAULT_POWER)
        self.async_set_updated_data(self._build_coordinator_data())
        self.hass.async_create_task(self.async_request_refresh())

    def _persist_all_zones_power(self, power: str) -> None:
        """Écrit le power par défaut sur toutes les zones en un seul update."""
        zones = list(self.entry.options.get(CONF_ZONES, []))
        changed = False
        for i, z in enumerate(zones):
            if z.get("power") != power:
                zones[i] = {**z, "power": power}
                changed = True
        if changed:
            new_opts = {**self.entry.options, CONF_ZONES: zones}
            self.hass.config_entries.async_update_entry(self.entry, options=new_opts)

    def update_zone_profiles(self, zone_id: str, profiles: list[dict[str, Any]]) -> None:
        """Replace the cascade of profiles for a zone (called by the card via service).

        Persists the full list to ConfigEntry.options and hot-reloads the
        zone's in-memory config — the active cycle is preserved (we do not
        rebuild the Zone, only swap the profile list).
        """
        zone = self._zones.get(zone_id)
        if not zone:
            return
        parsed = [Profile.from_dict(p) for p in profiles]
        zone.config.profiles = parsed
        self._persist_zone_config(zone_id, profiles=[p.to_dict() for p in parsed])
        self.async_set_updated_data(self._build_coordinator_data())

    def update_split_config(
        self,
        zone_id: str,
        climate_entity: str,
        *,
        target: float | None = ...,  # type: ignore[assignment]
        power: str | None = ...,      # type: ignore[assignment]
        swing: str | None = ...,      # type: ignore[assignment]
    ) -> None:
        """Met à jour la config d'un split individuel dans une zone (§3).

        Seules les clés explicitement fournies sont modifiées ; les autres
        restent inchangées (None = hériter du niveau zone). Persiste dans
        ConfigEntry.options → CONF_ZONES → zone → splits_config.
        """
        zone = self._zones.get(zone_id)
        if not zone:
            return
        split_cfg: dict[str, Any] = dict(zone.config.splits_config.get(climate_entity, {}))
        if target is not ...:
            split_cfg["target"] = target
        if power is not ...:
            split_cfg["power"] = power
        if swing is not ...:
            split_cfg["swing"] = swing
        zone.config.splits_config[climate_entity] = split_cfg
        # Persiste : tout le splits_config de la zone dans options
        new_sc = {k: dict(v) for k, v in zone.config.splits_config.items()}
        self._persist_zone_config(zone_id, splits_config=new_sc)
        self.async_set_updated_data(self._build_coordinator_data())

    async def async_tick_now(self) -> None:
        """Force an immediate tick (used after a service call)."""
        await self.async_request_refresh()

    # === DataUpdateCoordinator hooks ===

    async def _async_setup(self) -> None:
        """Register state-change listeners — called once before first refresh."""
        await self._load_runtime_state()
        await self._setup_state_listeners()

    async def _async_update_data(self) -> dict[str, Any]:
        """Tick all zones — sauf en mode observation où on se contente de lire."""
        if not self.control_enabled():
            # Aucune mutation d'état, aucune commande : la carte reflète juste
            # les températures et l'état réel des splits.
            return self._build_coordinator_data()
        now = utc_now_ts()
        # Évaluation du cycle hors-gel avant de ticker les zones
        self._tick_frost(now)
        for zone in self._zones.values():
            # Hors-gel : forcer la direction sur les zones qui se retrouveraient
            # en IDLE / SCHEDULE_OFF alors que le cycle doit les maintenir actives.
            if self._frost_active():
                self._ensure_frost_direction(zone, now)
            inputs = self._gather_inputs(zone)
            commands = zone.tick(inputs)
            for cmd in commands:
                await self._apply_command(cmd)
        await self._save_runtime_state_if_changed()
        return self._build_coordinator_data()

    async def _load_runtime_state(self) -> None:
        """Restore per-zone runtime state from disk.

        v0.17.3+ stores the full ZoneRuntimeState under ``zones``. Older
        releases stored only completed cycle history in ``*_cycles_*``; if no
        runtime payload exists yet, import that history so the UI does not lose
        past sessions on upgrade.
        """
        data = await self._runtime_store.async_load() or {}
        zones_data = data.get("zones", {}) if isinstance(data, dict) else {}

        if zones_data:
            for zid, zone in self._zones.items():
                restored = ZoneRuntimeState.from_dict(zones_data.get(zid))
                # Keep the fresh ZoneConfig, restore only runtime.
                zone.state = restored
            # Restore frost state if present in the payload
            frost_data = data.get("frost") or {}
            self._frost_start_ts = _as_float(frost_data.get("start_ts"))
            self._frost_direction = frost_data.get("direction")
            self._last_runtime_payload = self._runtime_payload()
            return

        legacy = await self._legacy_cycle_store.async_load() or {}
        legacy_zones = legacy.get("zones", {}) if isinstance(legacy, dict) else {}
        for zid, zone in self._zones.items():
            zone.state.completed_cycles = list(legacy_zones.get(zid, []))
        # Installation fraîche en mode observation : on affiche toutes les zones
        # éteintes (rien n'est piloté de toute façon). L'utilisateur les remet en
        # Marche après avoir activé le pilotage.
        if not self.control_enabled():
            for zone in self._zones.values():
                zone.state.mode = ZoneMode.OFF
        self._last_runtime_payload = self._runtime_payload()

    def _runtime_payload(self) -> dict[str, Any]:
        return {
            "zones": {zid: zone.state.to_dict() for zid, zone in self._zones.items()},
            # Cycle hors-gel (§2) — persisté pour survivre aux restarts HA.
            "frost": {
                "start_ts": self._frost_start_ts,
                "direction": self._frost_direction,
            },
        }

    async def _save_runtime_state_if_changed(self) -> None:
        """Persist runtime state when it changed since the last tick.

        This is deliberately broader than completed-history persistence: timed
        phases and cycle anchors are safety-critical after a HA restart.
        """
        payload = self._runtime_payload()
        if payload != self._last_runtime_payload:
            await self._runtime_store.async_save(payload)
            self._last_runtime_payload = payload

    # === Zone setup / rebuild ===

    def _rebuild_zones(self) -> None:
        """(Re)build zones from ConfigEntry.options['zones'].

        Le flag pendulum_idle est système (entry.data) : on le propage à tous
        les ZoneConfig reconstruits pour que la logique Zone puisse le lire
        directement sans remonter au coordinator à chaque tick.
        """
        pendulum_idle = bool(
            self.entry.data.get(CONF_PENDULUM_IDLE, DEFAULT_PENDULUM_IDLE)
        )
        zones_cfg = self.entry.options.get(CONF_ZONES, [])
        new_zones: dict[str, Zone] = {}
        for cfg_dict in zones_cfg:
            zc = ZoneConfig.from_dict(cfg_dict)
            zc.pendulum_idle = pendulum_idle  # flag système → toutes les zones
            existing = self._zones.get(zc.zone_id)
            new_zones[zc.zone_id] = Zone(
                zc, state=existing.state if existing else ZoneRuntimeState()
            )
        self._zones = new_zones

    async def _setup_state_listeners(self) -> None:
        """Re-register state listeners on each rebuild."""
        if self._unsub_state_listener:
            self._unsub_state_listener()
            self._unsub_state_listener = None
        # A rebuild invalidates any pending override decisions: the zones may
        # be different, and we'd resolve against a stale Zone reference.
        self._cancel_pending_overrides()
        self._setup_alarm_listener()
        if not self._zones:
            return
        # Multi-splits : on écoute TOUS les splits de toutes les zones.
        entities: list[str] = []
        for z in self._zones.values():
            entities.extend(z.config.climate_entities or [z.config.climate_entity])
        entities = list(dict.fromkeys(e for e in entities if e))
        if entities:
            self._unsub_state_listener = async_track_state_change_event(
                self.hass, entities, self._on_clim_state_changed
            )

    def _alarm_entity(self) -> str | None:
        """Entité présence/alarme globale (sert au gating + reset au désarmement)."""
        return self.entry.data.get(CONF_PRESENCE_ENTITY)

    def _setup_alarm_listener(self) -> None:
        """(Re)pose le listener sur l'alarme pour le reset au désarmement."""
        if self._unsub_alarm_listener:
            self._unsub_alarm_listener()
            self._unsub_alarm_listener = None
        ent = self._alarm_entity()
        if not ent:
            return
        self._unsub_alarm_listener = async_track_state_change_event(
            self.hass, [ent], self._on_alarm_state_changed
        )

    @callback
    def _on_alarm_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Front de désarmement → reset quotidien de toutes les zones.

        Quand le 1er arrivant désarme l'alarme le matin, on remet chaque zone au
        profil par défaut (ON + Normal) et on purge tout override laissé par un
        collègue la veille. L'extinction du soir est gérée par le gating
        (alarme armée → schedule_off → splits coupés), pas ici.
        """
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or old_state is None:
            return
        if new_state.state == "disarmed" and old_state.state != "disarmed":
            _LOGGER.info("Alarme désarmée → reset quotidien des zones")
            self.reset_all_zones_to_default()

    async def async_reload_zones(self) -> None:
        """Rebuild zones (after a config update)."""
        self._rebuild_zones()
        await self._setup_state_listeners()
        await self.async_request_refresh()

    # === State listener: detect external overrides ===

    # Attributes on a climate.* entity that a user (or app) actively chooses.
    # A change to current_temperature, last_updated, etc. is the integration's
    # own polling — NOT an override. Detecting override on those was the v0.1.x
    # bug where every Daikin poll silently flipped the zone to MANUAL_OVERRIDE_TIMED.
    _OVERRIDE_TRIGGER_ATTRS = frozenset(
        {
            "temperature",  # setpoint
            "fan_mode",
            "swing_mode",
            "swing_horizontal_mode",
            "preset_mode",
            "target_temp_high",
            "target_temp_low",
        }
    )

    @callback
    def _on_clim_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        zone = next(
            (
                z
                for z in self._zones.values()
                if entity_id in (z.config.climate_entities or [z.config.climate_entity])
            ),
            None,
        )
        if zone is None:
            return
        if self._context_tracker.is_ours(event.context):
            return
        # Did anything user-actionable actually change? If old_state is None this
        # is the initial state (HA boot or integration reload) — not an override.
        if old_state is None:
            return
        if old_state.state == new_state.state and not self._user_action_changed(
            old_state, new_state
        ):
            return
        # Debounce: the Daikin BRP integration occasionally emits temperature
        # flaps (X→Y→X) on poll, in two events at the same timestamp. Reacting
        # to the first wrongly trips MANUAL_OVERRIDE_TIMED. Coalesce events per
        # entity, then at fire time compare the cumulative diff to what we last
        # commanded — if it's an echo of our intent, ignore it.
        pending = self._pending_overrides.get(entity_id)
        if pending is None:
            pending = {"old_state": old_state, "new_state": new_state, "handle": None}
            self._pending_overrides[entity_id] = pending
        else:
            pending["new_state"] = new_state
            if pending["handle"] is not None:
                pending["handle"].cancel()
        pending["handle"] = self.hass.loop.call_later(
            OVERRIDE_DEBOUNCE_SECONDS,
            self._resolve_pending_override,
            entity_id,
            zone,
        )

    @callback
    def _resolve_pending_override(self, entity_id: str, zone: Zone) -> None:
        """Fire after the debounce window. Decide if it's a real override."""
        pending = self._pending_overrides.pop(entity_id, None)
        if pending is None:
            return
        old_state = pending["old_state"]
        new_state = pending["new_state"]
        # Echo check: latest state matches what we last commanded → no override.
        if _is_echo_of_intent(zone, new_state.attributes or {}):
            return
        # Cumulative diff: in the X→Y→X flap, old.temperature == new.temperature
        # so _user_action_changed returns False here and we bail.
        if old_state.state == new_state.state and not self._user_action_changed(
            old_state, new_state
        ):
            return
        now = utc_now_ts()
        schedule_on = self._active_profile(zone) is not None
        zone.on_external_override(now, schedule_on)
        self.hass.async_create_task(self.async_request_refresh())

    def _cancel_pending_overrides(self) -> None:
        for pending in self._pending_overrides.values():
            handle = pending.get("handle")
            if handle is not None:
                handle.cancel()
        self._pending_overrides.clear()

    async def async_shutdown(self) -> None:  # type: ignore[override]
        self._cancel_pending_overrides()
        if self._unsub_state_listener:
            self._unsub_state_listener()
            self._unsub_state_listener = None
        if self._unsub_alarm_listener:
            self._unsub_alarm_listener()
            self._unsub_alarm_listener = None
        await super().async_shutdown()

    def _user_action_changed(self, old_state, new_state) -> bool:
        """Return True iff a user-actionable attribute differs between old and new."""
        old_attrs = old_state.attributes or {}
        new_attrs = new_state.attributes or {}
        for attr in self._OVERRIDE_TRIGGER_ATTRS:
            if old_attrs.get(attr) != new_attrs.get(attr):
                return True
        return False

    # === Apply commands ===

    async def _apply_command(self, cmd: Command) -> None:
        ctx = Context()
        self._context_tracker.track(ctx)
        try:
            await self.hass.services.async_call(
                cmd.domain, cmd.service, cmd.data, blocking=False, context=ctx
            )
        except Exception:
            _LOGGER.exception("Failed to call %s.%s with %s", cmd.domain, cmd.service, cmd.data)

    # === Inputs gathering ===

    def _active_profile(self, zone: Zone) -> Profile | None:
        """Return the first profile whose gate matches the current state, or None.

        Cascade rules:
        - A profile's schedule entity must be ON (or be None, meaning "always on")
        - If presence_entity is set, its current state must be in the
          presence_required_state (str or list of str). Both being None means
          no presence condition.

        Order matters: the user puts the more specific conditions (e.g. needing
        absence) at the top of the list, and a generic fallback last.
        """
        for p in zone.config.profiles:
            if not self._profile_schedule_on(p):
                continue
            if not self._profile_presence_match(p):
                continue
            return p
        return None

    def _profile_schedule_on(self, p: Profile) -> bool:
        if not p.schedule_entity:
            return True
        st = self.hass.states.get(p.schedule_entity)
        if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return True  # fail-open like _read_schedule_on for the zone-level entity
        return st.state == STATE_ON

    def _profile_presence_match(self, p: Profile) -> bool:
        if not p.presence_entity:
            return True
        required = p.presence_required_state
        if required is None:
            return True
        st = self.hass.states.get(p.presence_entity)
        if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False  # fail-closed on presence: don't assume the condition holds
        if isinstance(required, str):
            return st.state == required
        return st.state in required

    def _gather_inputs(self, zone: Zone) -> ZoneInputs:
        now = utc_now_ts()
        room_temperature = self._average_temperature(zone.config.temperature_sensors)
        splits = zone.config.climate_entities or [zone.config.climate_entity]

        internals: list[float] = []
        internals_by_entity: dict[str, float] = {}
        setpoints: list[float] = []
        modes: list[str] = []
        fans: set[Any] = set()
        swings: set[Any] = set()
        clim_last_changed_ts: float | None = None
        clim_setpoint_step = DEFAULT_SETPOINT_STEP
        # Capabilities : lues sur le 1er split disponible (même modèle dans une
        # zone). None tant qu'aucun split lisible → defaults permissifs ensuite.
        supports_cool: bool | None = None
        supports_heat: bool | None = None
        supports_fan_mode: bool | None = None
        supports_windnice: bool | None = None

        for ent in splits:
            st = self.hass.states.get(ent)
            if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            attrs = st.attributes
            modes.append(st.state)
            it = _as_float(attrs.get(ATTR_CURRENT_TEMPERATURE))
            if it is not None:
                internals.append(it)
                internals_by_entity[ent] = it  # sonde par split pour §3
            sp = _as_float(attrs.get(ATTR_TEMPERATURE))
            if sp is not None:
                setpoints.append(sp)
            fans.add(attrs.get(ATTR_FAN_MODE))
            swings.add(attrs.get(ATTR_SWING_MODE))
            step = _as_float(attrs.get("target_temp_step"))
            if step:
                clim_setpoint_step = step
            if supports_cool is None:
                supports_cool = "cool" in (attrs.get("hvac_modes") or [])
                supports_heat = "heat" in (attrs.get("hvac_modes") or [])
                supports_fan_mode = bool(attrs.get("fan_modes") or [])
                supports_windnice = "windnice" in (attrs.get("swing_modes") or [])
            if st.last_changed is not None:
                clim_last_changed_ts = st.last_changed.timestamp()

        if supports_cool is None:  # aucun split lisible → permissif
            supports_cool = supports_heat = supports_fan_mode = supports_windnice = True

        # Représentant consensuel pour les comparaisons "déjà dans le bon état ?".
        clim_hvac = _consensus_mode(modes)
        clim_internal = (sum(internals) / len(internals)) if internals else None
        clim_setpoint = _consensus_setpoint(setpoints)
        clim_fan = fans.pop() if len(fans) == 1 else None
        clim_swing = swings.pop() if len(swings) == 1 else None

        active_profile = self._active_profile(zone)
        # Gating boulot : le système n'est actif que lorsque l'alarme est
        # désarmée (bâtiment occupé). Armée → schedule_off → splits coupés.
        # Exception : cycle hors-gel (§2) → system_enabled forcé à True.
        system_enabled = not self._house_is_absent() or self._frost_active()
        return ZoneInputs(
            now_ts=now,
            room_temperature=room_temperature,
            clim_internal_temperature=clim_internal,
            clim_current_hvac_mode=clim_hvac,
            clim_current_setpoint=clim_setpoint,
            clim_current_fan_mode=clim_fan,
            clim_current_swing_mode=clim_swing,
            schedule_is_on=system_enabled and active_profile is not None,
            any_window_open=self._any_window_open(zone),
            house_is_absent=self._house_is_absent(),
            supports_cool=supports_cool,
            supports_heat=supports_heat,
            supports_fan_mode=supports_fan_mode,
            supports_windnice=supports_windnice,
            clim_state_last_changed_ts=clim_last_changed_ts,
            active_profile=active_profile,
            clim_internal_temperatures=tuple(internals),
            clim_setpoint_step=clim_setpoint_step,
            clim_internal_by_entity=internals_by_entity,
        )

    def _average_temperature(self, sensors: list[str]) -> float | None:
        values: list[float] = []
        for sid in sensors:
            st = self.hass.states.get(sid)
            if not st or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            v = _as_float(st.state)
            if v is not None:
                values.append(v)
        if not values:
            return None
        return sum(values) / len(values)

    def _schedule_next_event(self, zone: Zone) -> str | None:
        """ISO timestamp of the next schedule transition, or None.

        With multi-profile, we surface the next transition of the *currently
        active* profile's schedule if there is one; otherwise the first
        upcoming transition across the configured profiles.
        """
        active = self._active_profile(zone)
        candidates = []
        if active and active.schedule_entity:
            candidates.append(active.schedule_entity)
        for p in zone.config.profiles:
            if p.schedule_entity and p.schedule_entity not in candidates:
                candidates.append(p.schedule_entity)
        for ent in candidates:
            st = self.hass.states.get(ent)
            if not st:
                continue
            nxt = st.attributes.get("next_event")
            if nxt:
                return str(nxt)
        return None

    def _any_window_open(self, zone: Zone) -> bool:
        for ent in zone.config.window_sensors:
            st = self.hass.states.get(ent)
            if st and st.state == STATE_ON:
                return True
        return False

    def _window_counts(self, zone: Zone) -> tuple[int, int]:
        """Return (open_count, total_count) for the zone's window sensors."""
        total = len(zone.config.window_sensors)
        open_n = 0
        for ent in zone.config.window_sensors:
            st = self.hass.states.get(ent)
            if st and st.state == STATE_ON:
                open_n += 1
        return open_n, total

    # === Protection hors-gel (§2) ===

    def _frost_protection_enabled(self) -> bool:
        return bool(self.entry.data.get(CONF_FROST_PROTECTION_ENABLED, DEFAULT_FROST_PROTECTION_ENABLED))

    def _frost_min_temp(self) -> float:
        return float(self.entry.data.get(CONF_FROST_MIN_TEMP, DEFAULT_FROST_MIN_TEMP))

    def _frost_max_temp(self) -> float:
        return float(self.entry.data.get(CONF_FROST_MAX_TEMP, DEFAULT_FROST_MAX_TEMP))

    def _frost_duration_min(self) -> int:
        return int(self.entry.data.get(CONF_FROST_DURATION_MIN, DEFAULT_FROST_DURATION_MIN))

    def _frost_active(self) -> bool:
        """True si un cycle hors-gel est en cours (démarré et non expiré)."""
        return self._frost_start_ts is not None

    def _tick_frost(self, now: float) -> None:
        """Évalue le déclenchement / l'arrêt du cycle hors-gel à chaque tick.

        - Si un cycle est en cours et sa durée fixe est écoulée → arrêt.
        - Si aucun cycle et conditions remplies (bâtiment absent + frost activé
          + T° moyenne hors bornes) → démarrage.
        """
        if not self._frost_protection_enabled():
            if self._frost_active():
                self._end_frost_cycle()
            return

        if not self._house_is_absent():
            # Bâtiment occupé → pas de hors-gel (la régulation normale prend le relais)
            if self._frost_active():
                self._end_frost_cycle()
            return

        # Vérifier fin de cycle en cours
        if self._frost_start_ts is not None:
            elapsed = now - self._frost_start_ts
            if elapsed >= self._frost_duration_min() * 60:
                _LOGGER.info("Hors-gel : cycle terminé après %d min", self._frost_duration_min())
                self._end_frost_cycle()
            return  # cycle en cours, rien à déclencher

        # Vérifier déclenchement sur T° bâtiment
        avg = self._building_avg_temperature()
        if avg is None:
            return
        if avg <= self._frost_min_temp():
            _LOGGER.info(
                "Hors-gel : T° bâtiment %.1f°C ≤ seuil %.1f°C → chauffage",
                avg, self._frost_min_temp(),
            )
            self._start_frost_cycle("heat", now)
        elif avg >= self._frost_max_temp():
            _LOGGER.info(
                "Hors-gel : T° bâtiment %.1f°C ≥ seuil %.1f°C → refroidissement",
                avg, self._frost_max_temp(),
            )
            self._start_frost_cycle("cool", now)

    def _start_frost_cycle(self, direction: str, now: float) -> None:
        self._frost_start_ts = now
        self._frost_direction = direction

    def _end_frost_cycle(self) -> None:
        self._frost_start_ts = None
        self._frost_direction = None

    def _ensure_frost_direction(self, zone: Zone, now: float) -> None:
        """Pendant le hors-gel, s'assurer que la zone va s'activer dans la bonne direction.

        On appelle force_start() seulement si la zone est dans un état passif
        (IDLE, SCHEDULE_OFF, COOLDOWN, WINDOW_OPEN) pour ne pas interrompre un
        cycle actif déjà dans le bon sens.

        Note : _frost_direction est 'heat' ou 'cool' — correspond directement
        aux valeurs HVACMode (chaînes identiques) → on passe la chaîne brute.
        """
        direction = self._frost_direction
        if direction is None:
            return
        passive_states = (
            ZoneState.IDLE, ZoneState.SCHEDULE_OFF,
            ZoneState.COOLDOWN, ZoneState.WINDOW_OPEN,
        )
        if zone.state.state in passive_states:
            zone.force_start(direction, now)

    def _building_avg_temperature(self) -> float | None:
        """Température moyenne de toutes les sondes de pièce de toutes les zones."""
        all_sensors: list[str] = []
        for zone in self._zones.values():
            all_sensors.extend(zone.config.temperature_sensors)
        if not all_sensors:
            return None
        return self._average_temperature(all_sensors)

    def _house_is_absent(self) -> bool:
        ent = self.entry.data.get(CONF_PRESENCE_ENTITY)
        absent_states = self.entry.data.get(CONF_PRESENCE_ABSENT_STATES, [])
        if not ent or not absent_states:
            return False
        st = self.hass.states.get(ent)
        if not st:
            return False
        return st.state in absent_states

    # === Persistence of mutable zone config ===

    def _persist_zone_config(self, zone_id: str, **kwargs: Any) -> None:
        """Save changed zone fields back to ConfigEntry.options."""
        zones = list(self.entry.options.get(CONF_ZONES, []))
        for i, z in enumerate(zones):
            if z.get("id") == zone_id:
                zones[i] = {**z, **kwargs}
                break
        new_opts = {**self.entry.options, CONF_ZONES: zones}
        self.hass.config_entries.async_update_entry(self.entry, options=new_opts)

    # === Data exposed to platforms ===

    def _build_coordinator_data(self) -> dict[str, Any]:
        # Données système : frost
        frost_ends_ts: float | None = None
        if self._frost_start_ts is not None:
            frost_ends_ts = self._frost_start_ts + self._frost_duration_min() * 60
        out: dict[str, Any] = {
            "zones": {},
            "frost": {
                "active": self._frost_active(),
                "direction": self._frost_direction,
                "start_ts": self._frost_start_ts,
                "ends_ts": frost_ends_ts,
                "enabled": self._frost_protection_enabled(),
                "min_temp": self._frost_min_temp(),
                "max_temp": self._frost_max_temp(),
                "duration_min": self._frost_duration_min(),
            },
        }
        for zid, zone in self._zones.items():
            inputs = self._gather_inputs(zone)
            # Derived: when we entered the current state, and (for timed states)
            # when we'll leave it. Exposing these lets the Lovelace card render
            # narrative timers like "stabilisation jusqu'à 11:25".
            entered_ts = zone.state.last_state_transition_ts or None
            stabilization_ends_ts = None
            cooldown_ends_ts = None
            if zone.state.state == ZoneState.STABILIZING and entered_ts:
                stabilization_ends_ts = entered_ts + zone.config.duree_stabilisation_min * 60
            if zone.state.state == ZoneState.COOLDOWN and entered_ts:
                cooldown_ends_ts = entered_ts + zone.config.duree_cooldown_min * 60

            # Direction & target temperature inferred from underlying clim mode
            # (more reliable than guessing from thresholds + room temp).
            # Thresholds come from the active profile when there is one;
            # fallback to zone defaults if not (e.g. zone idle without a
            # matching profile, or transition gap).
            active = inputs.active_profile or zone.config.profiles[0]
            # Seuils effectifs : dérivés de la cible de zone si elle est définie.
            active = _apply_target_temp(active, zone.config.target_temp)
            clim_mode = inputs.clim_current_hvac_mode
            direction: str | None = None
            target_temperature: float | None = None
            if clim_mode == "cool":
                direction = "cool"
                target_temperature = active.seuil_fin_refroidissement
            elif clim_mode == "heat":
                direction = "heat"
                target_temperature = active.seuil_fin_chauffage
            elif zone.state.state in (ZoneState.STARTING, ZoneState.RUNNING):
                rt = inputs.room_temperature
                if rt is not None and rt > active.seuil_debut_refroidissement:
                    direction, target_temperature = "cool", active.seuil_fin_refroidissement
                elif rt is not None and rt < active.seuil_debut_chauffage:
                    direction, target_temperature = "heat", active.seuil_fin_chauffage

            out["zones"][zid] = {
                "config": zone.config,
                "state": zone.state.state,
                "regime": zone.state.regime,
                "mode": zone.state.mode,
                "room_temperature": inputs.room_temperature,
                "clim_internal_temperature": inputs.clim_internal_temperature,
                "clim_current_setpoint": inputs.clim_current_setpoint,
                "last_setpoint_sent": zone.state.last_setpoint_sent,
                "override_until_ts": zone.state.override_until_ts,
                "boost_until_ts": zone.state.boost_until_ts,
                "schedule_on": inputs.schedule_is_on,
                "any_window_open": inputs.any_window_open,
                "house_is_absent": inputs.house_is_absent,
                "in_override": zone.state.state
                in (ZoneState.MANUAL_OVERRIDE_TIMED, ZoneState.MANUAL_OVERRIDE_FREE),
                "is_off_mode": zone.state.mode == ZoneMode.OFF,
                "state_entered_ts": entered_ts,
                "stabilization_ends_ts": stabilization_ends_ts,
                "cooldown_ends_ts": cooldown_ends_ts,
                "cycle_started_ts": zone.state.cycle_started_ts,
                "direction": direction,
                "target_temperature": target_temperature,
                "aggressivity": zone.config.aggressivity,  # legacy alias
                "power": active.power,
                "fan_intensity": active.fan_intensity,
                "supports_cool": inputs.supports_cool,
                "supports_heat": inputs.supports_heat,
                "supports_fan_mode": inputs.supports_fan_mode,
                "supports_windnice": inputs.supports_windnice,
                "schedule_next_event": self._schedule_next_event(zone),
                "windows_open": self._window_counts(zone)[0],
                "windows_total": self._window_counts(zone)[1],
                # Profiles surfaced for the card §2: list of profiles in priority
                # order + the name of the currently active one (or None when no
                # profile matches → zone gated off).
                "profiles": [p.to_dict() for p in zone.config.profiles],
                "active_profile_name": (
                    inputs.active_profile.name if inputs.active_profile else None
                ),
                # Cible de zone (thermostat) — None = 4 seuils classiques.
                "target_temp": zone.config.target_temp,
                # Historical cycles for §5 of the card. List of dicts, newest
                # at the end; coordinator persists across HA restarts.
                "cycle_history": zone.state.completed_cycles,
                # Données par split pour la carte (§3). Pour chaque split de
                # la zone, expose les paramètres actuels + hérités + état clim.
                "splits": self._build_splits_data(zone, inputs),
                # Direction pendule verrouillée (§1)
                "active_direction": zone.state.active_direction,
            }
        return out

    def _build_splits_data(self, zone: Zone, inputs: ZoneInputs) -> list[dict[str, Any]]:
        """Données par split exposées à la carte pour l'affichage et l'édition.

        Format :
        {
            entity_id: str,
            name: str,            # simplifié depuis l'entity_id
            internal_temp: float|None,
            current_setpoint: float|None,
            current_swing: str|None,
            hvac_mode: str,       # état hvac réel de CE split
            # Paramètres configurés (None = hérité du niveau zone)
            target: float|None,
            power: str|None,
            swing: str|None,
        }
        """
        splits_data: list[dict[str, Any]] = []
        splits = zone.config.climate_entities or [zone.config.climate_entity]
        active = inputs.active_profile or (zone.config.profiles[0] if zone.config.profiles else None)
        # Seuils effectifs (dérivés de la cible de zone) pour l'héritage des splits.
        if active is not None:
            active = _apply_target_temp(active, zone.config.target_temp)

        for ent in splits:
            st = self.hass.states.get(ent)
            split_cfg = zone.config.splits_config.get(ent, {})
            attrs = st.attributes if st else {}

            # Nom court : dernier segment du entity_id après le point, sans le préfixe domaine
            short_name = ent.split(".")[-1] if "." in ent else ent

            internal_temp = inputs.clim_internal_by_entity.get(ent)
            current_sp = _as_float(attrs.get(ATTR_TEMPERATURE)) if attrs else None
            current_swing = attrs.get(ATTR_SWING_MODE) if attrs else None
            hvac_mode = st.state if st else "unavailable"

            # target héritée : seuil_fin du sens actif du profil si non défini
            inherited_target: float | None = None
            if active:
                dir_ = zone.state.active_direction or inputs.clim_current_hvac_mode
                if dir_ == "cool":
                    inherited_target = active.seuil_fin_refroidissement
                elif dir_ == "heat":
                    inherited_target = active.seuil_fin_chauffage

            splits_data.append({
                "entity_id": ent,
                "name": short_name,
                "internal_temp": internal_temp,
                "current_setpoint": current_sp,
                "current_swing": current_swing,
                "hvac_mode": hvac_mode,
                "target": split_cfg.get("target"),          # None = hérité
                "power": split_cfg.get("power"),            # None = hérité
                "swing": split_cfg.get("swing"),            # None = hérité
                "effective_target": split_cfg.get("target") if split_cfg.get("target") is not None else inherited_target,
            })
        return splits_data


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _consensus_mode(modes: list[str]) -> str:
    """Mode hvac représentatif d'une zone multi-splits.

    Tous d'accord → ce mode. Désaccord → on privilégie cool puis heat (si un
    split chauffe/refroidit, la zone est considérée active dans ce sens), ce qui
    force la resynchronisation des splits en retard au tick suivant.
    """
    if not modes:
        return STATE_OFF
    uniq = set(modes)
    if len(uniq) == 1:
        return modes[0]
    if "cool" in uniq:
        return "cool"
    if "heat" in uniq:
        return "heat"
    return STATE_OFF


def _consensus_setpoint(setpoints: list[float]) -> float | None:
    """Consigne représentative. Splits alignés (écart < delta) → moyenne ;
    désaccord → None pour forcer la réémission (dans la limite du rate-limit)."""
    if not setpoints:
        return None
    if max(setpoints) - min(setpoints) < SETPOINT_NOOP_DELTA:
        return sum(setpoints) / len(setpoints)
    return None


def _is_echo_of_intent(zone: Zone, new_attrs: dict[str, Any]) -> bool:
    """Pure helper: True iff the post-debounce attributes match what the zone
    last commanded — i.e. this state_changed burst is just the Daikin
    integration echoing our own writes back at us.

    We only consider it an echo when *every* attribute we have an intent for
    matches. Attributes we never set (preset_mode, swing_horizontal_mode,
    target_temp_high/low) are not considered: any movement on those still
    counts as a user action and falls through to the cumulative-diff check.
    """
    last_sp = zone.state.last_setpoint_sent
    if last_sp is None:
        return False
    cur_sp = _as_float(new_attrs.get(ATTR_TEMPERATURE))
    if cur_sp is None or abs(cur_sp - last_sp) >= SETPOINT_NOOP_DELTA:
        return False
    last_fan = zone.state.last_fan_sent
    if last_fan is not None and new_attrs.get(ATTR_FAN_MODE) != last_fan:
        return False
    return True
