"""Régression : un réglage de pilotage (ventilation, puissance, seuils…) modifié
via les entités select/number doit survivre au reload d'options.

Bug historique : `_persist_zone_config` sauvait le champ au niveau zone mais pas
dans le tableau `profiles` persisté. À chaque update d'options, le listener
relançait `async_reload_zones` → `ZoneConfig.from_dict` reconstruisait les
profils depuis leurs valeurs d'origine et écrasait le réglage en mémoire.
Résultat concret : la ventilation « doux » choisie dans l'UI n'atteignait jamais
le split (il restait sur `auto`), car `_emit_active` lit `p.fan_intensity` sur le
profil ACTIF, pas sur `zone.config`.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climate_manager.const import CONF_ZONES
from custom_components.climate_manager.coordinator import DelormejClimateCoordinator
from custom_components.climate_manager.zone import ZoneConfig


def _zone_dict_with_profile(fan_intensity: str = "normal") -> dict:
    return {
        "id": "z1",
        "name": "Jonathan",
        "climate_entity": "climate.z1",
        "temperature_sensors": ["sensor.t"],
        "power": "doux",
        "fan_intensity": fan_intensity,
        "profiles": [
            {
                "name": "Pilotage par défaut",
                "power": "doux",
                "fan_intensity": fan_intensity,
                "seuil_debut_refroidissement": 24.0,
                "seuil_fin_refroidissement": 23.0,
            }
        ],
    }


def _fake_coord(zones: list[dict]):
    """`self` minimal pour appeler _persist_zone_config sans démarrer HA."""
    captured: dict = {}

    def _update_entry(entry, options=None):
        captured["options"] = options
        entry.options = options  # reflète l'écriture réelle de HA

    entry = SimpleNamespace(options={CONF_ZONES: zones})
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    coord = SimpleNamespace(
        entry=entry,
        hass=hass,
        _DRIVER_FIELDS=DelormejClimateCoordinator._DRIVER_FIELDS,
    )
    return coord, captured


def test_persist_propagates_driver_field_into_profiles() -> None:
    coord, captured = _fake_coord([_zone_dict_with_profile("normal")])

    DelormejClimateCoordinator._persist_zone_config(coord, "z1", fan_intensity="doux")

    persisted = captured["options"][CONF_ZONES][0]
    # Mis à jour au niveau zone ET dans le profil persisté.
    assert persisted["fan_intensity"] == "doux"
    assert persisted["profiles"][0]["fan_intensity"] == "doux"

    # Round-trip via from_dict (ce que fait le reload) : le profil actif reflète
    # bien « doux » — c'est lui que lit _emit_active pour piloter le ventilo.
    cfg = ZoneConfig.from_dict(persisted)
    assert cfg.profiles[0].fan_intensity == "doux"


def test_persist_propagates_threshold_into_profiles() -> None:
    coord, captured = _fake_coord([_zone_dict_with_profile("normal")])

    DelormejClimateCoordinator._persist_zone_config(
        coord, "z1", seuil_fin_refroidissement=22.0
    )

    persisted = captured["options"][CONF_ZONES][0]
    assert persisted["profiles"][0]["seuil_fin_refroidissement"] == 22.0
    cfg = ZoneConfig.from_dict(persisted)
    assert cfg.profiles[0].seuil_fin_refroidissement == 22.0


def test_persist_non_driver_field_leaves_profiles_untouched() -> None:
    coord, captured = _fake_coord([_zone_dict_with_profile("normal")])

    DelormejClimateCoordinator._persist_zone_config(
        coord, "z1", override_until_reset=True
    )

    persisted = captured["options"][CONF_ZONES][0]
    assert persisted["override_until_reset"] is True
    # Un champ hors pilotage ne doit pas être injecté dans les profils.
    assert "override_until_reset" not in persisted["profiles"][0]


def test_persist_without_profiles_key_is_safe() -> None:
    # Zone legacy sans tableau `profiles` explicite (synthèse via __post_init__).
    zone = {
        "id": "z1",
        "name": "Legacy",
        "climate_entity": "climate.z1",
        "temperature_sensors": ["sensor.t"],
        "fan_intensity": "normal",
    }
    coord, captured = _fake_coord([zone])

    DelormejClimateCoordinator._persist_zone_config(coord, "z1", fan_intensity="doux")

    persisted = captured["options"][CONF_ZONES][0]
    assert persisted["fan_intensity"] == "doux"
    # Pas de profils persistés → la synthèse __post_init__ lira le niveau zone.
    cfg = ZoneConfig.from_dict(persisted)
    assert cfg.profiles[0].fan_intensity == "doux"
