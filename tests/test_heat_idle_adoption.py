"""Régression du 26/10 : daily_reset remet en IDLE des zones qui chauffent.

En pendule, IDLE ne pilotait rien et ne réengageait que sous le seuil de début :
les splits restaient en `heat` sur leur dernière consigne. Toute zone en hiver
dont un split chauffe doit être reprise par le maintien chaud (OFF temporisé).
"""

from dataclasses import replace

import pytest

from custom_components.climate_manager.const import (
    HEAT_MIN_ON_SECONDS,
    ZoneState,
)
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

NOW = 1_000_000.0
SPLITS = ["climate.stock", "climate.repro"]


def _zone(**kwargs):
    cfg = dict(
        zone_id="bureau", name="Bureau", climate_entity=SPLITS[0],
        climate_entities=SPLITS, temperature_sensors=["sensor.room"],
        schedule_entity=None, pendulum_idle=True, target_temp=22.0, power="normal",
        splits_config={SPLITS[0]: {}, SPLITS[1]: {}},
    )
    cfg.update(kwargs)
    return Zone(ZoneConfig(**cfg))


def _inp(**kwargs):
    inp = dict(
        now_ts=NOW, room_temperature=24.5, clim_internal_temperature=29.0,
        clim_current_hvac_mode="heat", clim_current_setpoint=25.0,
        clim_current_fan_mode="auto", clim_current_swing_mode=None,
        schedule_is_on=True, any_window_open=False, house_is_absent=False,
        supports_windnice=False, clim_setpoint_step=1.0, system_direction="heat",
        clim_internal_by_entity={SPLITS[0]: 28.0, SPLITS[1]: 30.0},
        clim_hvac_by_entity={s: "heat" for s in SPLITS},
    )
    inp.update(kwargs)
    return ZoneInputs(**inp)


def _running_then_reset(heating_for=3 * 3600):
    """Zone en attaque depuis `heating_for` s, puis reset du désarmement."""
    zone = _zone()
    zone.state.state = ZoneState.RUNNING
    zone.state.active_direction = "heat"
    zone.state.heat_output_on = True
    zone.state.heat_output_since_ts = NOW - heating_for
    zone.state.last_hvac_sent = "heat"
    zone.daily_reset(NOW - 1)
    assert zone.state.state == ZoneState.IDLE
    return zone


def _services(cmds):
    return [(c.service, c.data.get("hvac_mode")) for c in cmds]


@pytest.mark.parametrize("room", [22.0, 22.5, 24.5, 25.5])
def test_reset_while_heating_above_target_cuts_the_zone(room):
    zone = _running_then_reset()
    cmds = zone.tick(_inp(room_temperature=room))
    assert ("set_hvac_mode", "off") in _services(cmds)
    assert cmds[0].data["entity_id"] == SPLITS
    assert zone.state.state == ZoneState.RUNNING
    assert zone.state.active_direction == "heat"
    assert zone.state.heat_output_on is False


def test_reset_while_heating_between_start_threshold_and_target_stays_controlled():
    # 21,5 : au-dessus du seuil de début (21) → l'ancien IDLE ne faisait rien
    zone = _running_then_reset(heating_for=60)
    zone.tick(_inp(room_temperature=21.5))
    assert zone.state.state == ZoneState.RUNNING
    assert zone.state.active_direction == "heat"
    # la pièce atteint la cible : le minimum allumé expiré, la zone est coupée
    cmds = zone.tick(_inp(now_ts=NOW + HEAT_MIN_ON_SECONDS, room_temperature=22.2))
    assert ("set_hvac_mode", "off") in _services(cmds)


def test_override_expiry_back_to_idle_while_heating_is_adopted():
    zone = _zone()
    zone.state.state = ZoneState.MANUAL_OVERRIDE_TIMED
    zone.state.override_until_ts = NOW - 1
    cmds = zone.tick(_inp(room_temperature=24.5))
    assert ("set_hvac_mode", "off") in _services(cmds)
    assert zone.state.active_direction == "heat"


def test_only_a_secondary_split_heating_is_cut():
    zone = _running_then_reset()
    modes = {SPLITS[0]: "off", SPLITS[1]: "heat"}
    cmds = zone.tick(_inp(room_temperature=24.5, clim_hvac_by_entity=modes,
                          clim_current_hvac_mode="heat"))
    assert ("set_hvac_mode", "off") in _services(cmds)


def test_secondary_heating_while_primary_cools_is_cut_in_winter():
    zone = _zone()
    zone.state.state = ZoneState.RUNNING  # active_direction perdue (reload)
    zone.state.heat_output_on = True
    zone.state.heat_output_since_ts = NOW - 3600
    modes = {SPLITS[0]: "cool", SPLITS[1]: "heat"}
    cmds = zone.tick(_inp(room_temperature=24.5, clim_hvac_by_entity=modes,
                          clim_current_hvac_mode="cool"))
    assert ("set_hvac_mode", "off") in _services(cmds)


def test_cool_season_is_unchanged():
    zone = _running_then_reset()
    zone.state.active_direction = None
    # 22,5 : dans la bande morte, aucun sens froid à engager
    assert zone.tick(_inp(room_temperature=22.5, system_direction="cool")) == []
    assert zone.state.state == ZoneState.IDLE


def test_idle_with_split_off_stays_idle():
    zone = _running_then_reset()
    off = {s: "off" for s in SPLITS}
    inp = _inp(room_temperature=24.5, clim_hvac_by_entity=off, clim_current_hvac_mode="off")
    assert zone.tick(inp) == []
    assert zone.state.state == ZoneState.IDLE


def test_room_sensor_lost_does_not_leave_heat_running_blind():
    zone = _zone()
    zone.state.state = ZoneState.RUNNING
    zone.state.active_direction = "heat"
    zone.tick(_inp(room_temperature=21.0, clim_state_last_changed_ts=NOW))
    assert zone.state.heat_output_on is True
    blind = _inp(room_temperature=None)
    # minimum allumé non écoulé : on ne coupe pas encore
    assert zone.tick(replace(blind, now_ts=NOW + HEAT_MIN_ON_SECONDS - 120)) == []
    # écoulé : plus de chauffe à l'aveugle
    cmds = zone.tick(replace(blind, now_ts=NOW + HEAT_MIN_ON_SECONDS))
    assert ("set_hvac_mode", "off") in _services(cmds)
    assert zone.state.heat_output_on is False
