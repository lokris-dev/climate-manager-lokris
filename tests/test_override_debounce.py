"""Tests for the override-detection debounce + value-based intent echo.

Two incidents motivate this file:

1. 2026-05-30 (Daikin) — the BRP integration emitted a `temperature 24→23→24`
   flap on a routine poll ~11 min after our last write. The old code reacted to
   the first event and tripped MANUAL_OVERRIDE_TIMED for 30 min.

2. 2026-06-30 (Hitachi/Modbus, cutover) — polling units re-emit their state
   periodically with a *fresh* context, so the ContextTracker can't catch the
   echo. With the old context-only detection, every zone the module tried to
   take over bounced straight back into MANUAL_OVERRIDE_TIMED and the module
   never managed to impose its setpoint (`last_setpoint_sent` stayed None
   because no-op commands didn't record intent).

The fix: intent is recorded on *every* tick (even no-op), and override is
detected by comparing the device's reported state to that intent (value-based),
not by context alone. The pure helper `_is_echo_of_intent` is exercised
directly; the debounce plumbing via the coordinator's state listener.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.climate_manager.const import (
    OVERRIDE_DEBOUNCE_SECONDS,
    SETPOINT_NOOP_DELTA,
    ZoneState,
)
from custom_components.climate_manager.coordinator import _is_echo_of_intent
from custom_components.climate_manager.zone import Zone, ZoneConfig, ZoneInputs


def _st(state: str, **attrs) -> SimpleNamespace:
    """Minimal stand-in for homeassistant.core.State (.state + .attributes)."""
    return SimpleNamespace(state=state, attributes=attrs)


def _zone(
    last_sp: float | None = None,
    last_fan: str | None = None,
    last_hvac: str | None = "cool",
    splits_config: dict | None = None,
) -> Zone:
    cfg = ZoneConfig(
        zone_id="z1",
        name="Z1",
        climate_entity="climate.z1",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
        splits_config=splits_config or {},
    )
    z = Zone(cfg)
    z.state.last_setpoint_sent = last_sp
    z.state.last_fan_sent = last_fan
    z.state.last_hvac_sent = last_hvac
    return z


# ---------- pure helper (value-based echo) ----------


def test_echo_when_setpoint_matches_within_delta() -> None:
    z = _zone(last_sp=24.0)
    assert _is_echo_of_intent(z, _st("cool", temperature=24)) is True
    assert _is_echo_of_intent(z, _st("cool", temperature=24.0)) is True


def test_not_echo_when_setpoint_diverges() -> None:
    z = _zone(last_sp=24.0)
    # The intermediate value in the Daikin flap that used to trip override
    assert _is_echo_of_intent(z, _st("cool", temperature=23)) is False
    # A real user action
    assert _is_echo_of_intent(z, _st("cool", temperature=22)) is False


def test_not_echo_when_mode_diverges() -> None:
    # We intend cool@24; the unit reports heat@24 → someone flipped the mode.
    z = _zone(last_sp=24.0, last_hvac="cool")
    assert _is_echo_of_intent(z, _st("heat", temperature=24)) is False


def test_not_echo_when_no_intent_at_all() -> None:
    # Fresh zone, no intent → can't be an echo (no-intent guard decides upstream).
    z = _zone(last_sp=None, last_hvac=None)
    assert _is_echo_of_intent(z, _st("cool", temperature=24)) is False


def test_echo_requires_fan_match_when_we_set_one() -> None:
    z = _zone(last_sp=24.0, last_fan="auto")
    assert _is_echo_of_intent(z, _st("cool", temperature=24, fan_mode="auto")) is True
    assert _is_echo_of_intent(z, _st("cool", temperature=24, fan_mode="quiet")) is False


def test_echo_ignores_fan_when_we_never_set_one() -> None:
    z = _zone(last_sp=24.0, last_fan=None)
    # User-changed fan flows through the cumulative-diff path, not the echo path.
    assert _is_echo_of_intent(z, _st("cool", temperature=24, fan_mode="5")) is True


def test_setpoint_noop_delta_boundary() -> None:
    z = _zone(last_sp=24.0)
    inside = 24.0 + (SETPOINT_NOOP_DELTA - 0.01)
    outside = 24.0 + SETPOINT_NOOP_DELTA
    assert _is_echo_of_intent(z, _st("cool", temperature=inside)) is True
    assert _is_echo_of_intent(z, _st("cool", temperature=outside)) is False


def test_unit_floor_rounding_within_one_step_is_echo() -> None:
    # 2026-06-30 Hitachi cutover: an aggressive 18 command is floored by the
    # unit to its effective min 19 and re-reported on a poll (fresh context).
    # With a 1° step, that 1° gap must read as an echo, not a user override.
    z = _zone(last_sp=18.0, last_hvac="cool")
    assert _is_echo_of_intent(z, _st("cool", temperature=19, target_temp_step=1.0)) is True
    # A genuine 2° change is still a divergence even with a 1° step.
    assert _is_echo_of_intent(z, _st("cool", temperature=21, target_temp_step=1.0)) is False


def test_off_intent_echoes_when_unit_off() -> None:
    # Zone intends OFF; an "off" re-emit (any context) must read as echo.
    z = _zone(last_sp=None, last_hvac="off")
    assert _is_echo_of_intent(z, _st("off")) is True
    # Colleague turns it on → not an echo (mode diverges).
    assert _is_echo_of_intent(z, _st("cool", temperature=21)) is False


def test_multi_split_echo_uses_mode_not_setpoint() -> None:
    # Multi-split: last_setpoint_sent only carries one split, so the echo check
    # must rely on mode (shared) + fan, not the setpoint of an arbitrary split.
    z = _zone(last_sp=19.0, last_hvac="cool", splits_config={"climate.a": {}, "climate.b": {}})
    # Different split reports a different (but legitimate) setpoint → still echo.
    assert _is_echo_of_intent(z, _st("cool", temperature=21)) is True
    # Mode flip still breaks the echo.
    assert _is_echo_of_intent(z, _st("heat", temperature=21)) is False


# ---------- end-to-end through the coordinator ----------


def _make_coordinator(hass):
    from custom_components.climate_manager.context_tracker import ContextTracker
    from custom_components.climate_manager.coordinator import DelormejClimateCoordinator

    coord = DelormejClimateCoordinator.__new__(DelormejClimateCoordinator)
    coord.hass = hass
    coord._context_tracker = ContextTracker()
    coord._zones = {}
    coord._unsub_state_listener = None
    coord._pending_overrides = {}
    coord.async_request_refresh = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    return coord


def _event(entity_id: str, old, new, context=None) -> SimpleNamespace:
    return SimpleNamespace(
        data={"entity_id": entity_id, "old_state": old, "new_state": new},
        context=context or SimpleNamespace(id="external-ctx"),
    )


@pytest.mark.asyncio
async def test_daikin_flap_does_not_trigger_override():
    """The 2026-05-30 incident: 24.0 → 23 → 24 resolves as echo, stays RUNNING."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=24.0)
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    base = {"fan_mode": "auto", "swing_mode": "windnice"}
    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=24.0, **base), _st("cool", temperature=23, **base))
    )
    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=23, **base), _st("cool", temperature=24, **base))
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "Daikin echo flap must not trigger override"
    assert "climate.z1" not in coord._pending_overrides
    await hass.async_stop()


