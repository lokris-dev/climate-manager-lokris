"""Pré-refroidissement matinal.

Le gating par l'alarme coupe tout le bâtiment de ~18h à ~8h. Le
pré-refroidissement est une exception CIBLÉE à ce gating : sur une fenêtre
horaire locale et une liste de zones explicites, `system_enabled` repasse à True
même bâtiment armé, et les zones régulent vers une cible imposée.

Ces tests couvrent la fenêtre (heures, jours, passage de minuit), l'éligibilité
d'une zone, le reset au front d'ouverture, et la prise en compte de la cible
imposée au niveau zone.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from custom_components.climate_manager.const import (
    CONF_PRECOOL_ENABLED,
    CONF_PRECOOL_END_HOUR,
    CONF_PRECOOL_START_HOUR,
    CONF_PRECOOL_TARGET_TEMP,
    CONF_PRECOOL_WEEKDAYS,
    ZoneState,
)
from custom_components.climate_manager.coordinator import DelormejClimateCoordinator
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs

HVAC_OFF = "off"
HVAC_COOL = "cool"

# 2026-08-26 est un mercredi ; le 29 un samedi, le 30 un dimanche.
WED = dt.datetime(2026, 8, 26)
SAT = dt.datetime(2026, 8, 29)


def _coord(
    *,
    enabled: bool = True,
    zones: list[str] | None = None,
    start: float = 4.0,
    end: float = 8.0,
    weekdays: list[int] | None = None,
    target: float | None = 24.0,
    direction: str = "cool",
    zone_objs: dict | None = None,
):
    """Shell minimal portant les vraies méthodes precool, sans Home Assistant.

    `zones` = les zone_id qui portent le flag `precool` (le périmètre est une
    propriété de zone, pas une liste système). `zone_objs` permet de fournir des
    Zone déjà construites quand le test a besoin d'observer leur état.
    """
    data = {
        CONF_PRECOOL_ENABLED: enabled,
        CONF_PRECOOL_START_HOUR: start,
        CONF_PRECOOL_END_HOUR: end,
        CONF_PRECOOL_WEEKDAYS: weekdays if weekdays is not None else [0, 1, 2, 3, 4],
    }
    if target is not None:
        data[CONF_PRECOOL_TARGET_TEMP] = target
    if zone_objs is None:
        ids = zones if zones is not None else ["z1"]
        zone_objs = {zid: Zone(_config(zone_id=zid, precool=True)) for zid in ids}
    fake = SimpleNamespace(
        entry=SimpleNamespace(data=data),
        _zones=zone_objs,
        _precool_window_open=False,
        _precool_window_was_open=False,
        _current_system_direction=direction,
    )
    for name in (
        "_precool_enabled", "_precool_start_hour", "_precool_end_hour",
        "_precool_target_temp", "_precool_zone_ids", "_precool_weekdays",
        "_compute_precool_window", "_precool_active_for", "_tick_precool",
    ):
        setattr(fake, name, getattr(DelormejClimateCoordinator, name).__get__(fake))
    fake._compute_system_direction = lambda: direction
    return fake


def _at(day: dt.datetime, hour: int, minute: int = 0) -> dt.datetime:
    return day.replace(hour=hour, minute=minute)


# ── Fenêtre horaire ──────────────────────────────────────────────────────────

def test_window_open_inside_hours():
    c = _coord()
    assert c._compute_precool_window(_at(WED, 4)) is True
    assert c._compute_precool_window(_at(WED, 6, 30)) is True


def test_window_closed_outside_hours():
    c = _coord()
    assert c._compute_precool_window(_at(WED, 3, 59)) is False
    # La borne de fin est exclue : à 8h pile, le gating normal reprend.
    assert c._compute_precool_window(_at(WED, 8)) is False
    assert c._compute_precool_window(_at(WED, 20)) is False


def test_window_closed_on_excluded_weekday():
    """Samedi n'est pas dans la liste des jours -> pas de pré-refroidissement."""
    c = _coord()
    assert c._compute_precool_window(_at(SAT, 6)) is False


