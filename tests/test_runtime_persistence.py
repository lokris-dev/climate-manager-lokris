"""Runtime persistence: HA restarts must resume timed phases idempotently."""

from __future__ import annotations

from custom_components.climate_manager.const import Regime, ZoneState
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs, ZoneRuntimeState

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
        duree_stabilisation_min=60,
        duree_cooldown_min=10,
    )


def _inp(now_ts: float, *, room: float, hvac: str = HVAC_COOL) -> ZoneInputs:
    return ZoneInputs(
        now_ts=now_ts,
        room_temperature=room,
        clim_internal_temperature=24.0,
        clim_current_hvac_mode=hvac,
        clim_current_setpoint=24.0,
        clim_current_fan_mode="quiet",
        clim_current_swing_mode="windnice",
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
    )


def test_runtime_state_round_trip_keeps_stabilizing_phase() -> None:
    state = ZoneRuntimeState(
        state=ZoneState.STABILIZING,
        regime=Regime.STABILISATION,
        last_state_transition_ts=2_000.0,
        last_command_ts=1_900.0,
        last_setpoint_sent=24.0,
        last_fan_sent="quiet",
        mode="auto",
        cycle_started_ts=1_000.0,
        cycle_start_room_temp=27.2,
        cycle_start_profile_name="Nuit",
        cycle_min_room_temp=23.9,
        cycle_regimes_seen=[Regime.ATTAQUE, Regime.STABILISATION],
        completed_cycles=[{"start_ts": 10.0, "end_ts": 20.0}],
    )

    restored = ZoneRuntimeState.from_dict(state.to_dict())

    assert restored.state == ZoneState.STABILIZING
    assert restored.regime == Regime.STABILISATION
    assert restored.last_state_transition_ts == 2_000.0
    assert restored.cycle_started_ts == 1_000.0
    assert restored.cycle_start_profile_name == "Nuit"
    assert restored.cycle_regimes_seen == [Regime.ATTAQUE, Regime.STABILISATION]
    assert restored.completed_cycles == [{"start_ts": 10.0, "end_ts": 20.0}]


def test_restored_stabilizing_zone_does_not_restart_cycle() -> None:
    restored = ZoneRuntimeState(
        state=ZoneState.STABILIZING,
        regime=Regime.STABILISATION,
        last_state_transition_ts=2_000.0,
        cycle_started_ts=1_000.0,
        cycle_start_room_temp=27.0,
        cycle_min_room_temp=24.0,
    )
    zone = Zone(_cfg(), state=ZoneRuntimeState.from_dict(restored.to_dict()))

    cmds = zone.tick(_inp(2_300.0, room=24.2, hvac=HVAC_COOL))

    assert zone.state.state == ZoneState.STABILIZING
    assert zone.state.cycle_started_ts == 1_000.0
    assert not any(c.service == "set_hvac_mode" for c in cmds)


def test_restored_stabilizing_advances_to_cooldown_when_elapsed() -> None:
    restored = ZoneRuntimeState(
        state=ZoneState.STABILIZING,
        regime=Regime.STABILISATION,
        last_state_transition_ts=2_000.0,
        cycle_started_ts=1_000.0,
    )
    zone = Zone(_cfg(), state=ZoneRuntimeState.from_dict(restored.to_dict()))

    zone.tick(_inp(6_000.0, room=24.2, hvac=HVAC_COOL))

    assert zone.state.state == ZoneState.COOLDOWN
    assert zone.state.cycle_started_ts is None


def test_partial_or_legacy_payload_is_tolerated() -> None:
    restored = ZoneRuntimeState.from_dict({"completed_cycles": [{"start_ts": 1.0}]})

    assert restored.state == ZoneState.IDLE
    assert restored.mode == "auto"
    assert restored.completed_cycles == [{"start_ts": 1.0}]
