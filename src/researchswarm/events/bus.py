"""A deliberately small in-process event bus."""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from researchswarm.models import Event

logger = logging.getLogger(__name__)
EventListener = Callable[[Event], object | Awaitable[object]]


class EventBus:
    """Dispatch events to synchronous or asynchronous listeners."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[EventListener]] = defaultdict(list)

    def subscribe(self, event_type: str, callback: EventListener) -> Callable[[], None]:
        """Subscribe a listener and return an unsubscribe function.

        Use ``*`` as the event type to observe every event.
        """
        self._listeners[event_type].append(callback)

        def unsubscribe() -> None:
            listeners = self._listeners[event_type]
            if callback in listeners:
                listeners.remove(callback)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Notify listeners without allowing listener errors to break execution."""
        listeners = [*self._listeners.get(event.event_type, ()), *self._listeners.get("*", ())]
        for listener in listeners:
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Event listener failed for %s", event.event_type)