def test_window_disabled_flag():
    assert _coord(enabled=False)._compute_precool_window(_at(WED, 6)) is False


def test_window_without_zones_never_opens():
    """Aucune zone flaguée -> la fonctionnalité est inerte même activée."""
    assert _coord(zones=[])._compute_precool_window(_at(WED, 6)) is False


def test_perimeter_comes_from_zone_flags():
    c = _coord(zone_objs={
        "a": Zone(_config(zone_id="a", precool=True)),
        "b": Zone(_config(zone_id="b", precool=False)),
        "c": Zone(_config(zone_id="c", precool=True)),
    })
    assert c._precool_zone_ids() == ["a", "c"]


def test_precool_flag_round_trips_through_storage():
    """Le flag doit survivre à un reload (from_dict lit entry.options)."""
    stored = {"name": "Z", "id": "z1", "climate_entity": "climate.x", "precool": True}
    assert ZoneConfig.from_dict(stored).precool is True
    assert ZoneConfig.from_dict({**stored, "precool": False}).precool is False
    # Clé absente (zone créée avant la fonctionnalité) -> désactivé.
    stored.pop("precool")
    assert ZoneConfig.from_dict(stored).precool is False


def test_window_degenerate_when_start_equals_end():
    assert _coord(start=6.0, end=6.0)._compute_precool_window(_at(WED, 6)) is False


def test_window_wrapping_midnight_anchors_on_opening_day():
    """Fenêtre 22h->7h : les heures d'après minuit comptent pour la veille.

    Avec les jours lun-ven, la nuit du vendredi (ven 22h -> sam 7h) doit être
    couverte, alors que la nuit du samedi ne doit pas l'être.
    """
    c = _coord(start=22.0, end=7.0)
    fri, sat, sun = WED + dt.timedelta(days=2), SAT, SAT + dt.timedelta(days=1)
    assert c._compute_precool_window(_at(fri, 23)) is True    # vendredi soir
    assert c._compute_precool_window(_at(sat, 3)) is True     # rattaché au vendredi
    assert c._compute_precool_window(_at(sat, 23)) is False   # samedi soir : exclu
    assert c._compute_precool_window(_at(sun, 3)) is False    # rattaché au samedi


# ── Éligibilité d'une zone ───────────────────────────────────────────────────

def test_zone_eligible_when_flagged_and_window_open():
    c = _coord(zones=["z1", "z2"])
    c._precool_window_open = True
    assert c._precool_active_for(c._zones["z1"]) is True


def test_zone_without_the_flag_is_never_eligible():
    c = _coord(zones=["z1"])
    c._precool_window_open = True
    assert c._precool_active_for(Zone(_config(zone_id="autre", precool=False))) is False


def test_zone_not_eligible_when_window_closed():
    c = _coord(zones=["z1"])
    c._precool_window_open = False
    assert c._precool_active_for(c._zones["z1"]) is False


def test_no_preheating_in_winter():
    """Groupe en chaud -> le pré-refroidissement se neutralise.

    Sans ce garde-fou on pré-CHAUFFERAIT les bureaux à 4h du matin, ce qui n'est
    pas la fonctionnalité demandée.
    """
    c = _coord(zones=["z1"], direction="heat")
    c._precool_window_open = True
    assert c._precool_active_for(c._zones["z1"]) is False


# ── Reset au front d'ouverture ───────────────────────────────────────────────

def test_opening_edge_resets_listed_zones_only():
    """Un collègue qui coupe sa zone la veille ne doit pas neutraliser la
    fenêtre : l'ouverture de la fenêtre EST le matin, donc elle reset."""
    z1 = Zone(_config(zone_id="z1", precool=True))
    z2 = Zone(_config(zone_id="z2", precool=False))
    from custom_components.climate_manager.const import ZoneMode
    z1.set_mode(ZoneMode.OFF, 0.0)
    z2.set_mode(ZoneMode.OFF, 0.0)
    c = _coord(zone_objs={"z1": z1, "z2": z2})
    c._compute_precool_window = lambda now_local=None: True

    c._tick_precool(1000.0)

    assert z1.state.mode == ZoneMode.AUTO      # zone listée : réveillée
    assert z2.state.mode == ZoneMode.OFF       # zone hors périmètre : intacte


