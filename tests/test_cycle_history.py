"""Tests for cycle_history — persistent log of completed cooling/heating
sessions exposed to the Lovelace card §5 'Sessions récentes'."""

from __future__ import annotations

from custom_components.climate_manager.const import Regime, ZoneState
from custom_components.climate_manager.zone import (
    CYCLE_HISTORY_MAX,
    Profile,
    Zone,
    ZoneConfig,
    ZoneInputs,
)

HVAC_OFF = "off"
HVAC_COOL = "cool"


def _cfg(**overrides) -> ZoneConfig:
    base = dict(
        zone_id="z1",
        name="Z1",
        climate_entity="climate.z1",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
        seuil_debut_refroidissement=25.0,
        seuil_fin_refroidissement=23.0,
        duree_stabilisation_min=60,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def _inp(
    now_ts: float,
    *,
    room: float,
    hvac: str = HVAC_OFF,
    profile: Profile | None = None,
    schedule_on: bool = True,
    window_open: bool = False,
) -> ZoneInputs:
    return ZoneInputs(
        now_ts=now_ts,
        room_temperature=room,
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=hvac,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=schedule_on,
        any_window_open=window_open,
        house_is_absent=False,
        active_profile=profile,
    )


def _profile(name="Journée présent") -> Profile:
    return Profile(
        name=name, seuil_debut_refroidissement=25.0, seuil_fin_refroidissement=23.0
    )


def test_completed_cycle_recorded_when_schedule_ends() -> None:
    """End-to-end: drive a real cycle through ticks, terminate via schedule
    going off → one record appended with the right metadata."""
    p = _profile()
    z = Zone(_cfg())

    # Start cycle: room above threshold → IDLE → STARTING
    z.tick(_inp(1_000.0, room=26.0, profile=p))
    assert z.state.cycle_started_ts == 1_000.0
    assert z.state.cycle_start_room_temp == 26.0
    assert z.state.cycle_start_profile_name == "Journée présent"

    # In-cycle ticks: room descends, clim is cooling
    z.tick(_inp(2_000.0, room=24.5, hvac=HVAC_COOL, profile=p))
    z.tick(_inp(3_000.0, room=23.5, hvac=HVAC_COOL, profile=p))
    assert z.state.cycle_min_room_temp == 23.5

    # Schedule turns off → tick goes to SCHEDULE_OFF → cycle ends
    z.tick(_inp(3_500.0, room=23.7, hvac=HVAC_COOL, profile=p, schedule_on=False))

    assert len(z.state.completed_cycles) == 1
    rec = z.state.completed_cycles[0]
    assert rec["start_ts"] == 1_000.0
    assert rec["end_ts"] == 3_500.0
    assert rec["duration_min"] == round(2_500.0 / 60, 1)
    assert rec["profile_at_start"] == "Journée présent"
    assert rec["temp_start"] == 26.0
    assert rec["temp_end"] == 23.7
    assert rec["temp_min"] == 23.5
    assert rec["end_reason"] == "schedule_ended"

    # Snapshot fields reset for next cycle
    assert z.state.cycle_start_room_temp is None
    assert z.state.cycle_start_profile_name is None
    assert z.state.cycle_min_room_temp is None
    assert z.state.cycle_regimes_seen == []


def test_min_temp_tracked_across_in_cycle_ticks() -> None:
    p = _profile()
    z = Zone(_cfg())
    z.tick(_inp(1_000.0, room=26.0, profile=p))
    z.tick(_inp(1_300.0, room=24.0, hvac=HVAC_COOL, profile=p))
    z.tick(_inp(1_600.0, room=23.2, hvac=HVAC_COOL, profile=p))
    z.tick(_inp(1_900.0, room=23.5, hvac=HVAC_COOL, profile=p))  # rebound
    assert z.state.cycle_min_room_temp == 23.2


def test_regime_trace_tracked_by_update_helper() -> None:
    """Direct test of the snapshot helper: regimes set on state.regime are
    appended uniquely to cycle_regimes_seen on each tick while active."""
    p = _profile()
    z = Zone(_cfg())

    # Force into an active state with a real cycle anchor
    z._transition(ZoneState.RUNNING, 1_000.0)
    prev = {
        "state": ZoneState.RUNNING,
        "cycle_started_ts": 1_000.0,
        "cycle_start_room_temp": 26.0,
        "cycle_start_profile_name": "P",
        "cycle_min_room_temp": 26.0,
        "cycle_regimes_seen": [],
    }
    # Tick 1: regime ATTAQUE
    z.state.regime = Regime.ATTAQUE
    z._update_cycle_snapshot(_inp(1_100.0, room=25.5, hvac=HVAC_COOL, profile=p), prev)
    assert z.state.cycle_regimes_seen == [Regime.ATTAQUE]

    # Tick 2: same regime, must not duplicate
    prev["cycle_regimes_seen"] = list(z.state.cycle_regimes_seen)
    z._update_cycle_snapshot(_inp(1_200.0, room=25.0, hvac=HVAC_COOL, profile=p), prev)
    assert z.state.cycle_regimes_seen == [Regime.ATTAQUE]

    # Tick 3: new regime appended
    z.state.regime = Regime.STABILISATION
    prev["cycle_regimes_seen"] = list(z.state.cycle_regimes_seen)
    z._update_cycle_snapshot(_inp(1_300.0, room=23.5, hvac=HVAC_COOL, profile=p), prev)
    assert z.state.cycle_regimes_seen == [Regime.ATTAQUE, Regime.STABILISATION]


def test_cycle_history_capped_at_max() -> None:
    """Rolling log: when full, oldest entry is dropped."""
    p = _profile()
    z = Zone(_cfg())
    for i in range(CYCLE_HISTORY_MAX + 3):
        base = 1_000 + i * 10_000
        z.tick(_inp(base, room=26.0, profile=p))
        z.tick(_inp(base + 500, room=24.0, hvac=HVAC_COOL, profile=p))
        # Schedule off ends cycle
        z.tick(_inp(base + 1_000, room=24.0, hvac=HVAC_COOL, profile=p, schedule_on=False))
        # Schedule back on for next iteration
        z.tick(_inp(base + 2_000, room=24.0, profile=p))
    assert len(z.state.completed_cycles) == CYCLE_HISTORY_MAX
    # Oldest 3 dropped → first remaining start_ts > 1_000
    assert z.state.completed_cycles[0]["start_ts"] > 1_000


def test_no_record_when_cycle_starts_inside_tick_and_remains_active() -> None:
    """Sanity: if we just entered an active state in this tick, the cycle
    is *starting*, not ending — nothing to record yet."""
    p = _profile()
    z = Zone(_cfg())
    z.tick(_inp(1_000.0, room=26.0, profile=p))
    assert z.state.completed_cycles == []


def test_end_reason_labels() -> None:
    """End reasons map to stable labels the card can render."""
    assert Zone._end_reason_label(ZoneState.COOLDOWN) == "stabilization_complete"
    assert Zone._end_reason_label(ZoneState.IDLE) == "natural_end"
    assert Zone._end_reason_label(ZoneState.SCHEDULE_OFF) == "schedule_ended"
    assert Zone._end_reason_label(ZoneState.WINDOW_OPEN) == "window_opened"
    assert Zone._end_reason_label(ZoneState.MANUAL_OVERRIDE_TIMED) == "user_override"
    assert Zone._end_reason_label(ZoneState.MANUAL_OVERRIDE_FREE) == "user_override"


def test_window_open_records_cycle_end_with_proper_reason() -> None:
    p = _profile()
    z = Zone(_cfg())
    z.tick(_inp(1_000.0, room=26.0, profile=p))
    assert z.state.cycle_started_ts == 1_000.0

    z.tick(_inp(2_000.0, room=24.0, hvac=HVAC_COOL, profile=p, window_open=True))

    assert len(z.state.completed_cycles) == 1
    assert z.state.completed_cycles[0]["end_reason"] == "window_opened"
