"""Constants for the Climate Manager integration."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.const import Platform

DOMAIN = "climate_manager"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
]

UPDATE_INTERVAL_SECONDS = 30

# ConfigEntry.data keys
CONF_PRESENCE_ENTITY = "presence_entity"
CONF_PRESENCE_ABSENT_STATES = "presence_absent_states"
# Interrupteur maître : quand False, le composant lit tout (températures, états
# des splits) et alimente la carte, mais N'ENVOIE AUCUNE commande aux clims
# (mode observation). Le seed LOKRIS démarre à False pour voir le rendu sans
# rien piloter ; defaut True partout ailleurs (comportement historique).
CONF_CONTROL_ENABLED = "control_enabled"

# Mode pendule continu : quand True, le split RESTE allumé après avoir atteint la
# cible (consigne relâchement au lieu de turn_off). Flag système (ConfigEntry.data).
CONF_PENDULUM_IDLE = "pendulum_idle"
DEFAULT_PENDULUM_IDLE = False

# Protection hors-gel (système — ConfigEntry.data, pas par zone)
CONF_FROST_PROTECTION_ENABLED = "frost_protection_enabled"
CONF_FROST_MIN_TEMP = "frost_min_temp"       # °C : déclenche le chauffage si T° moy. ≤ ce seuil
CONF_FROST_MAX_TEMP = "frost_max_temp"       # °C : déclenche la climatisation si T° moy. ≥ ce seuil
CONF_FROST_DURATION_MIN = "frost_duration_min"  # durée FIXE du cycle hors-gel (minutes)

DEFAULT_FROST_PROTECTION_ENABLED = False
DEFAULT_FROST_MIN_TEMP = 8.0
DEFAULT_FROST_MAX_TEMP = 32.0
DEFAULT_FROST_DURATION_MIN = 120

# Cible de zone (thermostat) : demi-bande d'hystérésis. La zone se stabilise à
# target_temp, engage le froid au-dessus de target+bande, le chaud en-dessous
# de target-bande. 1.0°C = réactif sans flip-flop (le pendule garde l'unité ON).
DEFAULT_TARGET_DEADBAND = 1.0

# Zone config keys
CONF_ZONES = "zones"
CONF_ZONE_ID = "id"
CONF_ZONE_NAME = "name"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_CLIMATE_ENTITIES = "climate_entities"
CONF_TEMPERATURE_SENSORS = "temperature_sensors"
CONF_SCHEDULE_ENTITY = "schedule_entity"
CONF_WINDOW_SENSORS = "window_sensors"
CONF_SEUIL_DEBUT_CHAUFFAGE = "seuil_debut_chauffage"
CONF_SEUIL_FIN_CHAUFFAGE = "seuil_fin_chauffage"
CONF_SEUIL_DEBUT_REFROIDISSEMENT = "seuil_debut_refroidissement"
CONF_SEUIL_FIN_REFROIDISSEMENT = "seuil_fin_refroidissement"
CONF_DUREE_STABILISATION_MIN = "duree_stabilisation_min"
CONF_DUREE_COOLDOWN_MIN = "duree_cooldown_min"
CONF_OVERRIDE_DUREE_MIN = "override_duree_min"
CONF_AGGRESSIVE_WHEN_ABSENT = "aggressive_when_absent"
CONF_AGGRESSIVITY = "aggressivity"          # legacy alias
CONF_POWER = "power"
CONF_FAN_INTENSITY = "fan_intensity"

DEFAULT_AGGRESSIVITY = "normal"
DEFAULT_POWER = "normal"
DEFAULT_FAN_INTENSITY = "normal"

# Defaults
DEFAULT_SEUIL_DEBUT_CHAUFFAGE = 19.5
DEFAULT_SEUIL_FIN_CHAUFFAGE = 21.0
DEFAULT_SEUIL_DEBUT_REFROIDISSEMENT = 26.5
DEFAULT_SEUIL_FIN_REFROIDISSEMENT = 25.0
DEFAULT_DUREE_STABILISATION_MIN = 60
DEFAULT_DUREE_COOLDOWN_MIN = 10
DEFAULT_OVERRIDE_DUREE_MIN = 30

# Hard limits
MIN_SEUIL = 5.0
MAX_SEUIL = 35.0
MIN_DUREE_MIN = 0
MAX_DUREE_MIN = 240
MIN_OVERRIDE_DUREE_MIN = 5
MAX_OVERRIDE_DUREE_MIN = 240

# Algorithme — bornes consigne envoyée à la clim
CLIM_MIN_SETPOINT = 18.0
CLIM_MAX_SETPOINT = 32.0

# Architecture D (juin 2026) : ATTAQUE pendant tout le RUNNING, STABILISATION
# quand seuil_fin atteint. Plus de CROISIERE ni APPROCHE. Daikin module
# elle-même via son inverter quand sa T° interne approche la consigne — on
# ne lui retire pas la marge nous-mêmes. Les offsets ne sont conservés ici que
# pour les tests historiques ; la valeur runtime vient de POWER_PROFILES.
OFFSET_ATTAQUE = 5.0
OFFSET_STABILISATION = 0.0

# Rate limiting
RATE_LIMIT_SECONDS = 60
SETPOINT_NOOP_DELTA = 0.5  # ne pas réémettre si delta < 0.5°C

# Context tracker window
CONTEXT_WINDOW_SECONDS = 30

# Override debounce — Daikin emits brief temperature flaps (X→Y→X) when its
# integration polls the unit, with both events on the same tick. Without
# debounce, the first event trips on_external_override before the second can
# resolve it. 2s is enough to coalesce the flap; UX impact on a real user
# action is invisible.
OVERRIDE_DEBOUNCE_SECONDS = 2

# Mode boost
BOOST_DURATION_MIN = 15
BOOST_OFFSET = 5.0
# Ventilation max pendant le boost. Hitachi/Modbus expose auto/low/medium/high/top
# (la version Daikin utilisait "4"). On prend le cran le plus fort.
BOOST_FAN_MODE = "top"

# Swing : géré uniquement si la clim expose un mode confort dédié ("windnice"
# côté Daikin). Les splits Hitachi/Modbus n'ont que swing on/off → la capacité
# `supports_windnice` reste False et le composant ne touche jamais au swing
# (on laisse l'orientation manuelle des collègues tranquille).
DEFAULT_SWING_MODE = "windnice"

# Pas de consigne. Daikin = 0.5°C. Les splits Hitachi/Modbus du boulot ont un
# target_temp_step de 1.0 → on arrondit la consigne envoyée à l'entier. La vraie
# valeur est lue par tick sur l'attribut target_temp_step de chaque split
# (coordinator) ; cette constante n'est que le repli quand l'attribut manque.
DEFAULT_SETPOINT_STEP = 0.5


# === State machine ===

class ZoneState:
    """Valeurs possibles de l'état d'une zone."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STABILIZING = "stabilizing"
    COOLDOWN = "cooldown"
    SCHEDULE_OFF = "schedule_off"
    MANUAL_OVERRIDE_TIMED = "manual_override_timed"
    MANUAL_OVERRIDE_FREE = "manual_override_free"
    WINDOW_OPEN = "window_open"

    ALL: ClassVar[list[str]] = [
        IDLE, STARTING, RUNNING, STABILIZING, COOLDOWN,
        SCHEDULE_OFF, MANUAL_OVERRIDE_TIMED, MANUAL_OVERRIDE_FREE, WINDOW_OPEN,
    ]


