"""A tiny in-process event bus so clients can watch a session without polling it.

The session emits; the CLI renders and the FastAPI service streams as SSE. Keeping this
separate from the journal matters: the journal is the durable, append-only record of what
happened, and this is the ephemeral view of what is happening. A dropped subscriber must
never be able to affect the former.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .models import serialize, utc_now


@dataclass
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, "payload": serialize(self.payload)}


class EventBus:
    """Fan-out to any number of subscribers, each with its own queue.

    Subscriber queues are bounded. A subscriber that stops draining loses its oldest
    events rather than growing without limit or blocking the session that is emitting.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue[Event | None]] = []
        self._closed = False

    def emit(self, kind: str, **payload: Any) -> Event:
        event = Event(kind=kind, payload=payload)
        for queue in list(self._subscribers):
            if queue.full():
                # Drop the oldest to make room; a slow reader degrades its own view only.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race with a fast reader
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - race with a fast reader
                pass
        return event

    def subscribe(self) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(queue)
        if self._closed:
            queue.put_nowait(None)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def close(self) -> None:
        """Signal end-of-stream to every subscriber with a sentinel `None`."""
        self._closed = True
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover
                pass
