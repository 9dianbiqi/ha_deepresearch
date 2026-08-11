"""Reusable observer implementations for typed research events."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from .contracts import ResearchEvent
from .ports import ResearchEventObserver

_LOGGER = logging.getLogger(__name__)


class NullObserver:
    """Discard every event."""

    def __call__(self, event: ResearchEvent) -> None:
        """Ignore ``event``."""


NULL_OBSERVER: ResearchEventObserver = NullObserver()


class CompositeObserver:
    """Notify child observers in order while isolating every failure."""

    def __init__(
        self,
        observers: Iterable[ResearchEventObserver] | ResearchEventObserver = (),
        *additional: ResearchEventObserver,
    ) -> None:
        """Normalize child observers into their declared notification order."""
        if callable(observers):
            self._observers = (observers, *additional)
        else:
            self._observers = (*tuple(observers), *additional)

    def __call__(self, event: ResearchEvent) -> None:
        """Notify every child in its declared order."""
        for observer in self._observers:
            try:
                observer(event)
            except Exception:
                _LOGGER.error(
                    "Research event observer failure isolated: run_id=%s sequence=%s",
                    event.run_id,
                    event.sequence,
                )


__all__ = ["CompositeObserver", "NULL_OBSERVER", "NullObserver"]
