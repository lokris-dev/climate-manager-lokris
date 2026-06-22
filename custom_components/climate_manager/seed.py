"""Pré-configuration LOKRIS (boulot).

Ce fork est dédié à l'instance Home Assistant du bureau : à l'installation, les
9 zones réelles sont créées automatiquement (mapping repris de l'ancienne
automation `climatisation_regulation_zones`), ainsi que le gating sur l'alarme
AJAX. Tout reste éditable ensuite via « Configurer ».

Rappel mapping (certains noms de splits ne collent pas aux zones — on suit le
mapping, pas les noms) :

  Openspace      = stock + reprographie   (capteur Atelier)
  Espace Détente = espace_detente + developpement (thermostat Openspace)
  Cuisine        = cuisine + entree       (thermostat Cuisine)
"""

from __future__ import annotations

from typing import Any

# Alarme AJAX : système actif uniquement quand le bâtiment est désarmé (occupé).
# Le front armé→désarmé déclenche le reset quotidien des zones.
SEED_PRESENCE: dict[str, Any] = {
    "presence_entity": "alarm_control_panel.ajax_zone_1_alarm",
    "presence_absent_states": [
        "armed_away",
        "armed_home",
        "armed_night",
        "armed_vacation",
        "arming",
        "pending",
        "triggered",
    ],
}

# (id, nom, [splits], [capteurs], consigne_cool)
_ZONES: list[tuple[str, str, list[str], list[str], float]] = [
    (
        "bureau_baptiste", "Bureau Baptiste",
        ["climate.climatisation_baptiste"],
        ["sensor.capteur_temperature_baptiste_temperature"],
        26.0,
    ),
    (
        "bureau_jonathan", "Bureau Jonathan",
        ["climate.climatisation_jonathan"],
        ["sensor.capteur_de_temperature_jonathan_temperature"],
        23.0,
    ),
    (
        "openspace", "Openspace",
        ["climate.climatisation_stock", "climate.climatisation_reprographie"],
        ["sensor.capteur_temperature_atelier_temperature"],
        25.0,
    ),
    (
        "salle_de_sport", "Salle de Sport",
        ["climate.climatisation_salle_de_sport"],
        ["sensor.capteur_temperature_salle_de_sport_temperature"],
        21.0,
    ),
    (
        "espace_detente", "Espace Détente",
        ["climate.climatisation_espace_detente", "climate.climatisation_developpement"],
        ["sensor.thermostat_openspace_air_temperature"],
        24.0,
    ),
    (
        "salle_appel", "Salle Appel",
        ["climate.climatisation_salle_d_appel"],
        ["sensor.capteur_de_temperature_salle_appel_temperature"],
        23.0,
    ),
    (
        "bureau_rdc", "Bureau RDC",
        ["climate.climatisation_bureau"],
        ["sensor.capteur_temperature_bureau_temperature"],
        22.0,
    ),
    (
        "cuisine", "Cuisine",
        ["climate.climatisation_cuisine", "climate.climatisation_entree"],
        ["sensor.thermostat_cuisine_air_temperature_2"],
        22.0,
    ),
    (
        "salle_reunion", "Salle Réunion",
        ["climate.climatisation_salle_de_reunion"],
        ["sensor.capteur_temperature_salle_de_reunion_temperature"],
        22.0,
    ),
]


def _zone_dict(
    zid: str, name: str, splits: list[str], sensors: list[str], consigne: float
) -> dict[str, Any]:
    """Construit une zone à partir de la consigne unique de l'ancien système.

    On en dérive une bande morte : on refroidit au-dessus de consigne+1, on
    s'arrête à la consigne ; chauffage seulement bien en dessous (bureau =
    refroidissement dominant). Pas de profils explicites → le composant en
    synthétise un depuis ces champs (l'intensité collègue reste pilotable).
    """
    return {
        "id": zid,
        "name": name,
        "climate_entities": splits,
        "climate_entity": splits[0],
        "temperature_sensors": sensors,
        "schedule_entity": None,
        "window_sensors": [],
        "seuil_debut_refroidissement": consigne + 1.0,
        "seuil_fin_refroidissement": float(consigne),
        "seuil_debut_chauffage": consigne - 3.0,
        "seuil_fin_chauffage": consigne - 2.0,
        "power": "normal",
        "fan_intensity": "normal",
        # Boulot : la main prise par un collègue tient jusqu'au reset du matin.
        "override_until_reset": True,
    }


def seed_zones() -> list[dict[str, Any]]:
    """Liste des 9 zones LOKRIS, prête à être stockée dans ConfigEntry.options."""
    return [_zone_dict(*z) for z in _ZONES]