class Regime:
    """Régime de pilotage actif."""

    NONE = "none"
    ATTAQUE = "attaque"
    STABILISATION = "stabilisation"
    BOOST = "boost"

    ALL: ClassVar[list[str]] = [NONE, ATTAQUE, STABILISATION, BOOST]


class ZoneMode:
    """Mode global d'une zone (sélecteur)."""

    AUTO = "auto"
    OFF = "off"
    BOOST = "boost"

    ALL: ClassVar[list[str]] = [AUTO, OFF, BOOST]


class Power:
    """Puissance de pilotage : contrôle uniquement le décalage de consigne envoyé
    à la clim (= à quel point on demande à la clim de turbiner). Dissociée de la
    ventilation pour permettre 'puissance agressif + ventilation douce' (chambre
    enfant qui dort)."""

    DOUX = "doux"
    NORMAL = "normal"
    AGRESSIF = "agressif"
    ALL: ClassVar[list[str]] = [DOUX, NORMAL, AGRESSIF]


class FanIntensity:
    """Intensité de ventilation : contrôle uniquement le fan_mode envoyé.
    Indépendante de la Puissance."""

    DOUX = "doux"
    NORMAL = "normal"
    FORT = "fort"
    ALL: ClassVar[list[str]] = [DOUX, NORMAL, FORT]


# Backward-compat — anciennes zones avaient une seule clé `aggressivity`.
class Aggressivity:
    DOUX = "doux"
    NORMAL = "normal"
    AGRESSIF = "agressif"
    ALL: ClassVar[list[str]] = [DOUX, NORMAL, AGRESSIF]


# Offsets °C par régime, signe appliqué au moment du pilotage selon hvac_mode.
# "release" : offset pendule — consigne de relâchement quand la cible est atteinte.
# En cool : consigne = ancre + release (AU-DESSUS de la sonde → split allumé mais idle).
# En heat : consigne = ancre - release (EN-DESSOUS de la sonde).
POWER_PROFILES: dict[str, dict] = {
    "doux":     {"attaque": 3.0, "stabilisation": 1.0, "release": 2.0},
    "normal":   {"attaque": 5.0, "stabilisation": 1.5, "release": 3.0},
    "agressif": {"attaque": 7.0, "stabilisation": 2.0, "release": 4.0},
}

# fan_mode par régime. Valeurs Hitachi/Modbus : auto, low, medium, high, top
# (la version Daikin utilisait quiet/auto/1..5). "attaque" = ventilation pendant
# RUNNING, "stabilisation" = ventilation douce pendant la phase de maintien.
FAN_PROFILES: dict[str, dict] = {
    "doux":   {"attaque": "low",  "stabilisation": "low"},
    "normal": {"attaque": "auto", "stabilisation": "low"},
    "fort":   {"attaque": "high", "stabilisation": "low"},
}
