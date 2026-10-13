"""Régression Hitachi du 13/10 : OFF réel, temporisations et approche douce."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.climate_manager.const import (
    HEAT_MIN_OFF_SECONDS,
    HEAT_MIN_ON_SECONDS,
    Regime,
    ZoneState,
)
from custom_components.climate_manager.coordinator import (
    DelormejClimateCoordinator,
    _is_echo_of_intent,
)
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs, ZoneRuntimeState

NOW = 1_000_000.0
SPLITS = ["climate.stock", "climate.repro"]


def _zone(**kwargs):
    cfg = dict(
        zone_id="openspace", name="Openspace", climate_entity=SPLITS[0],
        climate_entities=SPLITS, temperature_sensors=["sensor.room"],
        schedule_entity=None, pendulum_idle=True, target_temp=22.0, power="normal",
        splits_config={SPLITS[0]: {}, SPLITS[1]: {"target": 25.0}},
    )
    cfg.update(kwargs)
    zone = Zone(ZoneConfig(**cfg))
    zone.state.state = ZoneState.RUNNING
    zone.state.active_direction = "heat"
    return zone


def _inp(**kwargs):
    inp = dict(
        now_ts=NOW, room_temperature=22.0, clim_internal_temperature=29.0,
        clim_current_hvac_mode="heat", clim_current_setpoint=30.0,
        clim_current_fan_mode="auto", clim_current_swing_mode=None,
        schedule_is_on=True, any_window_open=False, house_is_absent=False,
        supports_windnice=False, clim_setpoint_step=1.0, system_direction="heat",
        clim_internal_by_entity={SPLITS[0]: 28.0, SPLITS[1]: 30.0},
    )
    inp.update(kwargs)
    return ZoneInputs(**inp)


def _hvac(cmds):
    return [c.data["hvac_mode"] for c in cmds if c.service == "set_hvac_mode"]


def _setpoints(cmds):
    return {c.data["entity_id"]: c.data["temperature"] for c in cmds if c.service == "set_temperature"}


@pytest.mark.parametrize("room", [22.0, 24.0, 25.5])
@pytest.mark.parametrize("splits_config", [{}, {SPLITS[0]: {}, SPLITS[1]: {"target": 25.0}}])
def test_production_replay_cuts_whole_zone(room, splits_config):
    zone = _zone(splits_config=splits_config)
    cmds = zone.tick(_inp(room_temperature=room))
    assert _hvac(cmds) == ["off"]
    assert cmds[0].data["entity_id"] == SPLITS
    assert _setpoints(cmds) == {}
    assert zone.state.regime == Regime.STABILISATION
    assert zone.state.active_direction == "heat"
    assert zone.state.state == ZoneState.RUNNING


def test_off_waits_15_minutes_then_resumes_just_below_target():
    zone = _zone()
    zone.tick(_inp())
    off = _inp(clim_current_hvac_mode="off", room_temperature=21.9)
    assert zone.tick(replace(off, now_ts=NOW + HEAT_MIN_OFF_SECONDS - 1)) == []
    assert zone.state.heat_output_since_ts == NOW
    assert _hvac(zone.tick(replace(off, now_ts=NOW + HEAT_MIN_OFF_SECONDS))) == ["heat"]
    assert zone.state.regime == Regime.ATTAQUE
    assert zone.state.heat_output_since_ts == NOW + HEAT_MIN_OFF_SECONDS


def test_expired_off_timer_does_not_restart_at_or_above_target():
    zone = _zone()
    zone.tick(_inp())
    for room in (22.0, 22.1, 25.5):
        assert zone.tick(_inp(
            now_ts=NOW + HEAT_MIN_OFF_SECONDS, room_temperature=room,
            clim_current_hvac_mode="off",
        )) == []
        assert zone.state.last_hvac_sent == "off"


def test_minimum_on_holds_then_cuts_at_15_minutes():
    zone = _zone()
    zone.tick(_inp())
    start = NOW + HEAT_MIN_OFF_SECONDS
    assert _hvac(zone.tick(_inp(now_ts=start, room_temperature=21.9, clim_current_hvac_mode="off"))) == ["heat"]
    assert max(_setpoints(zone.tick(_inp(now_ts=start + 30, room_temperature=22.2))).values()) <= 23.0
    assert _hvac(zone.tick(_inp(now_ts=start + HEAT_MIN_ON_SECONDS - 1, room_temperature=22.2))) == []
    assert zone.state.heat_output_since_ts == start
    assert _hvac(zone.tick(_inp(now_ts=start + HEAT_MIN_ON_SECONDS, room_temperature=22.2))) == ["off"]


def test_hard_overshoot_bypasses_minimum_on():
    zone = _zone()
    zone.state.heat_output_on = True
    zone.state.heat_output_since_ts = NOW - 60
    assert _hvac(zone.tick(_inp(room_temperature=23.0))) == ["off"]
    assert zone.state.heat_output_since_ts == NOW


@pytest.mark.parametrize("gate", [{"any_window_open": True}, {"schedule_is_on": False}, {"mode": "off"}])
def test_explicit_off_is_immediate_and_restart_respects_minimum_off(gate):
    zone = _zone()
    zone.state.heat_output_on = True
    zone.state.heat_output_since_ts = NOW - 60
    mode = gate.get("mode")
    if mode:
        zone.state.mode = mode
    cmds = zone.tick(_inp(**{k: v for k, v in gate.items() if k != "mode"}))
    assert any(c.service == "turn_off" for c in cmds)
    assert zone.state.heat_output_on is False
    zone.state.mode = "auto"
    assert zone.tick(_inp(now_ts=NOW + 60, clim_current_hvac_mode="off", room_temperature=20.0)) == []


@pytest.mark.parametrize("on", [True, False])
def test_persisted_timer_survives_reload(on):
    zone = _zone()
    zone.state.heat_output_on = on
    zone.state.heat_output_since_ts = NOW
    zone.state.last_hvac_sent = "heat" if on else "off"
    restored = Zone(zone.config, ZoneRuntimeState.from_dict(zone.state.to_dict()))
    cmds = restored.tick(_inp(
        now_ts=NOW + 120, room_temperature=22.2 if on else 21.8,
        clim_current_hvac_mode="heat" if on else "off",
    ))
    assert _hvac(cmds) == []
    assert restored.state.heat_output_since_ts == NOW
    assert restored.state.heat_output_on is on


def test_daily_reset_does_not_shorten_minimum_off():
    zone = _zone()
    zone.tick(_inp())
    zone.daily_reset(NOW + 60)
    assert zone.tick(_inp(now_ts=NOW + 60, clim_current_hvac_mode="off", room_temperature=20.0)) == []
    assert zone.state.heat_output_since_ts == NOW


def test_known_recent_heat_is_adopted_without_resetting_timer():
    zone = _zone()
    assert _hvac(zone.tick(_inp(clim_state_last_changed_ts=NOW - 60))) == []
    assert zone.state.heat_output_since_ts == NOW - 60
    assert _hvac(zone.tick(_inp(now_ts=NOW + HEAT_MIN_ON_SECONDS - 60))) == ["off"]


def test_secondary_still_heating_gets_off_again_without_resetting_timer():
    zone = _zone()
    zone.tick(_inp())
    cmds = zone.tick(_inp(
        now_ts=NOW + 120, clim_current_hvac_mode="off",
        clim_hvac_by_entity={SPLITS[0]: "off", SPLITS[1]: "heat"},
    ))
    assert _hvac(cmds) == ["off"]
    assert cmds[0].data["entity_id"] == SPLITS
    assert zone.state.heat_output_since_ts == NOW


def test_secondary_still_off_is_resynchronized_on_resume():
    zone = _zone()
    cmds = zone.tick(_inp(
        room_temperature=20.0, clim_current_hvac_mode="heat",
        clim_hvac_by_entity={SPLITS[0]: "heat", SPLITS[1]: "off"},
    ))
    assert _hvac(cmds) == ["heat"]
    assert cmds[0].data["entity_id"] == SPLITS


def test_secondary_setpoint_is_compared_with_its_own_value():
    zone = _zone()
    initial = _inp(room_temperature=21.5)
    intended = _setpoints(zone.tick(initial))
    current = {SPLITS[0]: intended[SPLITS[0]], SPLITS[1]: 30.0}
    cmds = zone.tick(replace(
        initial, now_ts=NOW + 15, clim_current_setpoint=intended[SPLITS[1]],
        clim_setpoint_by_entity=current,
    ))
    assert _setpoints(cmds) == {SPLITS[1]: intended[SPLITS[1]]}


def test_attack_tapers_with_room_temperature_even_with_hot_split_probes():
    zone = _zone()
    values = []
    for i, room in enumerate((19.0, 20.0, 20.5, 21.0, 21.5, 21.9)):
        cmds = zone.tick(_inp(now_ts=NOW + i * 60, room_temperature=room))
        values.append(_setpoints(cmds)[SPLITS[1]])
    assert values == sorted(values, reverse=True)
    assert values[0] == 32.0  # loin de la cible : attaque inchangée, clamp 32
    assert values[-1] == 23.0
    assert all(c.data.get("hvac_mode") != "off" for c in cmds)


def test_first_start_uses_taper_and_starts_minimum_on():
    zone = _zone()
    zone.state.state = ZoneState.IDLE
    zone.state.active_direction = None
    cmds = zone.tick(_inp(room_temperature=20.9, clim_current_hvac_mode="off"))
    assert _hvac(cmds) == ["heat"]
    assert max(_setpoints(cmds).values()) < 32.0
    assert zone.state.heat_output_since_ts == NOW


def test_missing_room_sensor_does_not_restart_released_heat():
    zone = _zone()
    zone.tick(_inp())
    assert zone.tick(_inp(
        now_ts=NOW + HEAT_MIN_OFF_SECONDS, room_temperature=None,
        clim_current_hvac_mode="off",
    )) == []
    assert zone.state.last_hvac_sent == "off"


def test_heat_cool_heat_flip_starts_a_new_minimum_on_period():
    zone = _zone()
    zone.tick(_inp(room_temperature=20.0))
    zone.tick(_inp(now_ts=NOW + 1000, room_temperature=25.0,
                   system_direction="cool", clim_current_hvac_mode="heat"))
    assert zone.state.heat_output_on is False
    cmds = zone.tick(_inp(now_ts=NOW + 2000, room_temperature=20.0,
                         system_direction="heat", clim_current_hvac_mode="cool"))
    assert _hvac(cmds) == ["heat"]
    assert zone.state.heat_output_since_ts == NOW + 2000
    assert _hvac(zone.tick(_inp(now_ts=NOW + 2060, room_temperature=22.2))) == []


def test_off_echo_ignores_residual_setpoint_and_fan_but_manual_on_diverges():
    zone = _zone()
    zone.state.last_fan_sent = "low"
    zone.state.last_setpoint_sent = 18.0
    zone.tick(_inp())
    state = SimpleNamespace(state="off", attributes={"temperature": 30.0, "fan_mode": "auto"})
    assert _is_echo_of_intent(zone, state)
    state.state = "heat"
    assert not _is_echo_of_intent(zone, state)


def test_gather_inputs_preserves_each_split_mode_and_setpoint():
    zone = _zone()
    states = {
        ent: SimpleNamespace(state=mode, attributes={
            "temperature": sp, "current_temperature": 29.0,
            "hvac_modes": ["off", "heat", "cool"], "fan_modes": ["auto", "low"],
        }, last_changed=None)
        for ent, mode, sp in ((SPLITS[0], "off", 23.0), (SPLITS[1], "heat", 30.0))
    }
    coord = object.__new__(DelormejClimateCoordinator)
    coord.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    coord._average_temperature = lambda _: 22.0
    coord._active_profile = lambda _: zone.config.profiles[0]
    coord._precool_active_for = lambda _: False
    coord._house_is_absent = lambda: False
    coord._any_window_open = lambda _: False
    coord._current_system_direction = "heat"
    inp = coord._gather_inputs(zone)
    assert inp.clim_hvac_by_entity == {SPLITS[0]: "off", SPLITS[1]: "heat"}
    assert inp.clim_setpoint_by_entity == {SPLITS[0]: 23.0, SPLITS[1]: 30.0}
    assert _hvac(zone.tick(inp)) == ["off"]
