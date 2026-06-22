"""Boot recovery: at HA restart, take over from a still-running clim."""

from __future__ import annotations

from custom_components.climate_manager.const import ZoneState
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

HVAC_OFF, HVAC_HEAT, HVAC_COOL = "off", "heat", "cool"


def _config(**ov) -> ZoneConfig:
    base = dict(
        zone_id="z1", name="Z1", climate_entity="climate.z1",
        temperature_sensors=["sensor.t1"], schedule_entity=None, window_sensors=[],
        seuil_debut_chauffage=19.5, seuil_fin_chauffage=21.0,
        seuil_debut_refroidissement=26.5, seuil_fin_refroidissement=24.0,
        duree_stabilisation_min=60, duree_cooldown_min=10, override_duree_min=30,
        aggressive_when_absent=True, aggressivity="normal",
    )
    base.update(ov)
    return ZoneConfig(**base)


def _inputs(**ov) -> ZoneInputs:
    base = dict(
        now_ts=1_000_000.0, room_temperature=25.0,
        clim_internal_temperature=24.0, clim_current_hvac_mode=HVAC_OFF,
        clim_current_setpoint=None, clim_current_fan_mode=None, clim_current_swing_mode=None,
        schedule_is_on=True, any_window_open=False, house_is_absent=False,
    )
    base.update(ov)
    return ZoneInputs(**base)


def test_fresh_zone_with_clim_already_cool_takes_over():
    """HA restart while a cooling cycle was in progress: don't turn the clim off."""
    zone = Zone(_config())
    # Fresh zone (last_state_transition_ts == 0.0) + clim already in cool
    cmds = zone.tick(_inputs(clim_current_hvac_mode=HVAC_COOL, room_temperature=25.5))
    # We expect to be in RUNNING now, not IDLE, and NOT to emit turn_off
    assert zone.state.state == ZoneState.RUNNING
    assert not any(c.service == "turn_off" for c in cmds), (
        f"Should not turn off the clim at boot. Got: {[c.service for c in cmds]}"
    )


def test_fresh_zone_with_clim_already_heat_takes_over():
    zone = Zone(_config(seuil_debut_chauffage=20.0, seuil_fin_chauffage=22.0))
    cmds = zone.tick(_inputs(clim_current_hvac_mode=HVAC_HEAT, room_temperature=20.5))
    assert zone.state.state == ZoneState.RUNNING
    assert not any(c.service == "turn_off" for c in cmds)


def test_fresh_zone_with_clim_off_stays_idle():
    """Standard case: nothing was running, stay IDLE."""
    zone = Zone(_config())
    zone.tick(_inputs(clim_current_hvac_mode=HVAC_OFF, room_temperature=25.0))
    assert zone.state.state == ZoneState.IDLE


def test_recovery_check_only_runs_on_fresh_zone():
    """Once the zone has transitioned once, we should NOT re-take-over on every
    tick. Otherwise an IDLE zone with a manually-running clim would never get
    the chance to actually turn it off."""
    zone = Zone(_config())
    # Simulate: zone already transitioned previously (last_state_transition_ts > 0)
    zone.state.last_state_transition_ts = 1.0
    zone.state.state = ZoneState.IDLE
    cmds = zone.tick(_inputs(clim_current_hvac_mode=HVAC_COOL, room_temperature=25.0))
    # Now the normal IDLE behaviour applies: turn off the running clim
    assert zone.state.state == ZoneState.IDLE
    assert any(c.service == "turn_off" for c in cmds)
