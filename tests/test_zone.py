"""Unit tests for the core Zone logic.

These tests do NOT need a running Home Assistant — they exercise the pure-logic
Zone class in isolation, which is the whole point of having it.

The most important assertion (and the reason this project exists): in cool mode,
the setpoint sent to the AC must be BELOW the AC's internal temperature, so that
the compressor actually runs. The YAML automation we are replacing got this
backwards and shipped a positive offset in cool, neutralizing the AC.
"""

from __future__ import annotations

import pytest

from custom_components.climate_manager.const import (
    BOOST_FAN_MODE,
    BOOST_OFFSET,
    OFFSET_ATTAQUE,
    Regime,
    ZoneMode,
    ZoneState,
)
from custom_components.climate_manager.zone import (
    Zone,
    ZoneConfig,
    ZoneInputs,
)

# Defer importing HVACMode (it lives in HA) — pytest will pick it via the
# project's requirements-dev.txt (homeassistant). Until HA is installed,
# we read the string values that match the homeassistant.components.climate
# enum.
HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"


def _config(**overrides) -> ZoneConfig:
    base = dict(
        zone_id="zone1",
        name="Zone 1",
        climate_entity="climate.zone1",
        temperature_sensors=["sensor.t1"],
        schedule_entity=None,
        window_sensors=[],
        seuil_debut_chauffage=19.5,
        seuil_fin_chauffage=21.0,
        seuil_debut_refroidissement=26.5,
        seuil_fin_refroidissement=25.0,
        duree_stabilisation_min=60,
        duree_cooldown_min=10,
        override_duree_min=30,
        aggressive_when_absent=True,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def _inputs(**overrides) -> ZoneInputs:
    base = dict(
        now_ts=1_000_000.0,
        room_temperature=22.0,
        clim_internal_temperature=24.0,
        clim_current_hvac_mode=HVAC_OFF,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
    )
    base.update(overrides)
    return ZoneInputs(**base)


def _find_setpoint(commands):
    for c in commands:
        if c.service == "set_temperature":
            return c.data.get("temperature")
    return None


def _find_hvac(commands):
    for c in commands:
        if c.service == "set_hvac_mode":
            return c.data.get("hvac_mode")
    return None


# === The bug fix ===


def test_cool_setpoint_is_BELOW_internal_temperature():
    """The whole point of this project: cooling must lower setpoint, not raise it.

    Reproduces the YAML automation's bug scenario: room=26, internal=26,
    target seuil_fin=25. The broken automation sent 29; the correct value
    must be below 26.
    """
    zone = Zone(_config(seuil_fin_refroidissement=25.0, seuil_debut_refroidissement=25.0))
    inp = _inputs(
        room_temperature=26.0,
        clim_internal_temperature=26.0,
        clim_current_hvac_mode=HVAC_OFF,
    )
    cmds = zone.tick(inp)
    setpoint = _find_setpoint(cmds)
    hvac = _find_hvac(cmds)
    assert hvac == HVAC_COOL
    assert setpoint is not None
    assert setpoint < 26.0, (
        f"Cool setpoint must be below internal temperature; got {setpoint}. "
        "This is the bug from the YAML automation."
    )


def test_heat_setpoint_is_ABOVE_internal_temperature():
    """Symmetric: heating must raise setpoint above internal temperature."""
    zone = Zone(_config(seuil_debut_chauffage=20.0, seuil_fin_chauffage=22.0))
    inp = _inputs(
        room_temperature=18.0,
        clim_internal_temperature=18.0,
        clim_current_hvac_mode=HVAC_OFF,
    )
    cmds = zone.tick(inp)
    setpoint = _find_setpoint(cmds)
    hvac = _find_hvac(cmds)
    assert hvac == HVAC_HEAT
    assert setpoint is not None
    assert setpoint > 18.0


# === Régime maths ===


def test_attaque_in_cool_uses_offset_attaque_below_internal():
    zone = Zone(_config(seuil_fin_refroidissement=25.0))
    inp = _inputs(
        room_temperature=28.0,  # écart = 3 > 2 → ATTAQUE
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=HVAC_COOL,
    )
    # Skip STARTING by pretending we're already RUNNING
    zone.state.state = ZoneState.RUNNING
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    expected = 27.0 - OFFSET_ATTAQUE
    assert sp is not None
    assert abs(sp - expected) < 0.5, f"Expected ≈ {expected}, got {sp}"
    assert zone.state.regime == Regime.ATTAQUE


def test_running_uses_constant_attaque_offset_close_to_target():
    """Architecture D (juin 2026): no more CROISIERE/APPROCHE handoffs. The
    offset stays at ATTAQUE throughout RUNNING — Daikin's inverter handles
    the ramp-down internally. Reproduces the bug where the descent plateaued
    when the integration switched to CROISIERE 2°C from target."""
    zone = Zone(_config(seuil_fin_refroidissement=25.0))
    # Close to target (would have been CROISIERE in the old model)
    inp = _inputs(
        room_temperature=26.0,
        clim_internal_temperature=26.0,
        clim_current_hvac_mode=HVAC_COOL,
    )
    zone.state.state = ZoneState.RUNNING
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    assert sp is not None
    assert abs(sp - (26.0 - OFFSET_ATTAQUE)) < 0.5
    assert zone.state.regime == Regime.ATTAQUE

    # Very close to target (would have been APPROCHE in the old model)
    inp = _inputs(
        room_temperature=25.3,
        clim_internal_temperature=25.0,
        clim_current_hvac_mode=HVAC_COOL,
    )
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    assert sp is not None
    assert abs(sp - (25.0 - OFFSET_ATTAQUE)) < 0.5
    assert zone.state.regime == Regime.ATTAQUE


@pytest.mark.parametrize(
    ("power", "expected_offset"),
    [
        ("doux", 1.0),
        ("normal", 1.5),
        ("agressif", 2.0),
    ],
)
def test_stabilisation_uses_soft_maintenance_offset_in_cool(power, expected_offset):
    zone = Zone(_config(seuil_fin_refroidissement=25.0, power=power))
    inp = _inputs(
        room_temperature=24.8,  # below seuil_fin → STABILIZING
        clim_internal_temperature=24.8,
        clim_current_hvac_mode=HVAC_COOL,
    )
    zone.state.state = ZoneState.STABILIZING
    zone.state.last_state_transition_ts = inp.now_ts  # just entered STABILIZING
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    expected = 24.8 - expected_offset
    assert sp is not None
    assert abs(sp - expected) < 0.5
    assert zone.state.regime == Regime.STABILISATION


def test_stabilisation_uses_soft_maintenance_offset_in_heat():
    zone = Zone(_config(seuil_fin_chauffage=21.0, power="normal"))
    inp = _inputs(
        room_temperature=21.2,  # above seuil_fin → STABILIZING
        clim_internal_temperature=20.0,
        clim_current_hvac_mode=HVAC_HEAT,
    )
    zone.state.state = ZoneState.STABILIZING
    zone.state.last_state_transition_ts = inp.now_ts
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    expected = 20.0 + 1.5
    assert sp is not None
    assert abs(sp - expected) < 0.5
    assert zone.state.regime == Regime.STABILISATION


# === Hard gates ===


def test_window_open_turns_off_clim():
    zone = Zone(_config(window_sensors=["binary_sensor.window"]))
    inp = _inputs(
        room_temperature=28.0,
        any_window_open=True,
        clim_current_hvac_mode=HVAC_COOL,
    )
    cmds = zone.tick(inp)
    assert any(c.service == "turn_off" for c in cmds)
    assert zone.state.state == ZoneState.WINDOW_OPEN


def test_schedule_off_turns_off_clim():
    zone = Zone(_config())
    inp = _inputs(
        room_temperature=28.0,
        schedule_is_on=False,
        clim_current_hvac_mode=HVAC_COOL,
    )
    cmds = zone.tick(inp)
    assert any(c.service == "turn_off" for c in cmds)
    assert zone.state.state == ZoneState.SCHEDULE_OFF


def test_schedule_off_then_on_returns_to_idle():
    """Option A: schedule reopens with clim running → immediate auto resume."""
    zone = Zone(_config())
    # First tick: schedule off → SCHEDULE_OFF
    zone.tick(_inputs(schedule_is_on=False, room_temperature=22.0))
    assert zone.state.state == ZoneState.SCHEDULE_OFF
    # Schedule reopens, T° in range → should become IDLE
    cmds = zone.tick(_inputs(schedule_is_on=True, room_temperature=22.0))
    assert zone.state.state == ZoneState.IDLE
    # No active commands since T° in range
    assert not any(c.service == "set_temperature" for c in cmds)


# === Mode OFF ===


def test_mode_off_kills_clim():
    zone = Zone(_config())
    zone.set_mode(ZoneMode.OFF, now_ts=0.0)
    inp = _inputs(room_temperature=28.0, clim_current_hvac_mode=HVAC_COOL)
    cmds = zone.tick(inp)
    assert any(c.service == "turn_off" for c in cmds)


# === Boost ===


def test_boost_uses_strong_offset_and_max_fan():
    zone = Zone(_config())
    zone.trigger_boost(now_ts=1_000_000.0)
    inp = _inputs(
        room_temperature=28.0,
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=HVAC_OFF,
    )
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    fan = next((c.data.get("fan_mode") for c in cmds if c.service == "set_fan_mode"), None)
    assert sp is not None
    assert sp < 27.0 - BOOST_OFFSET + 0.5  # ± rounding/clamping
    assert fan == BOOST_FAN_MODE  # cran ventilation max (Hitachi: "top")
    assert zone.state.regime == Regime.BOOST


# === Override detection ===


def test_external_override_during_schedule_sets_timed_override():
    zone = Zone(_config(override_duree_min=20))
    zone.on_external_override(now_ts=1_000_000.0, schedule_is_on=True)
    assert zone.state.state == ZoneState.MANUAL_OVERRIDE_TIMED
    assert zone.state.override_until_ts == 1_000_000.0 + 20 * 60


def test_external_override_outside_schedule_is_free():
    zone = Zone(_config())
    zone.on_external_override(now_ts=1_000_000.0, schedule_is_on=False)
    assert zone.state.state == ZoneState.MANUAL_OVERRIDE_FREE
    assert zone.state.override_until_ts is None


def test_override_does_not_pilot_the_clim():
    """During override, the coordinator must not send commands."""
    zone = Zone(_config())
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    inp = _inputs(now_ts=1_001.0)  # 1s later
    cmds = zone.tick(inp)
    # The hard-gate check returns [] for both override states
    assert cmds == []


def test_override_timed_expires_and_returns_to_idle():
    zone = Zone(_config(override_duree_min=5))
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    # Tick after expiry, with T° in range → IDLE
    inp = _inputs(now_ts=1_000.0 + 5 * 60 + 1, room_temperature=22.0)
    zone.tick(inp)
    assert zone.state.state == ZoneState.IDLE


def test_reset_override_with_clim_on_keeps_cycle_running():
    """Reported 2026-05-30: user is in override, clim is cooling, they hit
    Resume auto, the next tick turns the clim off. Auto should instead take
    over the running cycle, and cycle_started_ts must anchor at the clim's
    real last_changed (not the click time, which would lie about elapsed)."""
    zone = Zone(_config())
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    assert zone.state.state == ZoneState.MANUAL_OVERRIDE_TIMED

    # User clicks Resume auto at t=2000s; clim went 'cool' at t=1200s.
    zone.reset_override(
        now_ts=2_000.0,
        clim_current_hvac_mode=HVAC_COOL,
        clim_state_last_changed_ts=1_200.0,
    )
    assert zone.state.state == ZoneState.RUNNING
    assert zone.state.cycle_started_ts == 1_200.0


def test_reset_override_with_clim_off_goes_idle():
    """Symmetric: if the clim is off when Resume auto is pressed, IDLE is
    correct — auto re-evaluates the thresholds from a cold start."""
    zone = Zone(_config())
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    zone.reset_override(now_ts=2_000.0, clim_current_hvac_mode=HVAC_OFF)
    assert zone.state.state == ZoneState.IDLE
    assert zone.state.cycle_started_ts is None


# === Decision algorithm gates ===


def test_room_temp_below_heat_start_triggers_heat():
    zone = Zone(_config(seuil_debut_chauffage=19.0))
    inp = _inputs(room_temperature=18.5, clim_current_hvac_mode=HVAC_OFF)
    cmds = zone.tick(inp)
    assert _find_hvac(cmds) == HVAC_HEAT


def test_room_temp_above_cool_start_triggers_cool():
    zone = Zone(_config(seuil_debut_refroidissement=26.0))
    inp = _inputs(room_temperature=26.5, clim_current_hvac_mode=HVAC_OFF)
    cmds = zone.tick(inp)
    assert _find_hvac(cmds) == HVAC_COOL


def test_room_temp_in_range_stays_idle():
    zone = Zone(_config(seuil_debut_chauffage=19.0, seuil_debut_refroidissement=26.0))
    inp = _inputs(room_temperature=22.0, clim_current_hvac_mode=HVAC_OFF)
    cmds = zone.tick(inp)
    assert zone.state.state == ZoneState.IDLE
    assert not any(c.service == "set_temperature" for c in cmds)


# === Stabilization timing ===


def test_running_below_seuil_fin_transitions_to_stabilizing():
    zone = Zone(_config(seuil_fin_refroidissement=25.0))
    zone.state.state = ZoneState.RUNNING
    inp = _inputs(
        room_temperature=24.5,
        clim_internal_temperature=24.5,
        clim_current_hvac_mode=HVAC_COOL,
    )
    zone.tick(inp)
    assert zone.state.state == ZoneState.STABILIZING


def test_stabilizing_advances_to_cooldown_after_duration():
    zone = Zone(_config(duree_stabilisation_min=10))
    zone.state.state = ZoneState.STABILIZING
    zone.state.last_state_transition_ts = 1_000.0
    inp = _inputs(
        now_ts=1_000.0 + 10 * 60 + 1,
        room_temperature=24.0,
        clim_internal_temperature=24.0,
        clim_current_hvac_mode=HVAC_COOL,
    )
    zone.tick(inp)
    assert zone.state.state == ZoneState.COOLDOWN


def test_cooldown_advances_to_idle_after_duration():
    zone = Zone(_config(duree_cooldown_min=5))
    zone.state.state = ZoneState.COOLDOWN
    zone.state.last_state_transition_ts = 1_000.0
    inp = _inputs(
        now_ts=1_000.0 + 5 * 60 + 1,
        room_temperature=22.0,
        clim_current_hvac_mode=HVAC_OFF,
    )
    zone.tick(inp)
    assert zone.state.state == ZoneState.IDLE


# === Defensive ===


def test_no_room_temperature_does_nothing():
    """If all room sensors are unavailable, don't touch the clim."""
    zone = Zone(_config())
    inp = _inputs(room_temperature=None, clim_current_hvac_mode=HVAC_OFF)
    cmds = zone.tick(inp)
    # IDLE stays IDLE; no commands
    assert cmds == []


@pytest.mark.parametrize(
    ("mode", "internal", "room", "seuil_fin", "expected_sign"),
    [
        (HVAC_COOL, 26.0, 26.0, 25.0, "below"),  # cool → setpoint < internal
        (HVAC_COOL, 30.0, 30.0, 25.0, "below"),
        (HVAC_HEAT, 18.0, 18.0, 21.0, "above"),  # heat → setpoint > internal
        (HVAC_HEAT, 15.0, 15.0, 21.0, "above"),
    ],
)
def test_setpoint_sign_is_correct_per_mode(mode, internal, room, seuil_fin, expected_sign):
    config_kwargs = (
        {"seuil_fin_refroidissement": seuil_fin, "seuil_debut_refroidissement": seuil_fin}
        if mode == HVAC_COOL
        else {"seuil_fin_chauffage": seuil_fin, "seuil_debut_chauffage": seuil_fin}
    )
    zone = Zone(_config(**config_kwargs))
    zone.state.state = ZoneState.RUNNING
    inp = _inputs(
        room_temperature=room, clim_internal_temperature=internal, clim_current_hvac_mode=mode
    )
    cmds = zone.tick(inp)
    sp = _find_setpoint(cmds)
    assert sp is not None
    if expected_sign == "below":
        assert sp < internal
    else:
        assert sp > internal


# === Multi-splits (boulot : 1 zone = N splits) ===


def _setpoint_cmd(commands):
    return next((c for c in commands if c.service == "set_temperature"), None)


def test_multisplit_cool_anchors_on_coldest_split_and_targets_all():
    """Openspace = stock + reprographie. En cool, on ancre la consigne sur le
    split le plus FROID pour qu'aucun des deux ne se neutralise, et on envoie la
    commande aux deux splits d'un coup."""
    zone = Zone(
        _config(
            climate_entity="climate.split_a",
            climate_entities=["climate.split_a", "climate.split_b"],
            seuil_fin_refroidissement=25.0,
            seuil_debut_refroidissement=25.0,
            power="normal",  # offset attaque = 5
        )
    )
    zone.state.state = ZoneState.RUNNING
    inp = _inputs(
        room_temperature=28.0,
        clim_internal_temperature=26.0,  # moyenne (affichage)
        clim_internal_temperatures=(25.0, 27.0),  # le plus froid = 25
        clim_current_hvac_mode=HVAC_COOL,
        clim_setpoint_step=1.0,  # Hitachi
    )
    cmds = zone.tick(inp)
    cmd = _setpoint_cmd(cmds)
    assert cmd is not None
    # ancre = min(25, 27) = 25 ; 25 - 5 = 20 ; pas 1.0 → 20
    assert cmd.data["temperature"] == 20.0
    assert cmd.data["entity_id"] == ["climate.split_a", "climate.split_b"]


def test_multisplit_heat_anchors_on_warmest_split():
    zone = Zone(
        _config(
            climate_entity="climate.split_a",
            climate_entities=["climate.split_a", "climate.split_b"],
            seuil_debut_chauffage=22.0,
            seuil_fin_chauffage=22.0,
            power="normal",
        )
    )
    zone.state.state = ZoneState.RUNNING
    inp = _inputs(
        room_temperature=18.0,
        clim_internal_temperature=19.0,
        clim_internal_temperatures=(18.0, 20.0),  # le plus chaud = 20
        clim_current_hvac_mode=HVAC_HEAT,
        clim_setpoint_step=1.0,
    )
    cmds = zone.tick(inp)
    cmd = _setpoint_cmd(cmds)
    assert cmd is not None
    # ancre = max(18, 20) = 20 ; 20 + 5 = 25 ; pas 1.0 → 25
    assert cmd.data["temperature"] == 25.0


def test_setpoint_rounds_to_hitachi_integer_step():
    """Avec un pas de 1.0, la consigne envoyée doit être entière (Modbus)."""
    zone = Zone(_config(seuil_fin_refroidissement=25.0, seuil_debut_refroidissement=25.0))
    zone.state.state = ZoneState.RUNNING
    inp = _inputs(
        room_temperature=28.0,
        clim_internal_temperature=26.5,  # 26.5 - 5 = 21.5 → arrondi 22 au pas 1.0
        clim_current_hvac_mode=HVAC_COOL,
        clim_setpoint_step=1.0,
    )
    sp = _find_setpoint(zone.tick(inp))
    assert sp == float(round(sp)), f"consigne non entière: {sp}"


# === Reset quotidien + override persistant (boulot) ===


def test_override_until_reset_never_expires_on_timer():
    """Boulot : un collègue prend la main → l'override tient (pas de timer 30 min)."""
    zone = Zone(_config(override_until_reset=True, override_duree_min=20))
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    assert zone.state.state == ZoneState.MANUAL_OVERRIDE_TIMED
    assert zone.state.override_until_ts is None  # pas d'expiration
    # 10h plus tard : toujours en override, aucune commande émise.
    inp = _inputs(
        now_ts=1_000.0 + 10 * 3600,
        room_temperature=28.0,
        clim_current_hvac_mode=HVAC_COOL,
    )
    assert zone.tick(inp) == []
    assert zone.state.state == ZoneState.MANUAL_OVERRIDE_TIMED


def test_daily_reset_clears_override_and_restores_auto_normal():
    """Le désarmement du matin remet la zone ON + Normal et purge l'override."""
    zone = Zone(_config(override_until_reset=True, power="agressif"))
    zone.on_external_override(now_ts=1_000.0, schedule_is_on=True)
    zone.set_mode(ZoneMode.OFF, now_ts=1_000.0)  # un collègue avait aussi éteint

    zone.daily_reset(now_ts=2_000.0, default_power="normal")

    assert zone.state.mode == ZoneMode.AUTO
    assert zone.state.override_until_ts is None
    assert zone.state.state == ZoneState.IDLE
    assert zone.config.power == "normal"
    assert all(p.power == "normal" for p in zone.config.profiles)
    # Et la régulation repart : pièce chaude → cool.
    inp = _inputs(
        now_ts=2_001.0,
        room_temperature=28.0,
        clim_internal_temperature=27.0,
        clim_current_hvac_mode=HVAC_OFF,
        clim_setpoint_step=1.0,
    )
    assert _find_hvac(zone.tick(inp)) == HVAC_COOL
