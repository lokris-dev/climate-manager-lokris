"""Relâchement pendule borné par la cible (v1.4.23).

Incident du 2026-10-08, groupe passé en « hiver » : toutes les zones en maintien
ont continué à chauffer (Bureau RDC 21,9 → 26,7 °C en 1h30, consignes envoyées
26 → 27 → 28). La consigne de relâchement était `sonde - release` ; or en chauffe
la sonde du split (air repris au plafond) monte avec la chauffe elle-même
(28-30 °C pour une pièce à 22-26) : la consigne suivait, le split ne relâchait
jamais. Boucle positive, symétrique en froid quand la sonde dérive vers le bas.

v1.4.24 : heat → OFF réel groupé ; cool conserve max(sonde, cible) + release
et sa butée si la pièce passe 1 °C sous la cible en maintien.
"""

from __future__ import annotations

from custom_components.climate_manager.const import (
    CLIM_MAX_SETPOINT,
    POWER_PROFILES,
    Regime,
    ZoneState,
)
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

HVAC_HEAT = "heat"
HVAC_COOL = "cool"


def _config(**overrides) -> ZoneConfig:
    base = dict(
        zone_id="rdc",
        name="Bureau RDC",
        climate_entity="climate.rdc",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
        target_temp=21.0,
        power="doux",  # release = 2 °C
        pendulum_idle=True,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def _inp(**overrides) -> ZoneInputs:
    base = dict(
        now_ts=1_000_000.0,
        room_temperature=21.9,
        clim_internal_temperature=28.0,
        clim_current_hvac_mode=HVAC_HEAT,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
        clim_setpoint_step=1.0,
        system_direction=HVAC_HEAT,
    )
    base.update(overrides)
    return ZoneInputs(**base)


def _running(cfg: ZoneConfig, direction: str) -> Zone:
    zone = Zone(cfg)
    zone.state.state = ZoneState.RUNNING
    zone.state.active_direction = direction
    return zone


def _setpoints(cmds) -> dict:
    return {
        (c.data["entity_id"] if isinstance(c.data["entity_id"], str)
         else tuple(c.data["entity_id"])): c.data["temperature"]
        for c in cmds if c.service == "set_temperature"
    }


def _sp(cmds):
    sps = list(_setpoints(cmds).values())
    return sps[0] if sps else None


# ── Chaud : la sonde dérive vers le haut ─────────────────────────────────────

def test_incident_replay_heat_release_ignores_hot_probe():
    """Pièce 21,9 (cible 21 atteinte), sonde 28 : extinction réelle en v1.4.24."""
    zone = _running(_config(), HVAC_HEAT)
    cmds = zone.tick(_inp())
    assert any(c.data.get("hvac_mode") == "off" for c in cmds)
    assert _sp(cmds) is None
    assert zone.state.regime == Regime.STABILISATION


def test_heat_release_setpoint_does_not_follow_probe_drift():
    """Sonde 28 → 29 → 30 : le split reste OFF, aucune consigne de chauffe."""
    zone = _running(_config(), HVAC_HEAT)
    sent: list[float] = []
    current_sp = None
    for i, probe in enumerate((28.0, 29.0, 30.0)):
        cmds = zone.tick(_inp(
            now_ts=1_000_000.0 + i * 120,
            room_temperature=21.6,
            clim_internal_temperature=probe,
            clim_current_setpoint=current_sp,
            clim_current_hvac_mode=HVAC_HEAT if i == 0 else "off",
        ))
        sp = _sp(cmds)
        assert zone.state.last_hvac_sent == "off"
        if sp is not None:
            sent.append(sp)
            current_sp = sp
    assert sent == []


def test_heat_release_cuts_even_when_probe_is_colder_than_target():
    """La pièce gouverne la coupure, même quand la sonde split est plus froide."""
    zone = _running(_config(power="normal", target_temp=22.0), HVAC_HEAT)
    cmds = zone.tick(_inp(room_temperature=22.5, clim_internal_temperature=21.0,
                          clim_setpoint_step=0.5))
    assert any(c.data.get("hvac_mode") == "off" for c in cmds)
    assert _sp(cmds) is None


def test_heat_overshoot_sends_off():
    """Même le dépassement important doit couper, pas envoyer une consigne basse."""
    zone = _running(_config(), HVAC_HEAT)
    cmds = zone.tick(_inp(room_temperature=22.0, clim_internal_temperature=27.0))
    assert any(c.data.get("hvac_mode") == "off" for c in cmds)
    assert _sp(cmds) is None


def test_heat_attack_softens_near_target():
    """Sous la cible, la consigne diminue dès l'approche."""
    zone = _running(_config(), HVAC_HEAT)
    sp = _sp(zone.tick(_inp(room_temperature=20.5, clim_internal_temperature=24.0)))
    assert 21.0 <= sp < 24.0 + POWER_PROFILES["doux"]["attaque"]
    assert zone.state.regime == Regime.ATTAQUE


# ── Froid : la sonde dérive vers le bas (symétrique) ─────────────────────────

def test_cool_release_ignores_cold_probe():
    """Cible 24 atteinte (23,8), sonde refroidie à 20 : avant = 23 (sous la
    pièce → le split refroidit encore), maintenant = 24 + 2 = 26."""
    zone = _running(_config(target_temp=24.0), HVAC_COOL)
    sp = _sp(zone.tick(_inp(
        room_temperature=23.8, clim_internal_temperature=20.0,
        clim_current_hvac_mode=HVAC_COOL, system_direction=HVAC_COOL,
    )))
    assert sp == 24.0 + POWER_PROFILES["doux"]["release"]
    assert sp > 23.8 and sp > 20.0


def test_cool_overshoot_sends_clim_ceiling():
    zone = _running(_config(target_temp=24.0), HVAC_COOL)
    sp = _sp(zone.tick(_inp(
        room_temperature=22.9, clim_internal_temperature=21.0,
        clim_current_hvac_mode=HVAC_COOL, system_direction=HVAC_COOL,
    )))
    assert sp == CLIM_MAX_SETPOINT


# ── Multi-splits (chemin splits_config, celui de la prod) ────────────────────

def _multi_cfg(**overrides) -> ZoneConfig:
    return _config(
        climate_entity="climate.a",
        climate_entities=["climate.a", "climate.b"],
        splits_config={"climate.a": {}, "climate.b": {"target": 23.0}},
        **overrides,
    )


def test_per_split_heat_release_cuts_all_splits():
    """Le maintien chaud coupe tous les splits, quelles que soient leurs sondes."""
    zone = _running(_multi_cfg(), HVAC_HEAT)
    cmds = zone.tick(_inp(
        room_temperature=23.4,
        clim_internal_by_entity={"climate.a": 29.0, "climate.b": 30.0},
    ))
    assert cmds[0].data == {"entity_id": ["climate.a", "climate.b"], "hvac_mode": "off"}
    assert _setpoints(cmds) == {}
    assert zone.state.regime == Regime.STABILISATION


def test_per_split_heat_cannot_keep_heating_past_zone_target():
    zone = _running(_multi_cfg(), HVAC_HEAT)
    cmds = zone.tick(_inp(
        room_temperature=21.5,
        clim_internal_by_entity={"climate.a": 27.0, "climate.b": 27.0},
    ))
    assert cmds[0].data == {"entity_id": ["climate.a", "climate.b"], "hvac_mode": "off"}
    assert _setpoints(cmds) == {}
    assert zone.state.regime == Regime.STABILISATION
