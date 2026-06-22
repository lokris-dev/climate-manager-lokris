"""Tests for cycle_started_ts — anchor exposed to the Lovelace card so it can
render 'démarré il y a Xmin' and the cooling-progress sparkline."""

from __future__ import annotations

from custom_components.climate_manager.const import ZoneState
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

HVAC_OFF = "off"
HVAC_COOL = "cool"


def _cfg() -> ZoneConfig:
    return ZoneConfig(
        zone_id="z1",
        name="Z1",
        climate_entity="climate.z1",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
        seuil_debut_refroidissement=26.5,
        seuil_fin_refroidissement=24.0,
    )


def _inp(now_ts: float, *, room: float, hvac: str = HVAC_OFF) -> ZoneInputs:
    return ZoneInputs(
        now_ts=now_ts,
        room_temperature=room,
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=hvac,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
    )


def test_cycle_starts_on_idle_to_starting_transition() -> None:
    z = Zone(_cfg())
    z.tick(_inp(1_000.0, room=22.0))  # below seuil_debut_refroidissement → stays IDLE
    assert z.state.cycle_started_ts is None

    z.tick(_inp(2_000.0, room=27.0))  # crosses seuil → STARTING
    assert z.state.state in (ZoneState.STARTING, ZoneState.RUNNING)
    assert z.state.cycle_started_ts == 2_000.0


def test_cycle_anchor_survives_running_and_stabilizing() -> None:
    z = Zone(_cfg())
    # Skip the IDLE→STARTING tick by setting STARTING directly
    z._transition(ZoneState.STARTING, 5_000.0)
    assert z.state.cycle_started_ts == 5_000.0

    z._transition(ZoneState.RUNNING, 5_030.0)
    assert z.state.cycle_started_ts == 5_000.0, "RUNNING must preserve cycle start"

    z._transition(ZoneState.STABILIZING, 6_500.0)
    assert z.state.cycle_started_ts == 5_000.0, "STABILIZING must preserve cycle start"


def test_cycle_anchor_clears_on_idle() -> None:
    z = Zone(_cfg())
    z._transition(ZoneState.STARTING, 1_000.0)
    z._transition(ZoneState.IDLE, 2_000.0)
    assert z.state.cycle_started_ts is None


def test_cycle_anchor_clears_on_override() -> None:
    """The morning-of-2026-05-30 scenario: cycle running, then external
    override interrupts. If it ever resumes, we want a fresh anchor — not the
    stale pre-override one."""
    z = Zone(_cfg())
    z._transition(ZoneState.STARTING, 1_000.0)
    z._transition(ZoneState.RUNNING, 1_030.0)
    z.on_external_override(1_500.0, schedule_is_on=True)
    assert z.state.state == ZoneState.MANUAL_OVERRIDE_TIMED
    assert z.state.cycle_started_ts is None


def test_boot_recovery_anchors_to_clim_last_changed() -> None:
    """At HA restart mid-cycle, the boot-recovery path adopts the running clim.
    Using `now` as anchor would lie about elapsed time after every restart, so
    we anchor to the clim's own last_changed (when it went heat/cool) instead.
    """
    z = Zone(_cfg())
    z.tick(ZoneInputs(
        now_ts=10_000.0,
        room_temperature=26.0,
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=HVAC_COOL,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
        clim_state_last_changed_ts=8_500.0,  # clim went cool 1500s before this tick
    ))
    assert z.state.state == ZoneState.RUNNING
    assert z.state.cycle_started_ts == 8_500.0


def test_boot_recovery_falls_back_to_now_without_clim_last_changed() -> None:
    """If we have no clim last_changed (e.g. very fresh HA install), the
    anchor falls back to now — best-effort, still better than nothing."""
    z = Zone(_cfg())
    z.tick(_inp(9_999.0, room=26.0, hvac=HVAC_COOL))
    assert z.state.cycle_started_ts == 9_999.0
