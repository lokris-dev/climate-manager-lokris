"""Tests du seed LOKRIS (9 zones réelles, multi-splits, override persistant)."""

from __future__ import annotations

from custom_components.climate_manager.seed import SEED_PRESENCE, seed_zones
from custom_components.climate_manager.zone import ZoneConfig


def test_seed_has_nine_zones():
    assert len(seed_zones()) == 9


def test_seed_zones_parse_via_zoneconfig():
    for z in seed_zones():
        cfg = ZoneConfig.from_dict(z)
        assert cfg.climate_entities  # au moins un split
        assert cfg.climate_entity == cfg.climate_entities[0]
        assert cfg.temperature_sensors
        assert cfg.override_until_reset is True


def test_seed_multisplit_zones():
    by_id = {z["id"]: ZoneConfig.from_dict(z) for z in seed_zones()}
    assert by_id["openspace"].climate_entities == [
        "climate.climatisation_stock",
        "climate.climatisation_reprographie",
    ]
    assert by_id["cuisine"].climate_entities == [
        "climate.climatisation_cuisine",
        "climate.climatisation_entree",
    ]
    assert by_id["espace_detente"].climate_entities == [
        "climate.climatisation_espace_detente",
        "climate.climatisation_developpement",
    ]


def test_seed_cooling_thresholds_from_consigne():
    by_id = {z["id"]: ZoneConfig.from_dict(z) for z in seed_zones()}
    j = by_id["bureau_jonathan"]  # consigne 23
    assert j.seuil_fin_refroidissement == 23.0
    assert j.seuil_debut_refroidissement == 24.0


def test_seed_presence_targets_ajax_alarm():
    assert SEED_PRESENCE["presence_entity"] == "alarm_control_panel.ajax_zone_1_alarm"
    assert "armed_away" in SEED_PRESENCE["presence_absent_states"]
