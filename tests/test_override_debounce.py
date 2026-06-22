"""Tests for the override-detection debounce + intent-echo filter.

These guard against the prod incident from 2026-05-30, where the Daikin BRP
integration emitted a `temperature: 24.0 → 23 → 24` flap on a routine poll,
~11 minutes after our last setpoint write. The old code reacted to the first
event, tripped MANUAL_OVERRIDE_TIMED for 30 minutes, and the cool cycle ended
in IDLE without ever reaching STABILIZING.

The pure helper `_is_echo_of_intent` is exercised here directly; the debounce
plumbing is covered by an end-to-end async test that drives the coordinator
state listener with synthetic events.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.climate_manager.const import (
    OVERRIDE_DEBOUNCE_SECONDS,
    SETPOINT_NOOP_DELTA,
    ZoneState,
)
from custom_components.climate_manager.coordinator import _is_echo_of_intent
from custom_components.climate_manager.zone import Zone, ZoneConfig


def _zone(last_sp: float | None = None, last_fan: str | None = None) -> Zone:
    cfg = ZoneConfig(
        zone_id="z1",
        name="Z1",
        climate_entity="climate.z1",
        temperature_sensors=["sensor.t"],
        schedule_entity=None,
    )
    z = Zone(cfg)
    z.state.last_setpoint_sent = last_sp
    z.state.last_fan_sent = last_fan
    return z


# ---------- pure helper ----------


def test_echo_when_setpoint_matches_within_delta() -> None:
    z = _zone(last_sp=24.0)
    assert _is_echo_of_intent(z, {"temperature": 24}) is True
    assert _is_echo_of_intent(z, {"temperature": 24.0}) is True


def test_not_echo_when_setpoint_diverges() -> None:
    z = _zone(last_sp=24.0)
    # The intermediate value in the Daikin flap that used to trip override
    assert _is_echo_of_intent(z, {"temperature": 23}) is False
    # A real user action
    assert _is_echo_of_intent(z, {"temperature": 22}) is False


def test_not_echo_when_we_never_sent_a_setpoint() -> None:
    # Fresh zone, no last_setpoint_sent → any change is a real user action.
    z = _zone(last_sp=None)
    assert _is_echo_of_intent(z, {"temperature": 24}) is False


def test_echo_requires_fan_match_when_we_set_one() -> None:
    z = _zone(last_sp=24.0, last_fan="auto")
    assert _is_echo_of_intent(z, {"temperature": 24, "fan_mode": "auto"}) is True
    assert _is_echo_of_intent(z, {"temperature": 24, "fan_mode": "quiet"}) is False


def test_echo_ignores_fan_when_we_never_set_one() -> None:
    z = _zone(last_sp=24.0, last_fan=None)
    # User-changed fan_mode flows through the cumulative-diff path, not the echo path.
    assert _is_echo_of_intent(z, {"temperature": 24, "fan_mode": "5"}) is True


def test_setpoint_noop_delta_boundary() -> None:
    z = _zone(last_sp=24.0)
    # Just inside delta → echo. Just outside → not echo.
    inside = 24.0 + (SETPOINT_NOOP_DELTA - 0.01)
    outside = 24.0 + SETPOINT_NOOP_DELTA
    assert _is_echo_of_intent(z, {"temperature": inside}) is True
    assert _is_echo_of_intent(z, {"temperature": outside}) is False


# ---------- end-to-end through coordinator ----------


@pytest.fixture
def hass_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _make_coordinator(hass):
    """Build a coordinator with one zone and skip the DataUpdateCoordinator
    init path (which wants a logger, intervals, etc. that we don't need here)."""
    from custom_components.climate_manager.context_tracker import ContextTracker
    from custom_components.climate_manager.coordinator import DelormejClimateCoordinator

    coord = DelormejClimateCoordinator.__new__(DelormejClimateCoordinator)
    coord.hass = hass
    coord._context_tracker = ContextTracker()
    coord._zones = {}
    coord._unsub_state_listener = None
    coord._pending_overrides = {}
    # async_request_refresh is normally inherited from DataUpdateCoordinator —
    # stub it so _resolve_pending_override doesn't blow up.
    coord.async_request_refresh = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    return coord


def _state(state: str, **attrs) -> SimpleNamespace:
    """Minimal stand-in for homeassistant.core.State (just .state + .attributes)."""
    return SimpleNamespace(state=state, attributes=attrs)


def _event(entity_id: str, old, new, context=None) -> SimpleNamespace:
    return SimpleNamespace(
        data={"entity_id": entity_id, "old_state": old, "new_state": new},
        context=context or SimpleNamespace(id="external-ctx"),
    )


@pytest.mark.asyncio
async def test_daikin_flap_does_not_trigger_override():
    """The 2026-05-30 incident, reproduced: 24.0 → 23 → 24 in two back-to-back
    events should resolve as 'echo' and leave the zone in RUNNING."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=24.0)
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    base = {"fan_mode": "auto", "swing_mode": "windnice"}
    # Event 1: temperature 24.0 → 23
    coord._on_clim_state_changed(
        _event("climate.z1", _state("cool", temperature=24.0, **base), _state("cool", temperature=23, **base))
    )
    # Event 2: temperature 23 → 24 (same tick)
    coord._on_clim_state_changed(
        _event("climate.z1", _state("cool", temperature=23, **base), _state("cool", temperature=24, **base))
    )
    # Let the debounce timer fire
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING, "Daikin echo flap must not trigger override"
    assert "climate.z1" not in coord._pending_overrides
    await hass.async_stop()


@pytest.mark.asyncio
async def test_real_user_setpoint_change_triggers_override():
    """A persistent setpoint change (no return-to-intent) is a real override."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=24.0)
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    base = {"fan_mode": "auto", "swing_mode": "windnice"}
    coord._on_clim_state_changed(
        _event("climate.z1", _state("cool", temperature=24.0, **base), _state("cool", temperature=22.0, **base))
    )
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    # No schedule entity configured → _read_schedule_on returns True (fail-open),
    # so the override goes to MANUAL_OVERRIDE_TIMED rather than FREE.
    assert z.state.state == ZoneState.MANUAL_OVERRIDE_TIMED, (
        "Real user setpoint change must trigger override "
        f"(state was {z.state.state})"
    )
    await hass.async_stop()


@pytest.mark.asyncio
async def test_listener_rebuild_cancels_pending_decisions():
    """Rebuilding zones (config change) must drop pending decisions so we
    don't resolve against a stale Zone reference."""
    from homeassistant.core import HomeAssistant
    hass = HomeAssistant("/tmp")
    coord = _make_coordinator(hass)
    z = _zone(last_sp=24.0)
    z.state.state = ZoneState.RUNNING
    coord._zones = {"z1": z}

    base = {"fan_mode": "auto", "swing_mode": "windnice"}
    coord._on_clim_state_changed(
        _event("climate.z1", _state("cool", temperature=24.0, **base), _state("cool", temperature=22.0, **base))
    )
    assert "climate.z1" in coord._pending_overrides
    coord._cancel_pending_overrides()
    assert coord._pending_overrides == {}
    # Wait past the original debounce — nothing should fire.
    await asyncio.sleep(OVERRIDE_DEBOUNCE_SECONDS + 0.2)
    assert z.state.state == ZoneState.RUNNING
    await hass.async_stop()