@pytest.mark.asyncio
async def test_polling_reemit_to_our_intent_does_not_trigger_override():
    """2026-06-30 cutover: a polling unit jumps to OUR setpoint via a fresh
    (non-tracked) context. Value-based echo must keep the zone RUNNING."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=19.0, last_hvac="cool")  # intent established by a prior tick
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    base = {"fan_mode": "auto"}
    # Old system left it at 21; the unit now reflects our 19 — but on a poll,
    # so the context is brand-new and NOT in the tracker.
    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=21, **base), _st("cool", temperature=19, **base))
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "Reaching our own intent is not an override"
    await hass.async_stop()


@pytest.mark.asyncio
async def test_unit_floor_rounding_does_not_trigger_override():
    """End-to-end: a poll re-emitting the unit's floored setpoint (18→19, fresh
    context, step 1.0) must keep the zone RUNNING, not bounce to override."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=18.0, last_hvac="cool")
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event(
            "climate.z1",
            _st("cool", temperature=18, target_temp_step=1.0),
            _st("cool", temperature=19, target_temp_step=1.0),
        )
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "Floor-rounding by one step is not an override"
    await hass.async_stop()


@pytest.mark.asyncio
async def test_no_intent_yet_suppresses_override():
    """Cold start / takeover not yet confirmed: with no recorded intent, a
    pre-existing device state must NOT be read as a manual override (else the
    zone bounces back to override before the module can ever take it over)."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=None, last_hvac=None)  # nothing claimed yet
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=21), _st("cool", temperature=18))
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "No established intent → no override"
    await hass.async_stop()


@pytest.mark.asyncio
async def test_real_divergence_from_intent_triggers_override():
    """A persistent setpoint that diverges from our intent is a real override."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=19.0, last_hvac="cool")
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=19.0), _st("cool", temperature=24.0))
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.MANUAL_OVERRIDE_TIMED, (
        f"Real divergence must trigger override (state was {z.state.state})"
    )
    await hass.async_stop()


