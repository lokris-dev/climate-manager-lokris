"""Sens global du groupe extérieur (mono-mode).

Un seul groupe extérieur = un seul mode (froid OU chaud) à la fois. Le composant
impose donc un `system_direction` unique à TOUTES les zones : une zone ne peut
qu'appeler ce sens ou rester au repos, jamais partir dans le sens opposé (sinon
le groupe se bloque). Ces tests couvrent la contrainte au niveau zone et la
résolution du sens (saison auto / été / hiver) au niveau coordinator.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climate_manager.const import (
    CONF_SEASON_MODE,
    SEASON_AUTO,
    SEASON_SUMMER,
    SEASON_WINTER,
    ZoneState,
)
from custom_components.climate_manager.coordinator import DelormejClimateCoordinator
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"


def _config(**overrides) -> ZoneConfig:
    base = dict(
        zone_id="z",
        name="Zone",
        climate_entity="climate.split1",
        temperature_sensors=["sensor.t1"],
        schedule_entity=None,
        seuil_debut_chauffage=20.0,
        seuil_fin_chauffage=22.0,
        seuil_debut_refroidissement=26.0,
        seuil_fin_refroidissement=24.0,
        power="normal",
        pendulum_idle=True,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def _inp(**overrides) -> ZoneInputs:
    base = dict(
        now_ts=1_000_000.0,
        room_temperature=25.0,
        clim_internal_temperature=25.0,
        clim_current_hvac_mode=HVAC_COOL,
        clim_current_setpoint=None,
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
    )
    base.update(overrides)
    return ZoneInputs(**base)


def _find_hvac(commands):
    for c in commands:
        if c.service == "set_hvac_mode":
            return c.data.get("hvac_mode")
    return None


# ── Contrainte au niveau zone ────────────────────────────────────────────────

def test_cool_only_cold_room_never_heats():
    """system_direction='cool' + pièce froide : la zone ne DOIT PAS chauffer."""
    zone = Zone(_config())
    zone.state.state = ZoneState.IDLE
    # 18° < seuil_debut_chauffage(20) → en temps normal la zone partirait en chaud
    cmds = zone.tick(_inp(room_temperature=18.0, clim_internal_temperature=18.0,
                          clim_current_hvac_mode=HVAC_OFF, system_direction="cool"))
    assert zone.state.active_direction != HVAC_HEAT
    assert _find_hvac(cmds) != HVAC_HEAT


def test_cool_only_warm_room_engages_cool():
    """system_direction='cool' + pièce chaude : la zone climatise."""
    zone = Zone(_config())
    zone.state.state = ZoneState.IDLE
    cmds = zone.tick(_inp(room_temperature=28.0, clim_internal_temperature=28.0,
                          clim_current_hvac_mode=HVAC_OFF, system_direction="cool"))
    assert zone.state.active_direction == HVAC_COOL
    # STARTING émet le mode froid
    assert _find_hvac(cmds) == HVAC_COOL


def test_heat_only_warm_room_never_cools():
    """system_direction='heat' + pièce chaude : la zone ne DOIT PAS refroidir."""
    zone = Zone(_config())
    zone.state.state = ZoneState.IDLE
    cmds = zone.tick(_inp(room_temperature=28.0, clim_internal_temperature=28.0,
                          clim_current_hvac_mode=HVAC_OFF, system_direction="heat"))
    assert zone.state.active_direction != HVAC_COOL
    assert _find_hvac(cmds) != HVAC_COOL


def test_heat_only_cold_room_engages_heat():
    """system_direction='heat' + pièce froide : la zone chauffe."""
    zone = Zone(_config())
    zone.state.state = ZoneState.IDLE
    cmds = zone.tick(_inp(room_temperature=18.0, clim_internal_temperature=18.0,
                          clim_current_hvac_mode=HVAC_OFF, system_direction="heat"))
    assert zone.state.active_direction == HVAC_HEAT
    assert _find_hvac(cmds) == HVAC_HEAT


def test_locked_heat_purged_when_system_flips_to_cool():
    """Une zone verrouillée en chaud voit sa direction purgée si le système passe
    en froid (elle ne doit plus jamais émettre de chaud → débloque le groupe)."""
    zone = Zone(_config())
    zone.state.state = ZoneState.RUNNING
    zone.state.active_direction = HVAC_HEAT
    # Pièce froide (18°) : sans contrainte elle resterait en chaud.
    cmds = zone.tick(_inp(room_temperature=18.0, clim_internal_temperature=18.0,
                          clim_current_hvac_mode=HVAC_HEAT, system_direction="cool"))
    assert zone.state.active_direction != HVAC_HEAT
    assert _find_hvac(cmds) != HVAC_HEAT


def test_no_constraint_is_backward_compatible():
    """system_direction=None (tests / rétro-compat) : la zone décide librement
    les deux sens comme avant."""
    zone = Zone(_config())
    zone.state.state = ZoneState.IDLE
    cmds = zone.tick(_inp(room_temperature=18.0, clim_internal_temperature=18.0,
                          clim_current_hvac_mode=HVAC_OFF, system_direction=None))
    assert zone.state.active_direction == HVAC_HEAT
    assert _find_hvac(cmds) == HVAC_HEAT


# ── Résolution du sens (saison) au niveau coordinator ────────────────────────

def _fake_coord(mode: str, avg: float | None, last: str | None = None):
    """Objet minimal portant les vraies méthodes de résolution, sans hass."""
    fake = SimpleNamespace(
        entry=SimpleNamespace(data={CONF_SEASON_MODE: mode}),
        _season_direction=last,
        _avg=avg,
    )
    fake.season_mode = DelormejClimateCoordinator.season_mode.__get__(fake)
    fake._building_avg_temperature = lambda: fake._avg
    fake._resolve_season_direction = (
        DelormejClimateCoordinator._resolve_season_direction.__get__(fake)
    )
    return fake


def test_season_summer_forces_cool():
    assert _fake_coord(SEASON_SUMMER, avg=5.0)._resolve_season_direction() == "cool"


def test_season_winter_forces_heat():
    assert _fake_coord(SEASON_WINTER, avg=35.0)._resolve_season_direction() == "heat"


def test_season_auto_hot_building_cools():
    assert _fake_coord(SEASON_AUTO, avg=26.0)._resolve_season_direction() == "cool"


def test_season_auto_cold_building_heats():
    assert _fake_coord(SEASON_AUTO, avg=17.0)._resolve_season_direction() == "heat"


def test_season_auto_middle_keeps_last_direction():
    """Entre les deux seuils (hystérésis), on garde le dernier sens."""
    assert _fake_coord(SEASON_AUTO, avg=22.0, last="heat")._resolve_season_direction() == "heat"
    assert _fake_coord(SEASON_AUTO, avg=22.0, last="cool")._resolve_season_direction() == "cool"


def test_season_auto_middle_no_memory_uses_midpoint():
    """Sans mémoire : au-dessus du milieu (22°) → froid, en-dessous → chaud."""
    assert _fake_coord(SEASON_AUTO, avg=23.9, last=None)._resolve_season_direction() == "cool"
    assert _fake_coord(SEASON_AUTO, avg=21.0, last=None)._resolve_season_direction() == "heat"
