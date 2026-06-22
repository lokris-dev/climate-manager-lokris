"""Capability auto-detection from the underlying climate entity.

A heat-only clim (e.g. the bathroom heater `climate.chauffage_salle_de_bain`)
should never have its cool side triggered by the integration. Symmetric for
a cool-only clim. fan_mode / windnice should be skipped when the clim doesn't
expose them.
"""

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
        now_ts=1_000_000.0, room_temperature=22.0,
        clim_internal_temperature=22.0, clim_current_hvac_mode=HVAC_OFF,
        clim_current_setpoint=None, clim_current_fan_mode=None, clim_current_swing_mode=None,
        schedule_is_on=True, any_window_open=False, house_is_absent=False,
        supports_cool=True, supports_heat=True,
        supports_fan_mode=True, supports_windnice=True,
    )
    base.update(ov)
    return ZoneInputs(**base)


def test_heat_only_zone_ignores_high_temperature():
    """T° pièce > seuil_debut_refroidissement on a heat-only clim → stays IDLE."""
    zone = Zone(_config(seuil_debut_refroidissement=26.0))
    zone.tick(_inputs(room_temperature=28.0, supports_cool=False))
    assert zone.state.state == ZoneState.IDLE


def test_cool_only_zone_ignores_low_temperature():
    """T° pièce < seuil_debut_chauffage on a cool-only clim → stays IDLE."""
    zone = Zone(_config(seuil_debut_chauffage=20.0))
    zone.tick(_inputs(room_temperature=17.0, supports_heat=False))
    assert zone.state.state == ZoneState.IDLE


def test_heat_only_zone_still_triggers_heat():
    """A heat-only clim still triggers heat when T° < seuil_debut_chauffage."""
    zone = Zone(_config(seuil_debut_chauffage=20.0, seuil_fin_chauffage=22.0))
    cmds = zone.tick(_inputs(room_temperature=18.0, supports_cool=False))
    hvac = next((c.data.get("hvac_mode") for c in cmds if c.service == "set_hvac_mode"), None)
    assert hvac == HVAC_HEAT


def test_force_start_cool_noop_on_heat_only_zone():
    zone = Zone(_config())
    zone.force_start("cool", now_ts=1_000.0, supports={"cool": False, "heat": True})
    assert zone.state.state == ZoneState.IDLE
    assert zone.state.forced_direction is None


def test_force_start_heat_works_on_heat_only_zone():
    zone = Zone(_config())
    zone.force_start("heat", now_ts=1_000.0, supports={"cool": False, "heat": True})
    assert zone.state.state == ZoneState.STARTING
    assert zone.state.forced_direction == HVAC_HEAT


def test_emit_active_skips_set_fan_mode_when_unsupported():
    """A clim without fan_modes should not receive any climate.set_fan_mode call."""
    zone = Zone(_config(seuil_debut_chauffage=20.0, seuil_fin_chauffage=22.0))
    cmds = zone.tick(_inputs(
        room_temperature=17.0,
        clim_internal_temperature=17.0,
        clim_current_hvac_mode=HVAC_OFF,
        supports_cool=False,
        supports_fan_mode=False,
        supports_windnice=False,
    ))
    fan_calls = [c for c in cmds if c.service == "set_fan_mode"]
    swing_calls = [c for c in cmds if c.service == "set_swing_mode"]
    assert fan_calls == []
    assert swing_calls == []


def test_emit_active_still_sets_hvac_and_temperature_for_heat_only():
    """Capability-skipped fan/swing shouldn't break the rest of the cycle."""
    zone = Zone(_config(seuil_debut_chauffage=20.0, seuil_fin_chauffage=22.0))
    cmds = zone.tick(_inputs(
        room_temperature=17.0,
        clim_internal_temperature=17.0,
        clim_current_hvac_mode=HVAC_OFF,
        supports_cool=False, supports_fan_mode=False, supports_windnice=False,
    ))
    services = {c.service for c in cmds}
    assert "set_hvac_mode" in services
    assert "set_temperature" in services
