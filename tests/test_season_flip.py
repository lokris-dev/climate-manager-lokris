"""Bascule de saison → cibles remises au neutre (v1.4.23).

Constaté le 2026-10-08 : en passant le groupe en « hiver », les cibles d'été
(ex. 25 °C) restaient en place et devenaient des cibles de CHAUFFE. À chaque
vraie bascule froid ↔ chaud (manuelle ou auto), toutes les cibles de zone
repassent à 22 °C et les cibles par split en « hérité ».
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.climate_manager.const import (
    CONF_SEASON_MODE,
    CONF_ZONES,
    SEASON_AUTO,
    SEASON_CHANGE_NEUTRAL_TARGET,
    SEASON_SUMMER,
    SEASON_WINTER,
)
from custom_components.climate_manager.coordinator import DelormejClimateCoordinator
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

_C = DelormejClimateCoordinator


def _zone_opts() -> list[dict]:
    return [
        {
            "id": "z1",
            "name": "Openspace",
            "climate_entities": ["climate.stock", "climate.repro"],
            "temperature_sensors": ["sensor.t1"],
            "target_temp": 25.0,
            "power": "normal",
            "splits_config": {
                "climate.stock": {"target": 24.0, "power": "doux", "swing": "on"},
                "climate.repro": {"target": None, "power": None},
            },
        },
        {
            "id": "z2",
            "name": "Bureau RDC",
            "climate_entity": "climate.rdc",
            "temperature_sensors": ["sensor.t2"],
            "target_temp": 26.0,
        },
    ]


def _fake(mode: str, last: str | None, avg: float | None = None, zone_opts=None):
    """Shell minimal portant les vraies méthodes saison/reset, sans HA."""
    zone_opts = zone_opts if zone_opts is not None else _zone_opts()
    updates: list[dict] = []
    entry = SimpleNamespace(
        data={CONF_SEASON_MODE: mode}, options={CONF_ZONES: zone_opts}
    )

    def _update_entry(e, *, options=None, data=None):
        if options is not None:
            e.options = options
            updates.append(options)

    fake = SimpleNamespace(
        entry=entry,
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=_update_entry)
        ),
        _zones={z["id"]: Zone(ZoneConfig.from_dict(z)) for z in zone_opts},
        _season_direction=last,
        _season_flip_pending=False,
        _updates=updates,
    )
    fake.season_mode = _C.season_mode.__get__(fake)
    fake._building_avg_temperature = lambda: avg
    for name in ("_resolve_season_direction", "_reset_targets_for_season"):
        setattr(fake, name, getattr(_C, name).__get__(fake))
    return fake


# ── Détection de la bascule ──────────────────────────────────────────────────

def test_manual_summer_to_winter_flags_flip():
    f = _fake(SEASON_WINTER, last="cool")
    assert f._resolve_season_direction() == "heat"
    assert f._season_flip_pending is True


def test_auto_flip_on_building_temperature_flags_flip():
    f = _fake(SEASON_AUTO, last="cool", avg=19.5)
    assert f._resolve_season_direction() == "heat"
    assert f._season_flip_pending is True


def test_same_direction_does_not_flag():
    """auto(chaud) → hiver : même sens, rien à remettre à zéro."""
    f = _fake(SEASON_WINTER, last="heat")
    f._resolve_season_direction()
    assert f._season_flip_pending is False


def test_auto_inside_hysteresis_does_not_flag():
    f = _fake(SEASON_AUTO, last="cool", avg=22.0)
    assert f._resolve_season_direction() == "cool"
    assert f._season_flip_pending is False


def test_first_decision_without_memory_does_not_flag():
    """Installation fraîche / store vide : pas une bascule, on ne touche à rien."""
    f = _fake(SEASON_SUMMER, last=None)
    f._resolve_season_direction()
    assert f._season_flip_pending is False


# ── Reset des cibles ─────────────────────────────────────────────────────────

def test_reset_sets_zone_targets_to_neutral_and_splits_to_inherited():
    f = _fake(SEASON_WINTER, last="cool")
    f._reset_targets_for_season()
    z1, z2 = f._zones["z1"].config, f._zones["z2"].config
    assert z1.target_temp == z2.target_temp == SEASON_CHANGE_NEUTRAL_TARGET == 22.0
    assert all(cfg["target"] is None for cfg in z1.splits_config.values())
    # Puissance / balayage par split intacts.
    assert z1.splits_config["climate.stock"]["power"] == "doux"
    assert z1.splits_config["climate.stock"]["swing"] == "on"


def test_reset_persists_in_a_single_options_write():
    f = _fake(SEASON_WINTER, last="cool")
    f._reset_targets_for_season()
    assert len(f._updates) == 1, "une seule écriture → un seul reload des zones"
    zones = {z["id"]: z for z in f._updates[0][CONF_ZONES]}
    assert zones["z1"]["target_temp"] == 22.0
    assert zones["z2"]["target_temp"] == 22.0
    assert zones["z1"]["splits_config"]["climate.stock"] == {
        "target": None, "power": "doux", "swing": "on",
    }
    assert "splits_config" not in zones["z2"]
    assert zones["z1"]["power"] == "normal"  # le reste de la zone est préservé


# ── Bout en bout : le tick de bascule pilote déjà vers 22 °C ─────────────────

def test_flip_tick_resets_targets_before_zones_are_ticked():
    """Pièce à 23 °C, cible d'été 25, passage en hiver. Sans reset, 23 < 25 -
    bande → la zone ENGAGE la chauffe. Avec reset (22), 23 est au-dessus de la
    cible : aucune chauffe n'est engagée dès ce premier tick."""
    zone_opts = [{
        "id": "z2", "name": "Bureau RDC", "climate_entity": "climate.rdc",
        "temperature_sensors": ["sensor.t2"], "target_temp": 25.0,
        "pendulum_idle": True,
    }]
    f = _fake(SEASON_WINTER, last="cool", zone_opts=zone_opts)
    applied: list = []

    async def _apply(cmd):
        applied.append(cmd)

    async def _noop():
        return None

    f.control_enabled = lambda: True
    f._tick_frost = lambda now: None
    f._tick_precool = lambda now: None
    f._frost_active = lambda: False
    f._frost_direction = None
    f._compute_system_direction = _C._compute_system_direction.__get__(f)
    f._gather_inputs = lambda zone: ZoneInputs(
        now_ts=1_000_000.0, room_temperature=23.0, clim_internal_temperature=23.0,
        clim_current_hvac_mode="off", clim_current_setpoint=None,
        clim_current_fan_mode=None, clim_current_swing_mode=None,
        schedule_is_on=True, any_window_open=False, house_is_absent=False,
        system_direction=f._current_system_direction,
    )
    f._apply_command = _apply
    f._save_runtime_state_if_changed = _noop
    f._build_coordinator_data = lambda: {}

    asyncio.run(_C._async_update_data(f))

    zone = f._zones["z2"]
    assert f._current_system_direction == "heat"
    assert zone.config.target_temp == 22.0
    assert zone.state.active_direction is None
    assert not any(c.data.get("hvac_mode") == "heat" for c in applied)
    assert f._season_flip_pending is False
