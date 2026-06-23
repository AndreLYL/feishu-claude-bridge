"""Engine — central asyncio orchestrator for per-session turn lifecycle.

Implements:
- Hold-until-result backpressure (C2): a queued message is written to the
  driver ONLY after the session receives a TurnResult.  Never two messages
  in flight simultaneously.
- Idle-timeout watchdog: if a session is busy and no event has been seen for
  ``idle_timeout`` seconds the watchdog force-unlocks it and emits
  TurnCancelled so downstream callers are unblocked.
"""

import asyncio
import logging
import time
from collections import deque

from cloudbridge import events

logger = logging.getLogger(__name__)


class _Session:
    """Per-session mutable state."""

    def __init__(self, name: str, driver, active: bool):
        self.name = name
        self.driver = driver
        self.active = active
        self.busy: bool = False
        self.pending: deque = deque()
        self.last_event_ts: float = time.monotonic()
        # asyncio tasks, set by Engine.start()
        self.event_task: asyncio.Task | None = None
        self.watchdog_task: asyncio.Task | None = None


class Engine:
    """Central asyncio Engine managing one or more session drivers.

    Parameters
    ----------
    health:
        A ``HealthModel`` instance; updated as session state changes.
    on_event:
        Callback ``(event) -> None`` called for every event emitted by any
        driver.  Called from within the event-consumer coroutine.
    queue_max:
        Maximum number of messages that may be queued per session while a
        turn is in flight.  If the queue is full the message is dropped and
        ``on_backpressure`` is called.
    idle_timeout:
        Seconds a busy session may be silent before the watchdog force-unlocks
        it.  Set to 0 to disable.
    on_backpressure:
        Optional ``(name: str, depth: int) -> None`` called when the per-
        session queue overflows.
    """

    def __init__(
        self,
        health,
        on_event,
        *,
        queue_max: int = 20,
        idle_timeout: float = 300,
        on_backpressure=None,
        max_sessions: int = 3,
    ):
        self.health = health
        self.on_event = on_event
        self.queue_max = queue_max
        self.idle_timeout = idle_timeout
        self.on_backpressure = on_backpressure or (lambda name, depth: None)
        self.max_sessions = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_session(self, name: str, driver, active: bool = False) -> None:
        """Register a named session with its driver."""
        s = _Session(name, driver, active)
        self._sessions[name] = s
        self.health.update_session(name, alive=True, busy=False, queue_depth=0, restarts=0)

    async def start(self) -> None:
        """Start event-consumer and watchdog coroutines for all sessions."""
        self._running = True
        for s in self._sessions.values():
            s.event_task = asyncio.create_task(self._consume(s))
            if self.idle_timeout > 0:
                s.watchdog_task = asyncio.create_task(self._watchdog(s))

    async def stop(self) -> None:
        """Cancel all per-session background tasks."""
        self._running = False
        for s in self._sessions.values():
            for task in (s.event_task, s.watchdog_task):
                if task is not None:
                    task.cancel()
            # Await cancellation to avoid ResourceWarning
            for task in (s.event_task, s.watchdog_task):
                if task is not None:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    async def submit(self, name: str, text: str) -> None:
        """Submit a user message to a session.

        If the session is idle the message is dispatched immediately.
        If the session is busy the message is enqueued.  If the pending queue
        is already full the message is dropped and ``on_backpressure`` fires.
        """
        s = self._sessions[name]
        if not s.busy:
            await self._dispatch(s, text)
        else:
            if len(s.pending) >= self.queue_max:
                self.on_backpressure(name, len(s.pending))
                return
            s.pending.append(text)
            self.health.update_session(name, queue_depth=len(s.pending))

    # ------------------------------------------------------------------
    # Multi-session management
    # ------------------------------------------------------------------

    @property
    def active_session_name(self):
        """Return the name of the session with active=True, or None."""
        for s in self._sessions.values():
            if s.active:
                return s.name
        return None

    def create_session(self, name, driver_factory):
        """Create and register a new session.

        Raises ValueError if ``name`` already exists or ``max_sessions`` is
        reached.  If the Engine is already running, starts the consumer task
        (and watchdog task if idle_timeout > 0) immediately, mirroring what
        ``start()`` does for pre-registered sessions.

        Returns the newly created driver.
        """
        if name in self._sessions:
            raise ValueError(f"session '{name}' already exists")
        if len(self._sessions) >= self.max_sessions:
            raise ValueError(f"max sessions reached ({self.max_sessions})")
        driver = driver_factory(name)
        self.add_session(name, driver, active=False)
        if self._running:
            s = self._sessions[name]
            s.event_task = asyncio.create_task(self._consume(s))
            if self.idle_timeout > 0:
                s.watchdog_task = asyncio.create_task(self._watchdog(s))
        return driver

    def switch_session(self, name):
        """Set ``name`` as the active session; deactivate all others.

        Raises ValueError if ``name`` is not registered.
        """
        if name not in self._sessions:
            raise ValueError(f"no such session '{name}'")
        for s in self._sessions.values():
            s.active = (s.name == name)

    def list_sessions(self):
        """Return a list of dicts describing all registered sessions."""
        return [
            {
                "name": s.name,
                "active": s.active,
                "busy": s.busy,
                "queue_depth": len(s.pending),
            }
            for s in self._sessions.values()
        ]

    async def delete_session(self, name):
        """Gracefully shut down and remove a session.

        Cancels the consumer task and watchdog task (matching what ``stop()``
        does), awaits driver.close(), and removes the session from the health
        model.  No-op if ``name`` is unknown.
        """
        s = self._sessions.pop(name, None)
        if s is None:
            return
        for task in (s.event_task, s.watchdog_task):
            if task is not None:
                task.cancel()
        for task in (s.event_task, s.watchdog_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await s.driver.close()
        self.health.remove_session(name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, s: _Session, text: str) -> None:
        """Mark session busy and write text to driver stdin (C2 gate)."""
        s.busy = True
        s.last_event_ts = time.monotonic()  # reset on new dispatch
        self.health.update_session(s.name, busy=True)
        await s.driver.send(text)

    async def _consume(self, s: _Session) -> None:
        """Per-session event loop.  Drains pending queue on TurnResult (C2).

        A failure in on_event for a single event does NOT kill the consumer —
        we catch per-event exceptions, log them, and continue iterating.  The
        TurnResult unlock/drain runs in a separate try so a failing on_event
        callback can never leave the session stuck busy=True.
        """
        try:
            async for ev in s.driver.events():
                # Refresh the idle-timeout clock on every event
                s.last_event_ts = time.monotonic()
                try:
                    self.on_event(ev)
                except Exception as exc:
                    logger.exception(
                        "Session %s on_event raised on %s: %s", s.name, type(ev).__name__, exc
                    )
                # Always process TurnResult bookkeeping even if on_event raised
                if isinstance(ev, events.TurnResult):
                    try:
                        await self._on_turn_result(s)
                    except Exception as exc:
                        logger.exception(
                            "Session %s _on_turn_result failed: %s", s.name, exc
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Session %s event consumer crashed: %s", s.name, exc)

    async def _on_turn_result(self, s: _Session) -> None:
        """Called (from the consumer) when a TurnResult arrives."""
        s.busy = False
        self.health.update_session(s.name, busy=False)
        if s.pending:
            nxt = s.pending.popleft()
            self.health.update_session(s.name, queue_depth=len(s.pending))
            await self._dispatch(s, nxt)

    async def _watchdog(self, s: _Session) -> None:
        """Per-session watchdog: force-unlock if busy and idle too long.

        The watchdog checks roughly every second.  When it fires it:
        1. Resets ``busy`` to False so further submits dispatch immediately.
        2. Drains one pending message if any (so callers aren't stuck).
        3. Calls ``on_event`` with ``TurnCancelled`` to notify downstream.

        Race safety: both the consumer and the watchdog may reset ``busy`` and
        drain pending.  The guard ``if not s.busy: return`` in the watchdog
        means it only acts when the consumer has NOT already drained.  Setting
        ``busy = False`` before draining ensures the consumer (which runs on
        the same event-loop thread) will see the correct state if it happens
        to receive a TurnResult concurrently — asyncio's single-threaded model
        guarantees the watchdog body runs atomically between awaits.
        """
        try:
            while True:
                await asyncio.sleep(min(1.0, self.idle_timeout / 2))
                if not s.busy:
                    continue
                elapsed = time.monotonic() - s.last_event_ts
                if elapsed < self.idle_timeout:
                    continue
                # Force-unlock the session
                logger.warning(
                    "Session %s idle for %.1fs (timeout=%.1fs) — force-unlocking",
                    s.name, elapsed, self.idle_timeout,
                )
                s.busy = False
                self.health.update_session(s.name, busy=False)
                self.on_event(events.TurnCancelled(session=s.name))
                if s.pending:
                    nxt = s.pending.popleft()
                    self.health.update_session(s.name, queue_depth=len(s.pending))
                    await self._dispatch(s, nxt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Session %s watchdog crashed: %s", s.name, exc)
