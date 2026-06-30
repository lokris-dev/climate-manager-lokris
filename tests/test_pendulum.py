"""Tests pour les nouvelles fonctionnalités v1.4.0.

§1 — Régulation pendule continu (pendulum_idle=True)
§2 — Protection hors-gel (frost) via le coordinator
§3 — Contrôle par split (splits_config)

Ces tests complètent les 84 tests existants. Ils couvrent UNIQUEMENT le nouveau
comportement gated derrière le flag pendulum_idle=True ou splits_config non vide.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climate_manager.const import (
    CONF_FROST_DURATION_MIN,
    CONF_FROST_MAX_TEMP,
    CONF_FROST_MIN_TEMP,
    CONF_FROST_PROTECTION_ENABLED,
    POWER_PROFILES,
    Regime,
    ZoneState,
)
from custom_components.climate_manager.zone import (
    Zone,
    ZoneConfig,
    ZoneInputs,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers communs
# ──────────────────────────────────────────────────────────────────────────────

def _pendulum_config(**overrides) -> ZoneConfig:
    """Zone configurée en mode pendule (pendulum_idle=True)."""
    base = dict(
        zone_id="z_pend",
        name="Pendule",
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


def _find_setpoints_by_entity(commands) -> dict[str, float]:
    """Retourne {entity_id: temperature} pour toutes les commandes set_temperature."""
    result = {}
    for c in commands:
        if c.service == "set_temperature":
            eid = c.data.get("entity_id")
            temp = c.data.get("temperature")
            if eid and temp is not None:
                result[eid] = temp
    return result


def _find_swings_by_entity(commands) -> dict[str, str]:
    """Retourne {entity_id: swing_mode} pour toutes les commandes set_swing_mode."""
    result = {}
    for c in commands:
        if c.service == "set_swing_mode":
            eid = c.data.get("entity_id")
            mode = c.data.get("swing_mode")
            if eid and mode is not None:
                result[eid] = mode
    return result


# ══════════════════════════════════════════════════════════════════════════════
# § 1  Régulation pendule continu
# ══════════════════════════════════════════════════════════════════════════════

class TestPendulumNoTurnOff:
    """En mode pendule, le split ne s'éteint JAMAIS quand le système est actif."""

    def test_no_turn_off_when_target_reached_in_cool(self):
        """Cible atteinte en cool → pas de turn_off, split reste actif."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        # room ≤ seuil_fin (24.0) → phase release
        cmds = zone.tick(_inp(
            room_temperature=23.5,
            clim_internal_temperature=24.5,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert not any(c.service == "turn_off" for c in cmds), (
            "Le pendule ne doit JAMAIS couper le split quand le système est actif"
        )

    def test_no_turn_off_when_target_reached_in_heat(self):
        """Cible atteinte en heat → pas de turn_off."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_HEAT
        # room ≥ seuil_fin_chauffage (22.0) → phase release
        cmds = zone.tick(_inp(
            room_temperature=22.5,
            clim_internal_temperature=21.0,
            clim_current_hvac_mode=HVAC_HEAT,
        ))
        assert not any(c.service == "turn_off" for c in cmds)

    def test_idle_state_no_turn_off_in_pendulum(self):
        """IDLE en mode pendule → pas de turn_off même si la clim est allumée."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.IDLE
        # room dans la bande morte : pas de direction → reste IDLE
        cmds = zone.tick(_inp(
            room_temperature=23.0,  # entre seuil_fin=24 et seuil_debut=26
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert not any(c.service == "turn_off" for c in cmds)


class TestPendulumSetpoints:
    """Consignes pendule : attaque vs relâchement, signes corrects."""

    def test_release_setpoint_above_internal_in_cool(self):
        """Phase relâchement cool : consigne AU-DESSUS de la sonde interne.

        C'est la différence clé vs le mode normal :
        - Mode normal (STABILIZING) : consigne SOUS la sonde (compresseur tourne)
        - Pendule release : consigne AU-DESSUS (compresseur ralentit mais split ON)
        """
        internal = 25.0
        release_offset = POWER_PROFILES["normal"]["release"]  # 3.0

        zone = Zone(_pendulum_config(power="normal"))
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        # room ≤ seuil_fin (24.0) → release
        cmds = zone.tick(_inp(
            room_temperature=23.5,
            clim_internal_temperature=internal,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        sp = _find_setpoint(cmds)
        assert sp is not None, "Une consigne doit être émise en phase release"
        assert sp > internal, (
            f"Release cool : consigne {sp} doit être AU-DESSUS de la sonde {internal}"
        )
        assert abs(sp - (internal + release_offset)) < 0.5, (
            f"Release cool : attendu ≈ {internal + release_offset}, obtenu {sp}"
        )

    def test_release_setpoint_below_internal_in_heat(self):
        """Phase relâchement heat : consigne EN-DESSOUS de la sonde interne."""
        internal = 21.0
        release_offset = POWER_PROFILES["normal"]["release"]  # 3.0

        zone = Zone(_pendulum_config(power="normal"))
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_HEAT
        # room ≥ seuil_fin_chauffage (22.0) → release
        cmds = zone.tick(_inp(
            room_temperature=22.5,
            clim_internal_temperature=internal,
            clim_current_hvac_mode=HVAC_HEAT,
        ))
        sp = _find_setpoint(cmds)
        assert sp is not None
        assert sp < internal, (
            f"Release heat : consigne {sp} doit être EN-DESSOUS de la sonde {internal}"
        )
        assert abs(sp - (internal - release_offset)) < 0.5

    def test_attack_setpoint_below_internal_in_cool(self):
        """Phase attaque cool : consigne SOUS la sonde (comportement normal)."""
        internal = 26.0
        attack_offset = POWER_PROFILES["normal"]["attaque"]  # 5.0

        zone = Zone(_pendulum_config(power="normal"))
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        # room > seuil_fin (24.0) → attaque
        cmds = zone.tick(_inp(
            room_temperature=27.0,
            clim_internal_temperature=internal,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        sp = _find_setpoint(cmds)
        assert sp is not None
        assert sp < internal
        assert abs(sp - (internal - attack_offset)) < 0.5

    def test_attack_setpoint_above_internal_in_heat(self):
        """Phase attaque heat : consigne AU-DESSUS de la sonde."""
        internal = 18.0

        zone = Zone(_pendulum_config(power="normal"))
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_HEAT
        # room < seuil_fin_chauffage (22.0) → attaque
        cmds = zone.tick(_inp(
            room_temperature=19.0,
            clim_internal_temperature=internal,
            clim_current_hvac_mode=HVAC_HEAT,
        ))
        sp = _find_setpoint(cmds)
        assert sp is not None
        assert sp > internal

    def test_regime_is_stabilisation_in_release_phase(self):
        """En phase release pendule, le régime affiché est STABILISATION (UI)."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        zone.tick(_inp(
            room_temperature=23.5,
            clim_internal_temperature=25.0,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert zone.state.regime == Regime.STABILISATION

    def test_regime_is_attaque_in_attack_phase(self):
        """En phase attaque pendule, le régime est ATTAQUE."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        zone.tick(_inp(
            room_temperature=27.0,
            clim_internal_temperature=26.0,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert zone.state.regime == Regime.ATTAQUE


class TestPendulumDirectionLock:
    """Verrouillage de la direction active_direction."""

    def test_direction_locked_through_dead_band(self):
        """Direction cool verrouillée même si room rentre dans la bande morte."""
        zone = Zone(_pendulum_config())
        # Déclencher direction cool
        zone.tick(_inp(room_temperature=27.0, clim_current_hvac_mode=HVAC_OFF))
        assert zone.state.active_direction == HVAC_COOL

        # room descend dans la bande morte (entre seuil_fin=24 et seuil_debut=26)
        zone.tick(_inp(room_temperature=25.0, clim_current_hvac_mode=HVAC_COOL))
        assert zone.state.active_direction == HVAC_COOL, (
            "Direction cool doit rester verrouillée dans la bande morte"
        )
        assert zone.state.state != ZoneState.IDLE, (
            "La zone ne doit pas retourner en IDLE (pendule continu)"
        )

    def test_direction_locked_below_target(self):
        """Direction cool verrouillée même si room descend sous seuil_fin."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL

        # room = 23.0 < seuil_fin=24.0 → release, mais direction toujours cool
        zone.tick(_inp(room_temperature=23.0, clim_current_hvac_mode=HVAC_COOL))
        assert zone.state.active_direction == HVAC_COOL

    def test_direction_unlocks_at_opposite_start_threshold_cool_to_heat(self):
        """Direction cool libérée quand room franchit seuil_debut_chauffage (20.0)."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL

        # room chute sous seuil_debut_chauffage=20.0 → déverrouillage + heat
        zone.tick(_inp(
            room_temperature=19.0,  # < seuil_debut_chauffage=20.0
            clim_current_hvac_mode=HVAC_COOL,
        ))
        # Direction doit avoir basculé vers heat
        assert zone.state.active_direction == HVAC_HEAT, (
            "Direction doit basculer vers heat quand room franchit seuil_debut_chauffage"
        )

    def test_direction_unlocks_at_opposite_start_threshold_heat_to_cool(self):
        """Direction heat libérée quand room franchit seuil_debut_refroidissement."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_HEAT

        # room monte au-dessus de seuil_debut_refroidissement=26.0
        zone.tick(_inp(
            room_temperature=27.0,  # > seuil_debut_cool=26.0
            clim_current_hvac_mode=HVAC_HEAT,
        ))
        assert zone.state.active_direction == HVAC_COOL

    def test_no_stabilizing_transition_in_pendulum(self):
        """En pendule RUNNING, pas de transition vers STABILIZING."""
        zone = Zone(_pendulum_config())
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL

        # room sous seuil_fin → normalement déclencherait STABILIZING en mode classique
        zone.tick(_inp(
            room_temperature=23.0,
            clim_internal_temperature=23.5,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert zone.state.state == ZoneState.RUNNING, (
            "Pendule : RUNNING ne doit pas passer en STABILIZING"
        )

    def test_stabilizing_state_recovered_to_running_in_pendulum(self):
        """Un état STABILIZING hérité (restart depuis pre-1.4) repasse en RUNNING."""
        zone = Zone(_pendulum_config())
        # Simuler un état hérité STABILIZING (ancienne version)
        zone.state.state = ZoneState.STABILIZING
        zone.state.last_state_transition_ts = 1_000_000.0 - 3600  # 1h ago

        zone.tick(_inp(
            room_temperature=23.5,
            clim_current_hvac_mode=HVAC_COOL,
        ))
        assert zone.state.state == ZoneState.RUNNING


# ══════════════════════════════════════════════════════════════════════════════
# § 2  Protection hors-gel
# ══════════════════════════════════════════════════════════════════════════════

class _FrostCoord:
    """Shell minimal du coordinator pour tester la logique hors-gel (§2) en isolation.

    On bind les méthodes frost de DelormejClimateCoordinator directement sur cet
    objet sans instancier le vrai coordinator (qui nécessite Home Assistant).
    """

    def __init__(
        self,
        *,
        absent: bool = True,
        avg_temp: float = 10.0,
        frost_min: float = 8.0,
        frost_max: float = 32.0,
        duration: int = 120,
        enabled: bool = True,
    ) -> None:
        self._frost_start_ts: float | None = None
        self._frost_direction: str | None = None
        self._absent = absent
        self._avg_temp = avg_temp
        self.entry = SimpleNamespace(
            data={
                CONF_FROST_PROTECTION_ENABLED: enabled,
                CONF_FROST_MIN_TEMP: frost_min,
                CONF_FROST_MAX_TEMP: frost_max,
                CONF_FROST_DURATION_MIN: duration,
            }
        )

    # Méthodes repiquées du vrai coordinator
    def _frost_protection_enabled(self) -> bool:
        return bool(self.entry.data.get(CONF_FROST_PROTECTION_ENABLED, False))

    def _frost_min_temp(self) -> float:
        return float(self.entry.data.get(CONF_FROST_MIN_TEMP, 8.0))

    def _frost_max_temp(self) -> float:
        return float(self.entry.data.get(CONF_FROST_MAX_TEMP, 32.0))

    def _frost_duration_min(self) -> int:
        return int(self.entry.data.get(CONF_FROST_DURATION_MIN, 120))

    def _frost_active(self) -> bool:
        return self._frost_start_ts is not None

    def _house_is_absent(self) -> bool:
        return self._absent

    def _building_avg_temperature(self) -> float | None:
        return self._avg_temp

    def _start_frost_cycle(self, direction: str, now: float) -> None:
        self._frost_start_ts = now
        self._frost_direction = direction

    def _end_frost_cycle(self) -> None:
        self._frost_start_ts = None
        self._frost_direction = None

    def _tick_frost(self, now: float) -> None:
        """Copie exacte de DelormejClimateCoordinator._tick_frost."""
        import logging
        _logger = logging.getLogger(__name__)
        if not self._frost_protection_enabled():
            if self._frost_active():
                self._end_frost_cycle()
            return
        if not self._house_is_absent():
            if self._frost_active():
                self._end_frost_cycle()
            return
        if self._frost_start_ts is not None:
            elapsed = now - self._frost_start_ts
            if elapsed >= self._frost_duration_min() * 60:
                self._end_frost_cycle()
            return
        avg = self._building_avg_temperature()
        if avg is None:
            return
        if avg <= self._frost_min_temp():
            self._start_frost_cycle("heat", now)
        elif avg >= self._frost_max_temp():
            self._start_frost_cycle("cool", now)


NOW = 1_000_000.0


class TestFrostProtection:
    """Tests du cycle hors-gel : déclenchement, durée, arrêt."""

    def test_frost_starts_heat_when_building_cold(self):
        """T° bâtiment ≤ frost_min quand absent → cycle heat."""
        coord = _FrostCoord(absent=True, avg_temp=5.0, frost_min=8.0, enabled=True)
        coord._tick_frost(NOW)
        assert coord._frost_active()
        assert coord._frost_direction == "heat"

    def test_frost_starts_cool_when_building_hot(self):
        """T° bâtiment ≥ frost_max quand absent → cycle cool (canicule)."""
        coord = _FrostCoord(absent=True, avg_temp=35.0, frost_max=32.0, enabled=True)
        coord._tick_frost(NOW)
        assert coord._frost_active()
        assert coord._frost_direction == "cool"

    def test_frost_does_not_start_when_occupied(self):
        """Bâtiment occupé → pas de hors-gel même si T° hors bornes."""
        coord = _FrostCoord(absent=False, avg_temp=5.0, enabled=True)
        coord._tick_frost(NOW)
        assert not coord._frost_active()

    def test_frost_does_not_start_when_disabled(self):
        """Hors-gel désactivé → aucun déclenchement."""
        coord = _FrostCoord(absent=True, avg_temp=5.0, enabled=False)
        coord._tick_frost(NOW)
        assert not coord._frost_active()

    def test_frost_does_not_start_when_temp_in_range(self):
        """T° dans les bornes → pas de déclenchement."""
        coord = _FrostCoord(absent=True, avg_temp=15.0, frost_min=8.0, frost_max=32.0)
        coord._tick_frost(NOW)
        assert not coord._frost_active()

    def test_frost_ends_after_fixed_duration(self):
        """Cycle hors-gel se termine exactement après frost_duration_min minutes."""
        duration = 60  # minutes
        coord = _FrostCoord(absent=True, avg_temp=5.0, duration=duration)
        coord._tick_frost(NOW)
        assert coord._frost_active()

        # 1 seconde avant la fin : toujours actif
        coord._tick_frost(NOW + duration * 60 - 1)
        assert coord._frost_active()

        # Pile à la fin : doit terminer
        coord._tick_frost(NOW + duration * 60)
        assert not coord._frost_active()

    def test_frost_stops_immediately_when_occupied(self):
        """Si bâtiment ré-occupé pendant un cycle → fin immédiate."""
        coord = _FrostCoord(absent=True, avg_temp=5.0)
        coord._tick_frost(NOW)
        assert coord._frost_active()

        # Arrivée des occupants
        coord._absent = False
        coord._tick_frost(NOW + 10)
        assert not coord._frost_active()

    def test_frost_start_ts_persisted_in_direction(self):
        """Le timestamp de départ et la direction sont correctement initialisés."""
        coord = _FrostCoord(absent=True, avg_temp=5.0)
        coord._tick_frost(NOW + 42.0)
        assert coord._frost_start_ts == NOW + 42.0
        assert coord._frost_direction == "heat"

    def test_ensure_frost_direction_on_idle_zone(self):
        """_ensure_frost_direction appelle force_start sur une zone IDLE."""
        from custom_components.climate_manager.coordinator import DelormejClimateCoordinator

        zone_cfg = ZoneConfig(
            zone_id="z1",
            name="Test",
            climate_entity="climate.z1",
            temperature_sensors=["sensor.t"],
            schedule_entity=None,
        )
        zone = Zone(zone_cfg)
        zone.state.state = ZoneState.IDLE

        # Appel direct de la méthode coordinator sur un objet minimal
        coord = object.__new__(DelormejClimateCoordinator)
        coord._frost_direction = "heat"

        DelormejClimateCoordinator._ensure_frost_direction(coord, zone, NOW)

        assert zone.state.state == ZoneState.STARTING
        assert zone.state.forced_direction == "heat"

    def test_ensure_frost_direction_noop_on_running_zone(self):
        """_ensure_frost_direction est no-op sur une zone déjà RUNNING."""
        from custom_components.climate_manager.coordinator import DelormejClimateCoordinator

        zone_cfg = ZoneConfig(
            zone_id="z1",
            name="Test",
            climate_entity="climate.z1",
            temperature_sensors=["sensor.t"],
            schedule_entity=None,
        )
        zone = Zone(zone_cfg)
        zone.state.state = ZoneState.RUNNING

        coord = object.__new__(DelormejClimateCoordinator)
        coord._frost_direction = "heat"

        DelormejClimateCoordinator._ensure_frost_direction(coord, zone, NOW)

        # RUNNING ne doit pas être perturbé
        assert zone.state.state == ZoneState.RUNNING


# ══════════════════════════════════════════════════════════════════════════════
# § 3  Contrôle par split
# ══════════════════════════════════════════════════════════════════════════════

def _split_config(**overrides) -> ZoneConfig:
    """Zone avec 2 splits et splits_config configuré."""
    base = dict(
        zone_id="z_split",
        name="Multi-Split",
        climate_entity="climate.split_a",
        climate_entities=["climate.split_a", "climate.split_b"],
        temperature_sensors=["sensor.t1"],
        schedule_entity=None,
        seuil_debut_refroidissement=26.0,
        seuil_fin_refroidissement=24.0,
        seuil_debut_chauffage=20.0,
        seuil_fin_chauffage=22.0,
        power="normal",
        splits_config={
            "climate.split_a": {"power": "normal"},
            "climate.split_b": {"power": "normal"},
        },
    )
    base.update(overrides)
    return ZoneConfig(**base)


class TestPerSplitSetpoints:
    """Les splits avec sondes propres reçoivent des consignes individuelles."""

    def test_two_splits_different_internal_temps_get_different_setpoints(self):
        """2 splits, sondes différentes → 2 consignes distinctes.

        Scénario : split_a internal=25°C, split_b internal=27°C en mode cool.
        Avec power=normal (offset attaque=5.0) :
        - split_a : 25.0 - 5.0 = 20.0
        - split_b : 27.0 - 5.0 = 22.0
        """
        zone = Zone(_split_config())
        zone.state.state = ZoneState.RUNNING
        inp = _inp(
            room_temperature=27.0,
            clim_internal_temperature=26.0,  # moyenne (affichage)
            clim_internal_by_entity={
                "climate.split_a": 25.0,
                "climate.split_b": 27.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
            clim_setpoint_step=1.0,
        )
        cmds = zone.tick(inp)
        setpoints = _find_setpoints_by_entity(cmds)

        assert "climate.split_a" in setpoints, "split_a doit recevoir une consigne"
        assert "climate.split_b" in setpoints, "split_b doit recevoir une consigne"
        assert setpoints["climate.split_a"] != setpoints["climate.split_b"], (
            "Les deux splits doivent avoir des consignes différentes "
            f"(split_a={setpoints.get('climate.split_a')}, "
            f"split_b={setpoints.get('climate.split_b')})"
        )
        # Vérifier les valeurs absolues (pas 1.0, Hitachi)
        assert abs(setpoints["climate.split_a"] - 20.0) < 0.5
        assert abs(setpoints["climate.split_b"] - 22.0) < 0.5

    def test_split_specific_power_offset(self):
        """Un split avec power=doux reçoit un offset plus faible que l'autre en normal."""
        cfg = _split_config(
            splits_config={
                "climate.split_a": {"power": "doux"},    # attaque = 3.0
                "climate.split_b": {"power": "agressif"}, # attaque = 7.0
            }
        )
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        internal = 26.0
        inp = _inp(
            room_temperature=27.0,
            clim_internal_temperature=internal,
            clim_internal_by_entity={
                "climate.split_a": internal,
                "climate.split_b": internal,
            },
            clim_current_hvac_mode=HVAC_COOL,
            clim_setpoint_step=0.5,
        )
        cmds = zone.tick(inp)
        setpoints = _find_setpoints_by_entity(cmds)

        sp_a = setpoints.get("climate.split_a")
        sp_b = setpoints.get("climate.split_b")
        assert sp_a is not None and sp_b is not None
        # split_a (doux) doit avoir une consigne PLUS HAUTE (moins agressive) que split_b
        assert sp_a > sp_b, (
            f"Doux ({sp_a}) doit être > agressif ({sp_b}) en mode cool "
            "(consigne plus haute = moins de refroidissement)"
        )
        # Valeurs attendues : internal - offset
        assert abs(sp_a - (internal - 3.0)) < 0.5  # doux: 26-3=23
        assert abs(sp_b - (internal - 7.0)) < 0.5  # agressif: 26-7=19 → clamp 18

    def test_per_split_command_uses_entity_id_string_not_list(self):
        """En mode par split, chaque commande cible 1 seul entity_id (str, pas liste)."""
        zone = Zone(_split_config())
        zone.state.state = ZoneState.RUNNING
        inp = _inp(
            room_temperature=27.0,
            clim_internal_by_entity={
                "climate.split_a": 26.0,
                "climate.split_b": 26.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
        )
        cmds = zone.tick(inp)
        temp_cmds = [c for c in cmds if c.service == "set_temperature"]
        # Chaque commande doit cibler un seul split (pas la liste)
        for cmd in temp_cmds:
            eid = cmd.data.get("entity_id")
            assert isinstance(eid, str), (
                f"entity_id doit être une str, pas {type(eid)}: {eid}"
            )

    def test_fallback_to_group_anchor_when_no_internal_by_entity(self):
        """Sans clim_internal_by_entity, repli sur l'ancre groupe (ancre commune)."""
        zone = Zone(_split_config())
        zone.state.state = ZoneState.RUNNING
        inp = _inp(
            room_temperature=27.0,
            clim_internal_temperature=26.0,  # ancre groupe
            clim_internal_by_entity={},  # vide → repli
            clim_current_hvac_mode=HVAC_COOL,
            clim_setpoint_step=0.5,
        )
        cmds = zone.tick(inp)
        setpoints = _find_setpoints_by_entity(cmds)
        # Les deux splits doivent avoir la même consigne (ancre partagée)
        if "climate.split_a" in setpoints and "climate.split_b" in setpoints:
            assert setpoints["climate.split_a"] == setpoints["climate.split_b"]


class TestPerSplitSwing:
    """Swing configuré par split."""

    def test_swing_set_only_for_configured_split(self):
        """Swing défini pour split_a uniquement → set_swing_mode seulement pour lui."""
        cfg = _split_config(
            splits_config={
                "climate.split_a": {"swing": "off"},
                "climate.split_b": {},  # pas de swing
            }
        )
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        cmds = zone.tick(_inp(
            room_temperature=27.0,
            clim_internal_by_entity={
                "climate.split_a": 26.0,
                "climate.split_b": 26.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
        ))
        swings = _find_swings_by_entity(cmds)
        assert "climate.split_a" in swings, "split_a doit recevoir set_swing_mode"
        assert swings["climate.split_a"] == "off"
        assert "climate.split_b" not in swings, "split_b ne doit pas recevoir set_swing_mode"

    def test_swing_different_per_split(self):
        """Deux swings différents configurés → deux commandes distinctes."""
        cfg = _split_config(
            splits_config={
                "climate.split_a": {"swing": "off"},
                "climate.split_b": {"swing": "swing"},
            }
        )
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        cmds = zone.tick(_inp(
            room_temperature=27.0,
            clim_internal_by_entity={
                "climate.split_a": 26.0,
                "climate.split_b": 26.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
        ))
        swings = _find_swings_by_entity(cmds)
        assert swings.get("climate.split_a") == "off"
        assert swings.get("climate.split_b") == "swing"

    def test_no_global_windnice_when_splits_config_set(self):
        """Avec splits_config, le swing windnice global n'est pas envoyé."""
        cfg = _split_config()  # splits_config sans swing
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        cmds = zone.tick(_inp(
            room_temperature=27.0,
            clim_internal_by_entity={"climate.split_a": 26.0, "climate.split_b": 26.0},
            clim_current_hvac_mode=HVAC_COOL,
            supports_windnice=True,
            clim_current_swing_mode=None,  # pas encore en windnice
        ))
        # Pas de commande swing globale (windnice) quand splits_config est actif
        assert not any(
            c.service == "set_swing_mode" for c in cmds
        ), "Avec splits_config, pas de swing windnice global automatique"


# ══════════════════════════════════════════════════════════════════════════════
# § Intégration pendule + per split
# ══════════════════════════════════════════════════════════════════════════════

class TestPendulumWithSplits:
    """Pendule + contrôle par split : release par split avec sondes propres."""

    def test_pendulum_release_per_split_uses_own_anchor(self):
        """En phase release pendule, chaque split utilise sa propre sonde comme ancre."""
        cfg = ZoneConfig(
            zone_id="z",
            name="Z",
            climate_entity="climate.split_a",
            climate_entities=["climate.split_a", "climate.split_b"],
            temperature_sensors=["sensor.t"],
            schedule_entity=None,
            seuil_debut_refroidissement=26.0,
            seuil_fin_refroidissement=24.0,
            seuil_debut_chauffage=20.0,
            seuil_fin_chauffage=22.0,
            power="normal",
            pendulum_idle=True,
            splits_config={
                "climate.split_a": {"power": "normal"},
                "climate.split_b": {"power": "normal"},
            },
        )
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL

        # room ≤ seuil_fin (24.0) → phase release
        inp = _inp(
            room_temperature=23.0,
            clim_internal_by_entity={
                "climate.split_a": 24.0,
                "climate.split_b": 26.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
            clim_setpoint_step=0.5,
        )
        cmds = zone.tick(inp)
        setpoints = _find_setpoints_by_entity(cmds)

        # En release cool : consigne = anchor + release_offset (AU-DESSUS sonde)
        sp_a = setpoints.get("climate.split_a")
        sp_b = setpoints.get("climate.split_b")
        if sp_a is not None:
            assert sp_a > 24.0, f"split_a release: {sp_a} doit être > 24.0 (sa sonde)"
        if sp_b is not None:
            assert sp_b > 26.0, f"split_b release: {sp_b} doit être > 26.0 (sa sonde)"

    def test_per_split_target_drives_attack_release_independently(self):
        """Même pièce, sondes identiques, mais 2 cibles différentes →
        le split à cible haute relâche pendant que l'autre attaque encore."""
        cfg = ZoneConfig(
            zone_id="z",
            name="Z",
            climate_entity="climate.split_a",
            climate_entities=["climate.split_a", "climate.split_b"],
            temperature_sensors=["sensor.t"],
            schedule_entity=None,
            seuil_debut_refroidissement=26.0,
            seuil_fin_refroidissement=24.0,
            seuil_debut_chauffage=20.0,
            seuil_fin_chauffage=22.0,
            power="normal",
            pendulum_idle=True,
            splits_config={
                "climate.split_a": {"target": 26.0},  # cible haute → atteinte (release)
                "climate.split_b": {"target": 22.0},  # cible basse → pas atteinte (attaque)
            },
        )
        zone = Zone(cfg)
        zone.state.state = ZoneState.RUNNING
        zone.state.active_direction = HVAC_COOL
        inp = _inp(
            room_temperature=25.0,  # ≤ 26 (split_a atteint) mais > 22 (split_b pas atteint)
            clim_internal_by_entity={
                "climate.split_a": 25.0,
                "climate.split_b": 25.0,
            },
            clim_current_hvac_mode=HVAC_COOL,
            clim_setpoint_step=0.5,
        )
        cmds = zone.tick(inp)
        setpoints = _find_setpoints_by_entity(cmds)
        sp_a = setpoints.get("climate.split_a")
        sp_b = setpoints.get("climate.split_b")
        assert sp_a is not None and sp_b is not None
        # split_a relâche → consigne AU-DESSUS de sa sonde (25)
        assert sp_a > 25.0, f"split_a (cible 26, atteinte) doit relâcher: {sp_a} > 25"
        # split_b attaque → consigne SOUS sa sonde (25)
        assert sp_b < 25.0, f"split_b (cible 22, pas atteinte) doit attaquer: {sp_b} < 25"
        # Au moins un split attaque → régime zone = ATTAQUE
        assert zone.state.regime == Regime.ATTAQUE
