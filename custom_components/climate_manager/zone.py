"""Zone logic: state machine, decision algorithm, pilot algorithm.

A Zone is a single climate entity + its room temperature sensors + thresholds.
It has its own state machine and computes commands to send to the AC.

The Zone class is pure logic — it does not directly call HA services. The
coordinator passes inputs (current temperatures, schedule state, etc.) to
`tick()`, gets back a list of Commands, and applies them.

This separation makes Zone unit-testable without mocking HA.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_SWING_MODE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE

from .const import (
    BOOST_DURATION_MIN,
    BOOST_FAN_MODE,
    BOOST_OFFSET,
    CLIM_MAX_SETPOINT,
    CLIM_MIN_SETPOINT,
    DEFAULT_AGGRESSIVITY,
    DEFAULT_DUREE_COOLDOWN_MIN,
    DEFAULT_DUREE_STABILISATION_MIN,
    DEFAULT_FAN_INTENSITY,
    DEFAULT_OVERRIDE_DUREE_MIN,
    DEFAULT_PENDULUM_IDLE,
    DEFAULT_POWER,
    DEFAULT_SETPOINT_STEP,
    DEFAULT_SEUIL_DEBUT_CHAUFFAGE,
    DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT,
    DEFAULT_SEUIL_FIN_CHAUFFAGE,
    DEFAULT_SEUIL_FIN_REFROIDISSEMENT,
    DEFAULT_SWING_MODE,
    DEFAULT_TARGET_DEADBAND,
    FAN_PROFILES,
    POWER_PROFILES,
    RATE_LIMIT_SECONDS,
    SETPOINT_NOOP_DELTA,
    Regime,
    ZoneMode,
    ZoneState,
)

_LOGGER = logging.getLogger(__name__)


# === Inputs / Outputs ===


@dataclass(frozen=True)
class ZoneInputs:
    """Everything the zone needs to decide its next action."""

    now_ts: float
    room_temperature: float | None  # moyenne capteurs, None si tous indispo
    # Sonde interne "représentative" (moyenne des splits) — affichage + repli
    # mono-split. Le pilotage multi-splits utilise clim_internal_temperatures.
    clim_internal_temperature: float | None
    clim_current_hvac_mode: str  # 'off' | 'heat' | 'cool' | ...
    clim_current_setpoint: float | None
    clim_current_fan_mode: str | None
    clim_current_swing_mode: str | None
    schedule_is_on: bool
    any_window_open: bool
    house_is_absent: bool
    # Capabilities auto-detected from the underlying climate.* entity
    supports_cool: bool = True
    supports_heat: bool = True
    supports_fan_mode: bool = True
    supports_windnice: bool = True
    # Wall-time of the climate entity's last state change (clim_state.last_changed).
    # Used as a best-effort anchor for cycle_started_ts when the integration
    # adopts an already-running clim (boot recovery, reset_override).
    clim_state_last_changed_ts: float | None = None
    # Active profile selected by the coordinator (the first whose gate matches
    # at this tick). None when no profile matches → zone idle. Zone code reads
    # all driver values (seuils, power, fan_intensity) from this when set;
    # falls back to ZoneConfig defaults otherwise (test paths that don't go
    # through the coordinator).
    active_profile: Profile | None = None
    # Sondes internes par split (multi-splits). Vide en mono-split → on retombe
    # sur clim_internal_temperature. Sert d'ancre au pendule : on prend la plus
    # froide en cool / la plus chaude en heat pour qu'AUCUN split ne soit
    # neutralisé par une consigne partagée au-dessus de sa propre sonde.
    clim_internal_temperatures: tuple[float, ...] = ()
    # Pas de consigne de la clim (target_temp_step). Hitachi/Modbus = 1.0.
    clim_setpoint_step: float = DEFAULT_SETPOINT_STEP
    # Sonde interne PAR split (entity_id → T°). Nécessaire pour le pilotage
    # par split (§3) : chaque split reçoit une consigne ancrée sur SA propre
    # sonde plutôt que sur la moyenne zone.
    clim_internal_by_entity: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Command:
    """A single HA service call the coordinator should execute on behalf of the zone."""

    domain: str
    service: str
    data: dict[str, Any]


# === Zone state holder ===


CYCLE_HISTORY_MAX = 10
ACTIVE_CYCLE_STATES = frozenset(
    {ZoneState.STARTING, ZoneState.RUNNING, ZoneState.STABILIZING}
)


@dataclass
class ZoneRuntimeState:
    """Mutable runtime state of a zone.

    This state is intentionally serialisable: HA restarts must not turn an
    in-progress STABILIZING phase back into a brand-new RUNNING/STARTING cycle.
    The coordinator persists it through Home Assistant Store after each tick.
    """

    state: str = ZoneState.IDLE
    regime: str = Regime.NONE
    last_state_transition_ts: float = 0.0
    last_command_ts: float = 0.0
    last_setpoint_sent: float | None = None
    last_fan_sent: str | None = None
    last_hvac_sent: str | None = None
    override_until_ts: float | None = None
    boost_until_ts: float | None = None
    mode: str = ZoneMode.AUTO  # auto / off / boost
    forced_direction: str | None = None  # 'cool' | 'heat' | None — set by force_start
    # Direction verrouillée par le pendule (§1). Distincte de forced_direction :
    # elle est engagée par le seuil de début et libérée uniquement quand la pièce
    # franchit le seuil de début OPPOSÉ. Persistée pour survivre aux restarts HA.
    active_direction: str | None = None  # 'cool' | 'heat' | None — pendule uniquement
    # Wall-time of the latest entry into STARTING. Survives RUNNING and
    # STABILIZING so the UI can render "démarré il y a Xmin" across the whole
    # cycle. Cleared whenever the zone leaves the active states.
    cycle_started_ts: float | None = None
    # Cycle snapshot fields — captured on entry into the active states and
    # used to build the historical CycleRecord when the cycle ends.
    cycle_start_room_temp: float | None = None
    cycle_start_profile_name: str | None = None
    cycle_min_room_temp: float | None = None
    cycle_regimes_seen: list[str] = field(default_factory=list)
    # Rolling history of completed cycles (most recent at the end).
    # Persisted by the coordinator via HA Store across restarts.
    completed_cycles: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for Home Assistant Store."""
        return {
            "state": self.state,
            "regime": self.regime,
            "last_state_transition_ts": self.last_state_transition_ts,
            "last_command_ts": self.last_command_ts,
            "last_setpoint_sent": self.last_setpoint_sent,
            "last_fan_sent": self.last_fan_sent,
            "last_hvac_sent": self.last_hvac_sent,
            "override_until_ts": self.override_until_ts,
            "boost_until_ts": self.boost_until_ts,
            "mode": self.mode,
            "forced_direction": self.forced_direction,
            "active_direction": self.active_direction,
            "cycle_started_ts": self.cycle_started_ts,
            "cycle_start_room_temp": self.cycle_start_room_temp,
            "cycle_start_profile_name": self.cycle_start_profile_name,
            "cycle_min_room_temp": self.cycle_min_room_temp,
            "cycle_regimes_seen": list(self.cycle_regimes_seen),
            "completed_cycles": list(self.completed_cycles),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ZoneRuntimeState:
        """Restore a runtime snapshot, tolerating older/partial store payloads."""
        if not isinstance(data, dict):
            return cls()
        valid_states = ZoneState.ALL
        valid_modes = ZoneMode.ALL
        state = str(data.get("state") or ZoneState.IDLE)
        mode = str(data.get("mode") or ZoneMode.AUTO)
        return cls(
            state=state if state in valid_states else ZoneState.IDLE,
            regime=str(data.get("regime") or Regime.NONE),
            last_state_transition_ts=_as_float_or_zero(data.get("last_state_transition_ts")),
            last_command_ts=_as_float_or_zero(data.get("last_command_ts")),
            last_setpoint_sent=_as_optional_float(data.get("last_setpoint_sent")),
            last_fan_sent=data.get("last_fan_sent"),
            last_hvac_sent=data.get("last_hvac_sent"),
            override_until_ts=_as_optional_float(data.get("override_until_ts")),
            boost_until_ts=_as_optional_float(data.get("boost_until_ts")),
            mode=mode if mode in valid_modes else ZoneMode.AUTO,
            forced_direction=data.get("forced_direction"),
            active_direction=data.get("active_direction"),
            cycle_started_ts=_as_optional_float(data.get("cycle_started_ts")),
            cycle_start_room_temp=_as_optional_float(data.get("cycle_start_room_temp")),
            cycle_start_profile_name=data.get("cycle_start_profile_name"),
            cycle_min_room_temp=_as_optional_float(data.get("cycle_min_room_temp")),
            cycle_regimes_seen=list(data.get("cycle_regimes_seen") or []),
            completed_cycles=list(data.get("completed_cycles") or []),
        )


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_float_or_zero(value: Any) -> float:
    parsed = _as_optional_float(value)
    return parsed if parsed is not None else 0.0


@dataclass
class Profile:
    """One driver (thresholds + power + fan) gated by a schedule + optional presence.

    A zone has an ordered list of Profiles. At each tick the coordinator picks
    the first whose gate matches the current state of the world (schedule on,
    and presence in required state if configured). That profile then drives
    the cooling cycle. If no profile matches → zone idle (= same as the old
    schedule_off behaviour).
    """

    name: str
    schedule_entity: str | None = None
    # Optional presence condition: profile matches only if the entity is in
    # the required state. State can be a single string or a list of accepted
    # strings (e.g. ["armed_away", "armed_night"] to mean "absent however").
    presence_entity: str | None = None
    presence_required_state: str | list[str] | None = None
    seuil_debut_chauffage: float = DEFAULT_SEUIL_DEBUT_CHAUFFAGE
    seuil_fin_chauffage: float = DEFAULT_SEUIL_FIN_CHAUFFAGE
    seuil_debut_refroidissement: float = DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT
    seuil_fin_refroidissement: float = DEFAULT_SEUIL_FIN_REFROIDISSEMENT
    power: str = DEFAULT_POWER
    fan_intensity: str = DEFAULT_FAN_INTENSITY

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Profile:
        return cls(
            name=str(d.get("name", "Profil")),
            schedule_entity=d.get("schedule_entity"),
            presence_entity=d.get("presence_entity"),
            presence_required_state=d.get("presence_required_state"),
            seuil_debut_chauffage=float(
                d.get("seuil_debut_chauffage", DEFAULT_SEUIL_DEBUT_CHAUFFAGE)
            ),
            seuil_fin_chauffage=float(d.get("seuil_fin_chauffage", DEFAULT_SEUIL_FIN_CHAUFFAGE)),
            seuil_debut_refroidissement=float(
                d.get("seuil_debut_refroidissement", DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT)
            ),
            seuil_fin_refroidissement=float(
                d.get("seuil_fin_refroidissement", DEFAULT_SEUIL_FIN_REFROIDISSEMENT)
            ),
            power=str(d.get("power", DEFAULT_POWER)),
            fan_intensity=str(d.get("fan_intensity", DEFAULT_FAN_INTENSITY)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schedule_entity": self.schedule_entity,
            "presence_entity": self.presence_entity,
            "presence_required_state": self.presence_required_state,
            "seuil_debut_chauffage": self.seuil_debut_chauffage,
            "seuil_fin_chauffage": self.seuil_fin_chauffage,
            "seuil_debut_refroidissement": self.seuil_debut_refroidissement,
            "seuil_fin_refroidissement": self.seuil_fin_refroidissement,
            "power": self.power,
            "fan_intensity": self.fan_intensity,
        }


@dataclass
class ZoneConfig:
    """Static config for a zone (from ConfigEntry.options)."""

    zone_id: str
    name: str
    # Représentant historique (1 zone = 1 split). Conservé pour la rétro-compat
    # des tests et des configs mono-split. Pour le boulot, une zone peut piloter
    # plusieurs splits → voir `climate_entities`.
    climate_entity: str
    temperature_sensors: list[str]
    schedule_entity: str | None
    # Tous les splits physiques pilotés par la zone (Openspace = stock +
    # reprographie, etc.). Normalisé dans __post_init__ : si vide, on retombe
    # sur [climate_entity] ; si climate_entity est vide, on prend le 1er.
    climate_entities: list[str] = field(default_factory=list)
    window_sensors: list[str] = field(default_factory=list)
    seuil_debut_chauffage: float = DEFAULT_SEUIL_DEBUT_CHAUFFAGE
    seuil_fin_chauffage: float = DEFAULT_SEUIL_FIN_CHAUFFAGE
    seuil_debut_refroidissement: float = DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT
    seuil_fin_refroidissement: float = DEFAULT_SEUIL_FIN_REFROIDISSEMENT
    duree_stabilisation_min: int = DEFAULT_DUREE_STABILISATION_MIN
    duree_cooldown_min: int = DEFAULT_DUREE_COOLDOWN_MIN
    override_duree_min: int = DEFAULT_OVERRIDE_DUREE_MIN
    # Boulot : quand un collègue prend la main, l'override tient jusqu'au prochain
    # reset (désarmement du matin) ou à l'extinction du soir, pas un timer fixe.
    # False = comportement maison (timer override_duree_min).
    override_until_reset: bool = False
    aggressive_when_absent: bool = True
    # Legacy single-knob (kept for backward compat in stored configs).
    aggressivity: str = DEFAULT_AGGRESSIVITY
    # New decoupled knobs — preferred. If a zone has only `aggressivity` in
    # storage (pre-v0.7 config), from_dict mirrors it into both power +
    # fan_intensity so behaviour is unchanged at upgrade time.
    power: str = DEFAULT_POWER
    fan_intensity: str = DEFAULT_FAN_INTENSITY
    # Ordered list of profiles (cascade evaluated top-to-bottom). If empty at
    # construction, __post_init__ synthesises one from the legacy fields so
    # an upgraded config keeps the same behaviour without any user action.
    profiles: list[Profile] = field(default_factory=list)
    # Mode pendule continu (§1). Flag système propagé par le coordinator.
    # Quand True : le split reste allumé en permanence (consigne de relâchement
    # quand la cible est atteinte au lieu de turn_off). Défaut False = comportement
    # historique — TOUS les tests existants passent avec False.
    pendulum_idle: bool = DEFAULT_PENDULUM_IDLE
    # Paramètres par split (§3). Clé = entity_id du split.
    # Valeurs possibles par entrée : target (float|None), power (str|None), swing (str|None).
    # None = hériter du niveau zone. Vide = comportement inchangé (tests existants OK).
    splits_config: dict[str, dict] = field(default_factory=dict)
    # Température cible UNIQUE de la zone (thermostat). Quand renseignée, elle
    # dérive les seuils : la zone se stabilise/relâche à `target_temp` et engage
    # le froid au-dessus de target+bande / le chaud en-dessous de target-bande.
    # None = on garde les 4 seuils configurés (comportement historique).
    target_temp: float | None = None

    def __post_init__(self) -> None:
        # Normalise le couple climate_entity / climate_entities pour que les deux
        # soient toujours cohérents quel que soit le point d'entrée (test mono-
        # split, config multi-splits, config legacy).
        if not self.climate_entities:
            self.climate_entities = [self.climate_entity] if self.climate_entity else []
        elif not self.climate_entity:
            self.climate_entity = self.climate_entities[0]
        if not self.profiles:
            self.profiles = [
                Profile(
                    name="Pilotage par défaut",
                    schedule_entity=self.schedule_entity,
                    seuil_debut_chauffage=self.seuil_debut_chauffage,
                    seuil_fin_chauffage=self.seuil_fin_chauffage,
                    seuil_debut_refroidissement=self.seuil_debut_refroidissement,
                    seuil_fin_refroidissement=self.seuil_fin_refroidissement,
                    power=self.power,
                    fan_intensity=self.fan_intensity,
                )
            ]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ZoneConfig:
        """Build from a stored options dict (with sensible defaults for missing keys)."""
        raw_profiles = d.get("profiles") or []
        profiles = [Profile.from_dict(p) for p in raw_profiles]
        return cls(
            zone_id=d.get("id") or str(uuid.uuid4())[:8],
            name=d["name"],
            climate_entity=d.get("climate_entity")
            or (d.get("climate_entities") or [""])[0],
            temperature_sensors=list(d.get("temperature_sensors", [])),
            schedule_entity=d.get("schedule_entity"),
            climate_entities=list(d.get("climate_entities") or []),
            window_sensors=list(d.get("window_sensors", [])),
            seuil_debut_chauffage=float(
                d.get("seuil_debut_chauffage", DEFAULT_SEUIL_DEBUT_CHAUFFAGE)
            ),
            seuil_fin_chauffage=float(d.get("seuil_fin_chauffage", DEFAULT_SEUIL_FIN_CHAUFFAGE)),
            seuil_debut_refroidissement=float(
                d.get("seuil_debut_refroidissement", DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT)
            ),
            seuil_fin_refroidissement=float(
                d.get("seuil_fin_refroidissement", DEFAULT_SEUIL_FIN_REFROIDISSEMENT)
            ),
            duree_stabilisation_min=int(
                d.get("duree_stabilisation_min", DEFAULT_DUREE_STABILISATION_MIN)
            ),
            duree_cooldown_min=int(d.get("duree_cooldown_min", DEFAULT_DUREE_COOLDOWN_MIN)),
            override_duree_min=int(d.get("override_duree_min", DEFAULT_OVERRIDE_DUREE_MIN)),
            override_until_reset=bool(d.get("override_until_reset", False)),
            aggressive_when_absent=bool(d.get("aggressive_when_absent", True)),
            aggressivity=str(d.get("aggressivity", DEFAULT_AGGRESSIVITY)),
            power=str(d.get("power", d.get("aggressivity", DEFAULT_POWER))),
            fan_intensity=str(d.get("fan_intensity", _legacy_to_fan(
                d.get("aggressivity", DEFAULT_AGGRESSIVITY)
            ))),
            profiles=profiles,
            pendulum_idle=bool(d.get("pendulum_idle", DEFAULT_PENDULUM_IDLE)),
            splits_config=dict(d.get("splits_config") or {}),
            target_temp=(
                float(d["target_temp"])
                if d.get("target_temp") is not None
                else None
            ),
        )


# === Zone (core logic) ===


class Zone:
    """Pure-logic state machine + algorithms for a single zone."""

    def __init__(self, config: ZoneConfig, state: ZoneRuntimeState | None = None) -> None:
        self.config = config
        self.state = state or ZoneRuntimeState()

    # --- public entry point ---

    def tick(self, inp: ZoneInputs) -> list[Command]:
        """Advance the state machine and emit commands for the current step."""
        # Snapshot pre-tick state — used at the end of the tick to detect cycle
        # start/end transitions and emit historical records. Captured here
        # because _transition() wipes cycle_started_ts before we can read it.
        prev = {
            "state": self.state.state,
            "cycle_started_ts": self.state.cycle_started_ts,
            "cycle_start_room_temp": self.state.cycle_start_room_temp,
            "cycle_start_profile_name": self.state.cycle_start_profile_name,
            "cycle_min_room_temp": self.state.cycle_min_room_temp,
            "cycle_regimes_seen": list(self.state.cycle_regimes_seen),
        }

        # Boot recovery: if the zone is freshly constructed (no transitions yet)
        # and the underlying clim is already in heat/cool, take over from it
        # instead of stopping it. Without this, a HA restart mid-cycle would
        # cause the next tick to see "IDLE + clim active" → emit turn_off.
        if (
            self.state.state == ZoneState.IDLE
            and self.state.last_state_transition_ts == 0.0
            and inp.clim_current_hvac_mode in (HVACMode.HEAT, HVACMode.COOL)
        ):
            self._transition(ZoneState.RUNNING, inp.now_ts)
            # Boot recovery — we don't know exactly when the cycle started, but
            # the clim's own last_changed (when it went to heat/cool) is a
            # much better estimate than "now" (which would lie about elapsed
            # time after every HA restart mid-cycle).
            self.state.cycle_started_ts = inp.clim_state_last_changed_ts or inp.now_ts

        if self.state.mode == ZoneMode.OFF:
            cmds = self._force_off(inp)
            self._update_cycle_snapshot(inp, prev)
            return cmds

        # Boost auto-expiry
        if self.state.boost_until_ts and inp.now_ts >= self.state.boost_until_ts:
            self.state.boost_until_ts = None

        # 1) Hard gates (window / schedule / override) override the regular flow
        gate_cmds = self._maybe_handle_hard_gates(inp)
        if gate_cmds is not None:
            self._update_cycle_snapshot(inp, prev)
            return gate_cmds

        # 2) Time-based transitions out of STABILIZING / COOLDOWN
        self._maybe_advance_timed_transitions(inp)

        # 3) Boost is a special active régime that ignores hysteresis
        if self.state.boost_until_ts and self.state.boost_until_ts > inp.now_ts:
            cmds = self._pilot_boost(inp)
            self._update_cycle_snapshot(inp, prev)
            return cmds

        # 4) Auto régime: decision (state machine) + pilot (commands)
        self._decide(inp)
        cmds = self._pilot(inp)
        self._update_cycle_snapshot(inp, prev)
        return cmds

    def _update_cycle_snapshot(self, inp: ZoneInputs, prev: dict[str, Any]) -> None:
        """Track in-progress cycle metrics and record completed cycles.

        Called once per tick after the state machine has settled. Detects:
        - cycle start (was idle, now active) → seed start_room_temp / profile
        - in-cycle update (still active) → update min_room_temp + regime trace
        - cycle end (was active, now idle) → append CycleRecord to history
        """
        was_active = prev["state"] in ACTIVE_CYCLE_STATES
        is_active = self.state.state in ACTIVE_CYCLE_STATES

        if is_active and inp.room_temperature is not None:
            cur_min = self.state.cycle_min_room_temp
            if cur_min is None or inp.room_temperature < cur_min:
                self.state.cycle_min_room_temp = inp.room_temperature
            if (
                self.state.regime
                and self.state.regime != Regime.NONE
                and self.state.regime not in self.state.cycle_regimes_seen
            ):
                self.state.cycle_regimes_seen.append(self.state.regime)

        if not was_active and is_active:
            self.state.cycle_start_room_temp = inp.room_temperature
            self.state.cycle_start_profile_name = (
                inp.active_profile.name if inp.active_profile else None
            )
            if self.state.cycle_min_room_temp is None and inp.room_temperature is not None:
                self.state.cycle_min_room_temp = inp.room_temperature

        if was_active and not is_active and prev["cycle_started_ts"] is not None:
            duration_s = inp.now_ts - prev["cycle_started_ts"]
            record = {
                "start_ts": prev["cycle_started_ts"],
                "end_ts": inp.now_ts,
                "duration_min": round(duration_s / 60, 1),
                "profile_at_start": prev["cycle_start_profile_name"],
                "profile_at_end": (
                    inp.active_profile.name if inp.active_profile else None
                ),
                "temp_start": prev["cycle_start_room_temp"],
                "temp_end": inp.room_temperature,
                "temp_min": prev["cycle_min_room_temp"],
                "regimes_seen": prev["cycle_regimes_seen"],
                "end_reason": self._end_reason_label(self.state.state),
            }
            self.state.completed_cycles.append(record)
            if len(self.state.completed_cycles) > CYCLE_HISTORY_MAX:
                self.state.completed_cycles = self.state.completed_cycles[
                    -CYCLE_HISTORY_MAX:
                ]
            self.state.cycle_start_room_temp = None
            self.state.cycle_start_profile_name = None
            self.state.cycle_min_room_temp = None
            self.state.cycle_regimes_seen = []

    @staticmethod
    def _end_reason_label(new_state: str) -> str:
        return {
            ZoneState.COOLDOWN: "stabilization_complete",
            ZoneState.IDLE: "natural_end",
            ZoneState.SCHEDULE_OFF: "schedule_ended",
            ZoneState.WINDOW_OPEN: "window_opened",
            ZoneState.MANUAL_OVERRIDE_TIMED: "user_override",
            ZoneState.MANUAL_OVERRIDE_FREE: "user_override",
        }.get(new_state, new_state)

    # --- mode / external triggers ---

    def set_mode(self, mode: str, now_ts: float) -> None:
        """Switch the zone between auto / off / boost."""
        if mode not in ZoneMode.ALL:
            return
        self.state.mode = mode
        if mode == ZoneMode.BOOST:
            self.state.boost_until_ts = now_ts + BOOST_DURATION_MIN * 60
        elif mode == ZoneMode.AUTO:
            self.state.boost_until_ts = None

    def trigger_boost(self, now_ts: float, direction: str | None = None) -> None:
        """Activate boost régime for BOOST_DURATION_MIN.

        If `direction` is given (cool/heat), it is forced — useful when the
        zone is idle and the temperature is in the dead band (between heat
        and cool thresholds): without a forced direction `_pilot_boost` has
        nothing to drive and silently no-ops, which used to look like a
        broken button.
        """
        self.state.boost_until_ts = now_ts + BOOST_DURATION_MIN * 60
        if direction in (HVACMode.COOL, HVACMode.HEAT):
            self.state.forced_direction = direction
            # If we were not running, transition into the active flow so the
            # next tick's _pilot_boost has a state.state to update commands on.
            if self.state.state in (
                ZoneState.IDLE,
                ZoneState.COOLDOWN,
                ZoneState.WINDOW_OPEN,
                ZoneState.SCHEDULE_OFF,
            ):
                self._transition(ZoneState.STARTING, now_ts)

    def force_start(self, direction: str, now_ts: float, *, supports: dict | None = None) -> None:
        """Force a cycle to start right now, in the given direction.

        Lets the user say 'start cooling now' while staying in auto mode —
        the integration runs a normal cycle, just without waiting for T°
        to cross the start threshold. The forced direction is cleared as
        soon as we leave STARTING/RUNNING (cycle completes or user
        intervenes).

        `supports` lets the caller pass capability flags so we no-op when
        the underlying clim doesn't support the requested direction.
        """
        if direction not in (HVACMode.COOL, HVACMode.HEAT):
            return
        if supports is not None:
            if direction == HVACMode.COOL and not supports.get("cool", True):
                return
            if direction == HVACMode.HEAT and not supports.get("heat", True):
                return
        # Only meaningful if we're idle / cooldown / window_open. From other
        # states (running, override, etc.), force_start is a no-op.
        if self.state.state not in (
            ZoneState.IDLE, ZoneState.COOLDOWN,
            ZoneState.WINDOW_OPEN, ZoneState.SCHEDULE_OFF,
        ):
            return
        self.state.forced_direction = direction
        self._transition(ZoneState.STARTING, now_ts)

    def reset_override(
        self,
        now_ts: float,
        clim_current_hvac_mode: str = "off",
        clim_state_last_changed_ts: float | None = None,
    ) -> None:
        """Court-circuit any ongoing manual override.

        If the clim is actively heating/cooling at the moment the user hits
        Resume auto, hand the reins to RUNNING so auto can continue the cycle
        instead of turning the unit off. Without that, the next tick saw IDLE
        + clim active and emitted turn_off — i.e. Resume auto killed an
        in-progress cycle (reported on étage, 2026-05-30).

        `clim_state_last_changed_ts` is the clim's own last_changed, used to
        anchor cycle_started_ts at the real moment the clim went active rather
        than at the click time.
        """
        self.state.override_until_ts = None
        if self.state.state in (ZoneState.MANUAL_OVERRIDE_TIMED, ZoneState.MANUAL_OVERRIDE_FREE):
            if clim_current_hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
                self._transition(ZoneState.RUNNING, now_ts)
                self.state.cycle_started_ts = clim_state_last_changed_ts or now_ts
            else:
                self._transition(ZoneState.IDLE, now_ts)

    def on_external_override(self, now_ts: float, schedule_is_on: bool) -> None:
        """A state_changed with a non-tracked context was detected on our clim."""
        if schedule_is_on:
            self._transition(ZoneState.MANUAL_OVERRIDE_TIMED, now_ts)
            if self.config.override_until_reset:
                # Boulot : tient jusqu'au reset du matin / extinction du soir.
                # override_until_ts=None ⇒ pas d'expiration par timer.
                self.state.override_until_ts = None
            else:
                self.state.override_until_ts = now_ts + self.config.override_duree_min * 60
        else:
            self._transition(ZoneState.MANUAL_OVERRIDE_FREE, now_ts)
            self.state.override_until_ts = None

    def daily_reset(self, now_ts: float, default_power: str | None = None) -> None:
        """Remise à zéro quotidienne d'une zone (déclenchée au désarmement).

        Repasse en AUTO, purge tout override/boost de la veille et restaure le
        profil par défaut (puissance Normal). Les splits seront réévalués au tick
        suivant à partir d'IDLE — aucune zone ne reste éteinte par inadvertance.
        """
        self.state.mode = ZoneMode.AUTO
        self.state.boost_until_ts = None
        self.state.override_until_ts = None
        self.state.forced_direction = None
        # Nouveau jour = ardoise propre : on oublie la direction pendule verrouillée
        # la veille (sinon une zone passée en chauffage hier reste bloquée en heat
        # même quand il faut refroidir aujourd'hui).
        self.state.active_direction = None
        if default_power is not None:
            self.config.power = default_power
            for p in self.config.profiles:
                p.power = default_power
        self._transition(ZoneState.IDLE, now_ts)

    # --- internal helpers ---

    def _transition(self, new_state: str, now_ts: float) -> None:
        if new_state == self.state.state:
            return
        _LOGGER.debug(
            "Zone %s: transition %s → %s", self.config.zone_id, self.state.state, new_state
        )
        self.state.state = new_state
        self.state.last_state_transition_ts = now_ts
        # A forced cycle (force_start) ends as soon as we leave the active states
        if new_state not in (ZoneState.STARTING, ZoneState.RUNNING):
            self.state.forced_direction = None
        # Cycle start timestamp — set on entry to STARTING, kept across
        # RUNNING/STABILIZING, cleared on any other transition. Override /
        # window / schedule_off interrupt the cycle; if we resume later it's
        # a new cycle (and a new starting point for the UI's elapsed time).
        if new_state == ZoneState.STARTING:
            self.state.cycle_started_ts = now_ts
        elif new_state not in (ZoneState.RUNNING, ZoneState.STABILIZING):
            self.state.cycle_started_ts = None

    def _force_off(self, inp: ZoneInputs) -> list[Command]:
        """Mode=OFF : ensure the clim is off, do nothing else."""
        self._transition(ZoneState.IDLE, inp.now_ts)
        self.state.regime = Regime.NONE
        self.state.last_hvac_sent = HVACMode.OFF  # intention = éteint
        if inp.clim_current_hvac_mode != HVACMode.OFF:
            return [self._cmd_turn_off()]
        return []

    def _maybe_handle_hard_gates(self, inp: ZoneInputs) -> list[Command] | None:
        """Return commands if a hard gate (window/schedule/override) overrides flow."""
        # Window open
        if inp.any_window_open:
            self.state.last_hvac_sent = HVACMode.OFF  # intention = éteint
            if self.state.state != ZoneState.WINDOW_OPEN:
                self._transition(ZoneState.WINDOW_OPEN, inp.now_ts)
                self.state.regime = Regime.NONE
                if inp.clim_current_hvac_mode != HVACMode.OFF:
                    return [self._cmd_turn_off()]
            return []

        # Schedule off
        if not inp.schedule_is_on:
            if self.state.state == ZoneState.MANUAL_OVERRIDE_FREE:
                # User is running clim manually with schedule off — leave it alone
                return []
            self.state.last_hvac_sent = HVACMode.OFF  # intention = éteint
            if self.state.state != ZoneState.SCHEDULE_OFF:
                self._transition(ZoneState.SCHEDULE_OFF, inp.now_ts)
                self.state.regime = Regime.NONE
                if inp.clim_current_hvac_mode != HVACMode.OFF:
                    return [self._cmd_turn_off()]
            return []

        # Schedule just turned on — leave override states alone, option A handled below
        if self.state.state == ZoneState.SCHEDULE_OFF:
            # Schedule just opened — return to IDLE for fresh decision (option A)
            self._transition(ZoneState.IDLE, inp.now_ts)
            # fall through to regular flow

        # Manual override
        if self.state.state == ZoneState.MANUAL_OVERRIDE_TIMED:
            if (
                self.state.override_until_ts is not None
                and inp.now_ts >= self.state.override_until_ts
            ):
                self.state.override_until_ts = None
                self._transition(ZoneState.IDLE, inp.now_ts)
                # fall through to regular flow
            else:
                return []
        elif self.state.state == ZoneState.MANUAL_OVERRIDE_FREE:
            # Schedule is on now (we're past the schedule-off check) → option A: take over
            self.state.override_until_ts = None
            self._transition(ZoneState.IDLE, inp.now_ts)
            # fall through

        # If we were in WINDOW_OPEN and windows are now closed, also transition out
        if self.state.state == ZoneState.WINDOW_OPEN:
            self._transition(ZoneState.IDLE, inp.now_ts)

        return None  # fall through to regular flow

    def _maybe_advance_timed_transitions(self, inp: ZoneInputs) -> None:
        """STABILIZING → COOLDOWN → IDLE based on elapsed time.

        En mode pendule, STABILIZING/COOLDOWN ne sont pas utilisés comme
        transitions automatiques (le split reste actif — pas de turn_off).
        Les états sont conservés dans l'enum pour la rétro-compat from_dict.
        """
        if self.config.pendulum_idle:
            # Pendule : si on se retrouve dans STABILIZING/COOLDOWN (état hérité
            # d'un démarrage pré-1.4 ou d'une bascule du flag), on revient
            # directement en RUNNING pour reprendre le cycle continu.
            if self.state.state in (ZoneState.STABILIZING, ZoneState.COOLDOWN):
                self._transition(ZoneState.RUNNING, inp.now_ts)
            return
        if self.state.state == ZoneState.STABILIZING:
            elapsed = inp.now_ts - self.state.last_state_transition_ts
            if elapsed >= self.config.duree_stabilisation_min * 60:
                self._transition(ZoneState.COOLDOWN, inp.now_ts)
        if self.state.state == ZoneState.COOLDOWN:
            elapsed = inp.now_ts - self.state.last_state_transition_ts
            if elapsed >= self.config.duree_cooldown_min * 60:
                self._transition(ZoneState.IDLE, inp.now_ts)

    def _active(self, inp: ZoneInputs) -> Profile:
        """Return the active driver profile for this tick.

        Falls back to the zone's default profile (synthesised in
        ZoneConfig.__post_init__ from legacy fields) when the coordinator has
        not resolved one — primarily the test path that builds ZoneInputs
        directly without going through the coordinator's cascade logic.

        Si la zone a une `target_temp` (thermostat), on dérive les 4 seuils du
        profil à partir d'elle pour que toute la logique (décision, pendule,
        relâchement, héritage des splits) s'appuie sur la cible unique.
        """
        prof = inp.active_profile or self.config.profiles[0]
        return _apply_target_temp(prof, self.config.target_temp)

    def _decide(self, inp: ZoneInputs) -> None:
        """Pure decision logic (IDLE -> STARTING) based on room sensor + thresholds."""
        if inp.room_temperature is None:
            return
        p = self._active(inp)

        if self.config.pendulum_idle:
            self._decide_pendulum(inp, p)
            return

        if self.state.state == ZoneState.IDLE:
            if inp.supports_cool and inp.room_temperature > p.seuil_debut_refroidissement:
                self._transition(ZoneState.STARTING, inp.now_ts)
            elif inp.supports_heat and inp.room_temperature < p.seuil_debut_chauffage:
                self._transition(ZoneState.STARTING, inp.now_ts)
        elif self.state.state == ZoneState.RUNNING:
            in_heat = inp.clim_current_hvac_mode == HVACMode.HEAT
            in_cool = inp.clim_current_hvac_mode == HVACMode.COOL
            if in_heat and inp.room_temperature >= p.seuil_fin_chauffage:
                self._transition(ZoneState.STABILIZING, inp.now_ts)
            elif in_cool and inp.room_temperature <= p.seuil_fin_refroidissement:
                self._transition(ZoneState.STABILIZING, inp.now_ts)

    def _decide_pendulum(self, inp: ZoneInputs, p: Profile) -> None:
        """Logique de décision en mode pendule continu.

        La direction est verrouillée (active_direction) dès le premier
        franchissement de seuil. Elle n'est libérée que si la T° pièce
        franchit le seuil de DÉBUT OPPOSÉ — ce qui est très rare en
        pratique (il faudrait qu'un bureau refroidisse jusqu'au seuil de
        chauffage en plein été). Entre ces deux extrêmes, le split alterne
        attaque / relâchement selon que la cible est atteinte ou non.
        """
        rt = inp.room_temperature
        if rt is None:
            return
        ad = self.state.active_direction

        # Déverrouillage : franchissement du seuil de début OPPOSÉ
        if ad == HVACMode.COOL and inp.supports_heat and rt < p.seuil_debut_chauffage:
            self.state.active_direction = None
            ad = None
        elif ad == HVACMode.HEAT and inp.supports_cool and rt > p.seuil_debut_refroidissement:
            self.state.active_direction = None
            ad = None

        # Engagement d'une direction si aucune n'est verrouillée
        if ad is None:
            if inp.supports_cool and rt > p.seuil_debut_refroidissement:
                self.state.active_direction = HVACMode.COOL
            elif inp.supports_heat and rt < p.seuil_debut_chauffage:
                self.state.active_direction = HVACMode.HEAT

        # Transitions d'état : IDLE → STARTING dès qu'on a une direction
        if self.state.state == ZoneState.IDLE and self.state.active_direction is not None:
            self._transition(ZoneState.STARTING, inp.now_ts)
        # RUNNING : si on n'a pas de active_direction (restart depuis un état
        # antérieur), on la déduit du mode courant du split.
        elif self.state.state == ZoneState.RUNNING and self.state.active_direction is None:
            if inp.clim_current_hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
                self.state.active_direction = inp.clim_current_hvac_mode
        # En RUNNING : PAS de transition vers STABILIZING (géré dans _maybe_advance)

    def _pilot(self, inp: ZoneInputs) -> list[Command]:
        """Translate the current state into commands."""
        if self.state.state in (ZoneState.IDLE, ZoneState.COOLDOWN):
            self.state.regime = Regime.NONE
            # Pendule : ne jamais éteindre quand le système est actif.
            # La clim reste allumée ; la prochaine décision réengagera une
            # direction dès que la T° franchira un seuil.
            if self.config.pendulum_idle:
                return []
            self.state.last_hvac_sent = HVACMode.OFF  # intention = éteint
            if inp.clim_current_hvac_mode != HVACMode.OFF:
                return [self._cmd_turn_off()]
            return []

        if self.state.state == ZoneState.STARTING:
            # Premier tick en mode actif → ATTAQUE pleine
            return self._emit_active(inp, Regime.ATTAQUE, force_hvac=True)

        if self.state.state == ZoneState.RUNNING:
            if self.config.pendulum_idle:
                return self._emit_active_pendulum(inp)
            regime = self._compute_regime(inp)
            return self._emit_active(inp, regime, force_hvac=False)

        if self.state.state == ZoneState.STABILIZING:
            return self._emit_active(inp, Regime.STABILISATION, force_hvac=False)

        return []

    def _pilot_boost(self, inp: ZoneInputs) -> list[Command]:
        """Boost régime — strong, ignores hysteresis, fixed 15 min."""
        if inp.room_temperature is None:
            return []

        # Boost only makes sense if we know which direction
        target_mode = self._desired_hvac_mode(inp)
        if target_mode is None:
            return []

        if self.state.state != ZoneState.RUNNING:
            self._transition(ZoneState.RUNNING, inp.now_ts)
        self.state.regime = Regime.BOOST
        self.state.last_hvac_sent = target_mode  # intention, même si no-op

        cmds: list[Command] = []
        if inp.clim_current_hvac_mode != target_mode:
            cmds.append(self._cmd_set_hvac_mode(target_mode))
        setpoint = self._setpoint_for_offset(inp, BOOST_OFFSET, target_mode)
        if setpoint is not None:
            if self._setpoint_should_send(setpoint, inp):
                cmds.append(self._cmd_set_temperature(setpoint))
            self.state.last_setpoint_sent = setpoint  # intention, même si no-op
        if inp.supports_fan_mode:
            self.state.last_fan_sent = BOOST_FAN_MODE  # intention, même si no-op
            if inp.clim_current_fan_mode != BOOST_FAN_MODE:
                cmds.append(self._cmd_set_fan_mode(BOOST_FAN_MODE))
        if inp.supports_windnice and inp.clim_current_swing_mode != "swing":
            cmds.append(self._cmd_set_swing_mode("swing"))
        if cmds:
            self.state.last_command_ts = inp.now_ts
        return cmds

    # --- régime + setpoint maths ---

    def _compute_regime(self, inp: ZoneInputs) -> str:
        # Architecture D: during RUNNING the offset stays constant (driven by
        # the Power knob). Daikin's inverter handles compressor modulation as
        # internal temp approaches setpoint — we don't ramp it down ourselves,
        # because doing so caused a visible plateau on the descent (the
        # CROISIERE→APPROCHE handoff used to drop the offset from 7°C to 3°C
        # and the unit slowed accordingly).
        if self._current_active_mode(inp) is None or inp.room_temperature is None:
            return Regime.NONE
        return Regime.ATTAQUE

    def _current_active_mode(self, inp: ZoneInputs) -> str | None:
        """What hvac_mode we should be running in this active state."""
        if self.state.state == ZoneState.STARTING:
            return self._desired_hvac_mode(inp)
        # In RUNNING/STABILIZING, keep what's already there (or recompute if off)
        if inp.clim_current_hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
            return inp.clim_current_hvac_mode
        return self._desired_hvac_mode(inp)

    def _desired_hvac_mode(self, inp: ZoneInputs) -> str | None:
        # User explicitly forced a direction (force_start) — honour it
        # (capability already checked at force_start time).
        if self.state.forced_direction in (HVACMode.COOL, HVACMode.HEAT):
            return self.state.forced_direction
        # Pendule : direction verrouillée → la respecter même si la T° est
        # revenue dans la bande morte (cas release — on reste actif dans ce sens).
        if (
            self.config.pendulum_idle
            and self.state.active_direction in (HVACMode.COOL, HVACMode.HEAT)
        ):
            return self.state.active_direction
        if inp.room_temperature is None:
            return None
        p = self._active(inp)
        if inp.supports_cool and inp.room_temperature > p.seuil_debut_refroidissement:
            return HVACMode.COOL
        if inp.supports_heat and inp.room_temperature < p.seuil_debut_chauffage:
            return HVACMode.HEAT
        return None

    def _emit_active(self, inp: ZoneInputs, regime: str, *, force_hvac: bool) -> list[Command]:
        """Emit commands for an active state (STARTING / RUNNING / STABILIZING)."""
        self.state.regime = regime
        target_mode = self._current_active_mode(inp)
        if target_mode is None:
            # Could not decide — be safe and do nothing this tick
            return []
        # Intention de direction : mémorisée même si aucune commande n'est
        # réémise (split déjà dans le bon mode). Sert à distinguer un écho du
        # polling d'une vraie prise en main.
        self.state.last_hvac_sent = target_mode

        cmds: list[Command] = []
        p = self._active(inp)
        power_profile = POWER_PROFILES.get(p.power, POWER_PROFILES[DEFAULT_POWER])
        fan_profile = FAN_PROFILES.get(p.fan_intensity, FAN_PROFILES[DEFAULT_FAN_INTENSITY])

        # HVAC mode (toujours groupé — direction commune à tous les splits)
        if force_hvac or inp.clim_current_hvac_mode != target_mode:
            cmds.append(self._cmd_set_hvac_mode(target_mode))

        # Consigne — par split si splits_config non vide, sinon groupée
        offset = _offset_for_regime(regime, power_profile)
        if self.config.splits_config:
            cmds.extend(self._emit_per_split_setpoints(inp, regime, target_mode, p))
        else:
            setpoint = self._setpoint_for_offset(inp, offset, target_mode)
            if setpoint is not None:
                if self._setpoint_should_send(setpoint, inp):
                    cmds.append(self._cmd_set_temperature(setpoint))
                self.state.last_setpoint_sent = setpoint  # intention, même si no-op

        # Fan — driven by the FAN profile, and only if the clim has fan_modes at all
        if inp.supports_fan_mode:
            target_fan = _fan_for_regime(regime, fan_profile)
            if target_fan:
                self.state.last_fan_sent = target_fan  # intention, même si no-op
                if inp.clim_current_fan_mode != target_fan:
                    cmds.append(self._cmd_set_fan_mode(target_fan))

        # Swing global (windnice) — seulement si pas de swing configuré par split
        if inp.supports_windnice and not self.config.splits_config \
                and inp.clim_current_swing_mode != DEFAULT_SWING_MODE:
            cmds.append(self._cmd_set_swing_mode(DEFAULT_SWING_MODE))

        if cmds:
            self.state.last_command_ts = inp.now_ts
            if force_hvac or any(c.service == "set_hvac_mode" for c in cmds):
                # STARTING just emitted hvac on → now RUNNING for the next tick
                if self.state.state == ZoneState.STARTING:
                    self._transition(ZoneState.RUNNING, inp.now_ts)
        return cmds

    def _anchor_internal_temperature(
        self, inp: ZoneInputs, target_mode: str
    ) -> float | None:
        """Sonde interne servant d'ancre au pendule.

        Mono-split → sa sonde. Multi-splits → la plus froide en cool (pour que
        chaque split reçoive une consigne sous SA propre sonde et refroidisse
        réellement) / la plus chaude en heat. Garantit qu'aucun split n'est
        neutralisé par une consigne partagée mal placée vis-à-vis de sa sonde.
        """
        temps = list(inp.clim_internal_temperatures)
        if not temps and inp.clim_internal_temperature is not None:
            temps = [inp.clim_internal_temperature]
        if not temps:
            return None
        return min(temps) if target_mode == HVACMode.COOL else max(temps)

    def _setpoint_for_offset(
        self, inp: ZoneInputs, offset: float, target_mode: str
    ) -> float | None:
        """Compute consigne envoyée = T°_interne ± offset, clamped to clim limits."""
        anchor = self._anchor_internal_temperature(inp, target_mode)
        if anchor is None:
            return None
        signed = offset if target_mode == HVACMode.HEAT else -offset
        raw = anchor + signed
        # Arrondi au pas de la clim (Daikin 0.5, Hitachi 1.0) puis clamp.
        step = inp.clim_setpoint_step or DEFAULT_SETPOINT_STEP
        rounded = round(raw / step) * step
        return max(CLIM_MIN_SETPOINT, min(CLIM_MAX_SETPOINT, rounded))

    def _setpoint_release_offset(
        self, inp: ZoneInputs, offset: float, target_mode: str
    ) -> float | None:
        """Consigne de relâchement pendule (signe INVERSÉ par rapport à l'attaque).

        En cool : anchor + offset → AU-DESSUS de la sonde → le compresseur
        ralentit (sa cible est déjà atteinte) mais le split reste allumé.
        En heat : anchor - offset → EN-DESSOUS de la sonde → même principe.
        """
        anchor = self._anchor_internal_temperature(inp, target_mode)
        if anchor is None:
            return None
        # Signe inversé : cool = positif, heat = négatif
        signed = -offset if target_mode == HVACMode.HEAT else offset
        raw = anchor + signed
        step = inp.clim_setpoint_step or DEFAULT_SETPOINT_STEP
        rounded = round(raw / step) * step
        return max(CLIM_MIN_SETPOINT, min(CLIM_MAX_SETPOINT, rounded))

    def _setpoint_for_split_anchor(
        self, anchor: float | None, offset: float, target_mode: str, inp: ZoneInputs
    ) -> float | None:
        """Consigne d'attaque pour un split avec son ancre propre.

        Si anchor est None, repli sur l'ancre groupe (_setpoint_for_offset).
        """
        if anchor is None:
            return self._setpoint_for_offset(inp, offset, target_mode)
        signed = offset if target_mode == HVACMode.HEAT else -offset
        raw = anchor + signed
        step = inp.clim_setpoint_step or DEFAULT_SETPOINT_STEP
        rounded = round(raw / step) * step
        return max(CLIM_MIN_SETPOINT, min(CLIM_MAX_SETPOINT, rounded))

    def _setpoint_release_for_split(
        self, anchor: float | None, offset: float, target_mode: str, inp: ZoneInputs
    ) -> float | None:
        """Consigne de relâchement pendule pour un split avec son ancre propre."""
        if anchor is None:
            return self._setpoint_release_offset(inp, offset, target_mode)
        signed = -offset if target_mode == HVACMode.HEAT else offset
        raw = anchor + signed
        step = inp.clim_setpoint_step or DEFAULT_SETPOINT_STEP
        rounded = round(raw / step) * step
        return max(CLIM_MIN_SETPOINT, min(CLIM_MAX_SETPOINT, rounded))

    # --- Pendule continu (pendulum_idle=True) ---

    def _is_pendulum_release(
        self, inp: ZoneInputs, p: Profile, target_mode: str
    ) -> bool:
        """True si la T° pièce a atteint la cible → phase de relâchement pendule.

        Cool : T° pièce ≤ seuil_fin_refroidissement → cible atteinte → release.
        Heat : T° pièce ≥ seuil_fin_chauffage → cible atteinte → release.
        """
        rt = inp.room_temperature
        if rt is None:
            return False
        if target_mode == HVACMode.COOL:
            return rt <= p.seuil_fin_refroidissement
        if target_mode == HVACMode.HEAT:
            return rt >= p.seuil_fin_chauffage
        return False

    def _emit_active_pendulum(self, inp: ZoneInputs) -> list[Command]:
        """Pilotage en mode pendule continu (RUNNING).

        Attaque si la pièce est encore au-delà de la cible dans le sens actif.
        Relâchement si la cible est atteinte : le split reste allumé mais la
        consigne inversée fait ralentir le compresseur au lieu de l'éteindre.
        Ce cycle «pendule» évite le retard thermique du turn_off/redémarrage.
        """
        p = self._active(inp)
        target_mode = self.state.active_direction or self._current_active_mode(inp)
        if target_mode is None:
            return []
        self.state.last_hvac_sent = target_mode  # intention de direction (pendule)

        # Par split : chaque split décide attaque/relâchement selon SA cible
        # propre (§3) — un split réglé sur 24°C relâche avant un split réglé
        # sur 22°C dans la même pièce.
        if self.config.splits_config:
            return self._emit_pendulum_per_split(inp, target_mode, p)

        if self._is_pendulum_release(inp, p, target_mode):
            self.state.regime = Regime.STABILISATION  # affiché comme «maintien» dans l'UI
            return self._emit_release(inp, target_mode, p)
        else:
            self.state.regime = Regime.ATTAQUE
            return self._emit_active(inp, Regime.ATTAQUE, force_hvac=False)

    def _split_effective_target(
        self, split_cfg: dict, p: Profile, target_mode: str
    ) -> float | None:
        """Cible de confort effective d'un split : la sienne ou celle héritée
        de la zone (seuil_fin du sens actif)."""
        t = split_cfg.get("target")
        if t is not None:
            try:
                return float(t)
            except (TypeError, ValueError):
                pass
        if target_mode == HVACMode.COOL:
            return p.seuil_fin_refroidissement
        if target_mode == HVACMode.HEAT:
            return p.seuil_fin_chauffage
        return None

    @staticmethod
    def _room_reached_target(
        room_temperature: float | None, target: float | None, target_mode: str
    ) -> bool:
        """True si la pièce a atteint la cible (→ relâchement) dans le sens actif."""
        if room_temperature is None or target is None:
            return False
        if target_mode == HVACMode.COOL:
            return room_temperature <= target
        if target_mode == HVACMode.HEAT:
            return room_temperature >= target
        return False

    def _emit_pendulum_per_split(
        self, inp: ZoneInputs, target_mode: str, p: Profile
    ) -> list[Command]:
        """Pendule continu avec décision attaque/relâchement INDÉPENDANTE par split.

        Chaque split compare la T° pièce à SA cible effective : tant qu'elle
        n'est pas atteinte → attaque (consigne sous la sonde en cool) ; une fois
        atteinte → relâchement (consigne au-dessus de la sonde → idle mais
        allumé). Le set_hvac_mode reste groupé (direction commune). Le régime
        zone affiché = ATTAQUE si au moins un split attaque, sinon maintien.
        """
        cmds: list[Command] = []
        if inp.clim_current_hvac_mode != target_mode:
            cmds.append(self._cmd_set_hvac_mode(target_mode))

        any_attack = False
        for entity_id in self._target_entities:
            split_cfg = self.config.splits_config.get(entity_id, {})
            split_power = split_cfg.get("power") or p.power
            spp = POWER_PROFILES.get(split_power, POWER_PROFILES[DEFAULT_POWER])
            anchor = inp.clim_internal_by_entity.get(entity_id)
            target = self._split_effective_target(split_cfg, p, target_mode)

            if self._room_reached_target(inp.room_temperature, target, target_mode):
                offset = spp.get("release", 3.0)
                sp = self._setpoint_release_for_split(anchor, offset, target_mode, inp)
            else:
                any_attack = True
                offset = _offset_for_regime(Regime.ATTAQUE, spp)
                sp = self._setpoint_for_split_anchor(anchor, offset, target_mode, inp)

            if sp is not None:
                if self._setpoint_should_send(sp, inp):
                    cmds.append(Command(
                        "climate", "set_temperature",
                        {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: sp},
                    ))
                self.state.last_setpoint_sent = sp  # intention (dernier split), même si no-op

            swing = split_cfg.get("swing")
            if swing:
                cmds.append(Command(
                    "climate", "set_swing_mode",
                    {ATTR_ENTITY_ID: entity_id, ATTR_SWING_MODE: swing},
                ))

        self.state.regime = Regime.ATTAQUE if any_attack else Regime.STABILISATION
        if cmds:
            self.state.last_command_ts = inp.now_ts
        return cmds

    def _emit_release(
        self, inp: ZoneInputs, target_mode: str, p: Profile
    ) -> list[Command]:
        """Émet la consigne de relâchement (consigne inversée = split idle actif).

        Par split si splits_config non vide, sinon groupée. Le mode HVAC est
        maintenu (pas de turn_off) mais la consigne est au-dessus de la sonde
        en cool / en-dessous en heat pour que l'inverter ralentisse.
        """
        power_profile = POWER_PROFILES.get(p.power, POWER_PROFILES[DEFAULT_POWER])
        cmds: list[Command] = []
        self.state.last_hvac_sent = target_mode  # intention de direction (relâchement)

        # Mode HVAC inchangé (le split reste dans son sens)
        if inp.clim_current_hvac_mode != target_mode:
            cmds.append(self._cmd_set_hvac_mode(target_mode))

        if self.config.splits_config:
            # Consignes par split avec offset release propre à chaque puissance
            for entity_id in self._target_entities:
                split_cfg = self.config.splits_config.get(entity_id, {})
                split_power = split_cfg.get("power") or p.power
                spp = POWER_PROFILES.get(split_power, POWER_PROFILES[DEFAULT_POWER])
                release_offset = spp.get("release", power_profile.get("release", 3.0))
                anchor = inp.clim_internal_by_entity.get(entity_id)
                sp = self._setpoint_release_for_split(anchor, release_offset, target_mode, inp)
                if sp is not None:
                    if self._setpoint_should_send(sp, inp):
                        cmds.append(Command(
                            "climate", "set_temperature",
                            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: sp},
                        ))
                    self.state.last_setpoint_sent = sp  # intention, même si no-op
                split_swing = split_cfg.get("swing")
                if split_swing:
                    cmds.append(Command(
                        "climate", "set_swing_mode",
                        {ATTR_ENTITY_ID: entity_id, ATTR_SWING_MODE: split_swing},
                    ))
        else:
            release_offset = power_profile.get("release", 3.0)
            setpoint = self._setpoint_release_offset(inp, release_offset, target_mode)
            if setpoint is not None:
                if self._setpoint_should_send(setpoint, inp):
                    cmds.append(self._cmd_set_temperature(setpoint))
                self.state.last_setpoint_sent = setpoint  # intention, même si no-op

        if cmds:
            self.state.last_command_ts = inp.now_ts
        return cmds

    # --- Par split (splits_config non vide) ---

    def _emit_per_split_setpoints(
        self, inp: ZoneInputs, regime: str, target_mode: str, p: Profile
    ) -> list[Command]:
        """Consignes individuelles par split quand splits_config est renseigné.

        Chaque split utilise sa propre sonde interne comme ancre (ou repli sur
        la moyenne groupe si indisponible), sa propre puissance (ou héritage de
        la zone), et envoie son propre swing si configuré.
        Le set_hvac_mode reste groupé (direction commune) et est géré par
        l'appelant (_emit_active). On n'émet ici QUE les températures et swings.
        """
        power_profile_zone = POWER_PROFILES.get(p.power, POWER_PROFILES[DEFAULT_POWER])
        cmds: list[Command] = []
        for entity_id in self._target_entities:
            split_cfg = self.config.splits_config.get(entity_id, {})
            # Puissance propre au split ou héritage zone
            split_power = split_cfg.get("power") or p.power
            split_pp = POWER_PROFILES.get(split_power, power_profile_zone)
            offset = _offset_for_regime(regime, split_pp)
            # Ancre interne propre au split (fournie par le coordinator)
            anchor = inp.clim_internal_by_entity.get(entity_id)
            sp = self._setpoint_for_split_anchor(anchor, offset, target_mode, inp)
            if sp is not None and self._setpoint_should_send(sp, inp):
                cmds.append(Command(
                    "climate", "set_temperature",
                    {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: sp},
                ))
                self.state.last_setpoint_sent = sp
            # Swing propre au split (remplace le windnice global)
            swing = split_cfg.get("swing")
            if swing:
                cmds.append(Command(
                    "climate", "set_swing_mode",
                    {ATTR_ENTITY_ID: entity_id, ATTR_SWING_MODE: swing},
                ))
        return cmds

    def _setpoint_should_send(self, setpoint: float, inp: ZoneInputs) -> bool:
        """Rate-limit: don't re-emit setpoint if too close to current or too soon."""
        if (
            inp.clim_current_setpoint is not None
            and abs(setpoint - inp.clim_current_setpoint) < SETPOINT_NOOP_DELTA
        ):
            return False
        if (
            self.state.last_command_ts
            and (inp.now_ts - self.state.last_command_ts) < RATE_LIMIT_SECONDS
            and self.state.last_setpoint_sent is not None
            and abs(setpoint - self.state.last_setpoint_sent) < SETPOINT_NOOP_DELTA
        ):
            return False
        return True

    # --- command factory ---

    @property
    def _target_entities(self) -> list[str]:
        """Tous les splits de la zone (HA accepte une liste pour entity_id)."""
        return self.config.climate_entities or [self.config.climate_entity]

    def _cmd_turn_off(self) -> Command:
        return Command(
            domain="climate",
            service="turn_off",
            data={ATTR_ENTITY_ID: self._target_entities},
        )

    def _cmd_set_hvac_mode(self, mode: str) -> Command:
        return Command(
            domain="climate",
            service="set_hvac_mode",
            data={ATTR_ENTITY_ID: self._target_entities, ATTR_HVAC_MODE: mode},
        )

    def _cmd_set_temperature(self, temp: float) -> Command:
        return Command(
            domain="climate",
            service="set_temperature",
            data={ATTR_ENTITY_ID: self._target_entities, ATTR_TEMPERATURE: temp},
        )

    def _cmd_set_fan_mode(self, mode: str) -> Command:
        return Command(
            domain="climate",
            service="set_fan_mode",
            data={ATTR_ENTITY_ID: self._target_entities, ATTR_FAN_MODE: mode},
        )

    def _cmd_set_swing_mode(self, mode: str) -> Command:
        return Command(
            domain="climate",
            service="set_swing_mode",
            data={ATTR_ENTITY_ID: self._target_entities, ATTR_SWING_MODE: mode},
        )


def _apply_target_temp(profile: Profile, target_temp: float | None) -> Profile:
    """Dérive un profil dont les 4 seuils découlent d'une cible unique.

    target_temp=None → profil inchangé (comportement historique 4 seuils).
    Sinon : stabilisation/relâchement à `target_temp`, démarrage froid à
    target+bande, démarrage chaud à target-bande. Le reste du profil
    (puissance, ventilation, nom) est conservé tel quel.
    """
    if target_temp is None:
        return profile
    d = DEFAULT_TARGET_DEADBAND
    return replace(
        profile,
        seuil_fin_refroidissement=target_temp,
        seuil_fin_chauffage=target_temp,
        seuil_debut_refroidissement=target_temp + d,
        seuil_debut_chauffage=target_temp - d,
    )


def _offset_for_regime(regime: str, power_profile: dict) -> float:
    if regime == Regime.ATTAQUE:
        return power_profile["attaque"]
    if regime == Regime.STABILISATION:
        return power_profile.get("stabilisation", 0.0)
    if regime == Regime.BOOST:
        return BOOST_OFFSET
    return 0.0


def _fan_for_regime(regime: str, fan_profile: dict) -> str | None:
    if regime == Regime.ATTAQUE:
        return fan_profile.get("attaque")
    if regime == Regime.STABILISATION:
        # Ventilation douce pendant le maintien — pilotée par le profil (Hitachi
        # n'a pas de "quiet" ; "low" tient ce rôle).
        return fan_profile.get("stabilisation", fan_profile.get("attaque"))
    if regime == Regime.BOOST:
        return BOOST_FAN_MODE
    return None


def _legacy_to_fan(legacy: str) -> str:
    """Legacy 'aggressivity' → fan_intensity. The old 'agressif' meant 'fort'
    for fan; doux/normal carry over."""
    return "fort" if legacy == "agressif" else legacy


def utc_now_ts() -> float:
    """Monotonic time isn't right for cross-tick durations across restarts.
    Use wall time so durations survive a HA reload."""
    return time.time()