@pytest.mark.asyncio
async def test_listener_rebuild_cancels_pending_decisions():
    """Rebuilding zones must drop pending decisions so we don't resolve against
    a stale Zone reference."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=24.0)
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=24.0), _st("cool", temperature=22.0))
    )
    assert "climate.z1" in coord._pending_overrides
    coord._cancel_pending_overrides()
    assert coord._pending_overrides == {}
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING
    await hass.async_stop()


# ---------- zone-level: intent recorded even on a no-op tick (root cause) ----------


def test_noop_setpoint_still_records_intent():
    """The root cause of the cutover bounce: when the unit is ALREADY at the
    desired setpoint, no service call is dispatched — yet the zone must still
    record its intent, otherwise the echo guard can never recognise the polling
    re-emits and the zone bounces to override forever."""
    cfg = ZoneConfig(
        zone_id="z1",
        name="Z1",
        climate_entity="climate.z1",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
        seuil_debut_refroidissement=24.0,
        seuil_fin_refroidissement=22.0,
    )
    z = Zone(cfg)
    z.state.state = ZoneState.RUNNING
    z.state.active_direction = "cool"
    inp = ZoneInputs(
        now_ts=1_000_000.0,
        room_temperature=26.0,
        clim_internal_temperature=26.0,
        clim_current_hvac_mode="cool",
        clim_current_setpoint=None,  # filled below
        clim_current_fan_mode=None,
        clim_current_swing_mode=None,
        schedule_is_on=True,
        any_window_open=False,
        house_is_absent=False,
    )
    # First tick: compute what the module wants, then pretend the unit already
    # sits exactly there so the second tick is a pure no-op.
    cmds = z.tick(inp)
    intended = None
    for c in cmds:
        if c.service == "set_temperature":
            intended = c.data["temperature"]
    assert intended is not None
    assert z.state.last_setpoint_sent == intended
    assert z.state.last_hvac_sent == "cool"

    # Second tick: unit already at the intended setpoint → no command emitted,
    # but intent must remain recorded.
    z.state.last_setpoint_sent = None  # simulate a state that lost its memory
    inp2 = ZoneInputs(**{**inp.__dict__, "clim_current_setpoint": intended})
    cmds2 = z.tick(inp2)
    assert not any(c.service == "set_temperature" for c in cmds2), "should be a no-op"
    assert z.state.last_setpoint_sent == intended, "intent must be recorded even on no-op"
    assert z.state.last_hvac_sent == "cool"


@pytest.mark.asyncio
async def test_split_going_unavailable_does_not_trigger_override():
    """Un split qui se déconnecte (cool → unavailable) n'est pas une action
    utilisateur : aucune décision d'override ne doit être programmée."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=19.0, last_hvac="cool")
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event("climate.z1", _st("cool", temperature=19.0, fan_mode="low"), _st(STATE_UNAVAILABLE))
    )
    assert "climate.z1" not in coord._pending_overrides
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING
    await hass.async_stop()


@pytest.mark.asyncio
async def test_split_reconnecting_does_not_trigger_override():
    """2026-07-01 : à la reconnexion des clims (unavailable → unknown → cool),
    6 zones passaient à tort en override. Une SORTIE d'indisponibilité n'est pas
    une action utilisateur, même si la valeur diffère de l'intent."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=25.0, last_hvac="cool")
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    coord._on_clim_state_changed(
        _event("climate.z1", _st(STATE_UNAVAILABLE), _st(STATE_UNKNOWN))
    )
    coord._on_clim_state_changed(
        _event("climate.z1", _st(STATE_UNKNOWN), _st("cool", temperature=30.0, fan_mode="auto"))
    )
    assert "climate.z1" not in coord._pending_overrides
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "reconnexion ≠ override"
    await hass.async_stop()