def test_reset_fires_only_on_the_edge_not_every_tick():
    """Sinon un collègue ne pourrait plus rien couper pendant la fenêtre."""
    from custom_components.climate_manager.const import ZoneMode
    z1 = Zone(_config(zone_id="z1", precool=True))
    c = _coord(zone_objs={"z1": z1})
    c._compute_precool_window = lambda now_local=None: True

    c._tick_precool(1000.0)                 # front d'ouverture
    z1.set_mode(ZoneMode.OFF, 1100.0)       # le collègue coupe pendant la fenêtre
    c._tick_precool(1200.0)                 # tick suivant, fenêtre toujours ouverte

    assert z1.state.mode == ZoneMode.OFF


def test_window_closing_rearms_the_edge():
    from custom_components.climate_manager.const import ZoneMode
    z1 = Zone(_config(zone_id="z1", precool=True))
    c = _coord(zone_objs={"z1": z1})
    open_now = {"v": True}
    c._compute_precool_window = lambda now_local=None: open_now["v"]

    c._tick_precool(1000.0)
    open_now["v"] = False
    c._tick_precool(2000.0)                 # fermeture
    z1.set_mode(ZoneMode.OFF, 2100.0)
    open_now["v"] = True
    c._tick_precool(3000.0)                 # nouvelle ouverture -> reset

    assert z1.state.mode == ZoneMode.AUTO


# ── Cible imposée au niveau zone ─────────────────────────────────────────────

def _config(**overrides) -> ZoneConfig:
    base = dict(
        zone_id="z1",
        name="Zone",
        climate_entity="climate.split1",
        temperature_sensors=["sensor.t1"],
        schedule_entity=None,
        seuil_debut_chauffage=20.0,
        seuil_fin_chauffage=21.0,
        seuil_debut_refroidissement=26.0,
        seuil_fin_refroidissement=25.0,
        power="doux",
        pendulum_idle=True,
        target_temp=None,
        precool=False,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def _inputs(**overrides) -> ZoneInputs:
    base = dict(
        now_ts=1000.0,
        room_temperature=26.0,
        clim_internal_temperature=26.0,
        clim_current_hvac_mode=HVAC_OFF,
        clim_current_setpoint=24.0,
        clim_current_fan_mode="low",
        clim_current_swing_mode="off",
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=True,
        system_direction=HVAC_COOL,
        clim_setpoint_step=1.0,
    )
    base.update(overrides)
    return ZoneInputs(**base)


def test_target_override_drives_the_thresholds():
    """La cible imposée dérive les 4 seuils comme le ferait la cible de zone."""
    zone = Zone(_config(target_temp=27.0))
    prof = zone._active(_inputs(target_override=24.0))
    assert prof.seuil_fin_refroidissement == 24.0
    assert prof.seuil_debut_refroidissement == 25.0


def test_zone_target_used_when_no_override():
    zone = Zone(_config(target_temp=27.0))
    prof = zone._active(_inputs(target_override=None))
    assert prof.seuil_fin_refroidissement == 27.0


def test_precool_engages_cooling_on_a_warm_room():
    """26 °C avec une cible imposée à 24 -> la zone part en froid."""
    zone = Zone(_config(target_temp=None))
    cmds = zone.tick(_inputs(room_temperature=26.0, target_override=24.0))
    assert zone.state.active_direction == HVAC_COOL
    assert any(c.service == "set_hvac_mode" and c.data.get("hvac_mode") == HVAC_COOL for c in cmds)


def test_precool_leaves_an_already_cool_room_alone():
    """24 °C pour une cible de 24 : rien à faire, on n'allume pas pour rien."""
    zone = Zone(_config(target_temp=None))
    zone.tick(_inputs(room_temperature=24.0, target_override=24.0))
    assert zone.state.active_direction is None
    assert zone.state.state == ZoneState.IDLE
