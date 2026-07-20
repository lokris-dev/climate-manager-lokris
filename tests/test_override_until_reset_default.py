"""Défaut fork LOKRIS : override jusqu'au reset du matin.

Les zones seed portent `override_until_reset: True` explicitement, mais une zone
créée via le flow d'options ne l'expose pas → `from_dict` retombe sur False
(comportement maison, timer). Sur ce fork, TOUTES les zones doivent tenir la
prise-en-main jusqu'au reset. `_rebuild_zones` applique donc le défaut fork aux
zones qui ne fixent PAS la clé, tout en respectant une valeur explicite.

Régression du 2026-07-20 : la zone « Bureau Développement » (sortie de l'Espace
Détente via le flow) était repartie en override 30 min au lieu du reset matin.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climate_manager.const import CONF_ZONES
from custom_components.climate_manager.coordinator import DelormejClimateCoordinator


def _coord(zones: list[dict], data: dict | None = None):
    """`self` minimal pour appeler _rebuild_zones sans démarrer HA."""
    entry = SimpleNamespace(data=data or {}, options={CONF_ZONES: zones})
    return SimpleNamespace(entry=entry, _zones={})


def _base_zone(**extra) -> dict:
    z = {
        "id": "z1",
        "name": "Dev",
        "climate_entity": "climate.z1",
        "temperature_sensors": ["sensor.t"],
    }
    z.update(extra)
    return z


def test_zone_without_flag_defaults_to_until_reset() -> None:
    # Zone façon "flow d'options" : pas de clé override_until_reset.
    coord = _coord([_base_zone()])
    DelormejClimateCoordinator._rebuild_zones(coord)
    assert coord._zones["z1"].config.override_until_reset is True


def test_explicit_true_is_respected() -> None:
    coord = _coord([_base_zone(override_until_reset=True)])
    DelormejClimateCoordinator._rebuild_zones(coord)
    assert coord._zones["z1"].config.override_until_reset is True


def test_explicit_false_is_respected() -> None:
    # Une valeur explicite (même False) n'est jamais écrasée par le défaut fork.
    coord = _coord([_base_zone(override_until_reset=False)])
    DelormejClimateCoordinator._rebuild_zones(coord)
    assert coord._zones["z1"].config.override_until_reset is False


def test_system_data_can_disable_default() -> None:
    # L'admin peut désactiver le défaut globalement via ConfigEntry.data.
    coord = _coord([_base_zone()], data={"override_until_reset": False})
    DelormejClimateCoordinator._rebuild_zones(coord)
    assert coord._zones["z1"].config.override_until_reset is False
