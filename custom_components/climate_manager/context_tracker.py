"""Track HA Context IDs emitted by our integration to detect external overrides.

When the integration calls a service against a climate entity, the resulting
state_changed event carries the Context we passed. We remember its id in a
sliding window. Any state_changed for a tracked entity whose context.id is NOT
in the window is treated as an external override (user, app, remote, etc.).
"""

from __future__ import annotations

import time
from collections import deque

from homeassistant.core import Context

from .const import CONTEXT_WINDOW_SECONDS


class ContextTracker:
    """Maintains a sliding window of recently-emitted context ids."""

    def __init__(self, window_seconds: int = CONTEXT_WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._entries: deque[tuple[str, float]] = deque()

    def track(self, context: Context) -> None:
        """Remember that we emitted this context."""
        self._prune()
        self._entries.append((context.id, time.monotonic()))

    def is_ours(self, context: Context | None) -> bool:
        """Return True if the context was emitted by us within the window."""
        if context is None:
            return False
        self._prune()
        return any(cid == context.id for cid, _ in self._entries)

    def _prune(self) -> None:
        deadline = time.monotonic() - self._window_seconds
        while self._entries and self._entries[0][1] < deadline:
            self._entries.popleft()
