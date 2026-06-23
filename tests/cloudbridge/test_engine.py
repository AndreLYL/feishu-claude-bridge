"""Tests for cloudbridge.engine — turn lifecycle, C2 backpressure, idle-timeout watchdog."""
import asyncio
import pytest
from cloudbridge import events
from cloudbridge.engine import Engine
from cloudbridge.health import HealthModel


class FakeDriver:
    """Records send call timing; events are manually fed via .feed()."""

    def __init__(self, name):
        self.name = name
        self.sends = []
        self._q = asyncio.Queue()

    async def start(self):
        pass

    async def send(self, text):
        self.sends.append(text)

    async def answer_permission(self, rid, allow):
        pass

    async def close(self, **k):
        pass

    async def events(self):
        while True:
            yield await self._q.get()

    def feed(self, e):
        self._q.put_nowait(e)


@pytest.mark.asyncio
async def test_second_message_not_sent_until_result():
    """C2: a queued message is written to the driver ONLY after TurnResult."""
    drv = FakeDriver("main")
    eng = Engine(health=HealthModel(), on_event=lambda e: None)
    eng.add_session("main", drv, active=True)
    await eng.start()

    await eng.submit("main", "first")
    await asyncio.sleep(0.05)
    assert drv.sends == ["first"]          # first message dispatched immediately

    await eng.submit("main", "second")    # turn still in flight → queued
    await asyncio.sleep(0.05)
    assert drv.sends == ["first"]         # C2: second NOT written yet

    drv.feed(events.TurnResult(session="main"))
    await asyncio.sleep(0.05)
    assert drv.sends == ["first", "second"]  # drained only after TurnResult

    await eng.stop()


@pytest.mark.asyncio
async def test_queue_full_reports_backpressure():
    """When pending queue is full, on_backpressure fires and message is dropped."""
    reported = []
    drv = FakeDriver("main")
    eng = Engine(
        health=HealthModel(),
        on_event=lambda e: None,
        queue_max=1,
        on_backpressure=lambda name, depth: reported.append((name, depth)),
    )
    eng.add_session("main", drv, active=True)
    await eng.start()

    await eng.submit("main", "first")     # occupies the turn (busy)
    await asyncio.sleep(0.02)
    await eng.submit("main", "q1")        # fills the single pending slot
    await eng.submit("main", "overflow")  # exceeds queue_max → backpressure
    await asyncio.sleep(0.02)

    assert reported and reported[-1][0] == "main"

    await eng.stop()


@pytest.mark.asyncio
async def test_idle_timeout_unlocks_busy_session():
    """Watchdog: if a session is busy and no events arrive within idle_timeout,
    busy is reset and TurnCancelled is emitted so downstream can proceed."""
    cancelled_events = []
    drv = FakeDriver("main")
    eng = Engine(
        health=HealthModel(),
        on_event=lambda e: cancelled_events.append(e) if isinstance(e, events.TurnCancelled) else None,
        idle_timeout=0.1,  # 100 ms — fast for tests
    )
    eng.add_session("main", drv, active=True)
    await eng.start()

    await eng.submit("main", "first")     # session becomes busy
    await asyncio.sleep(0.02)
    assert drv.sends == ["first"]

    # Feed NO further events — watchdog must fire after idle_timeout
    await asyncio.sleep(0.25)             # well past 0.1 s timeout

    # busy must be reset
    sess = eng._sessions["main"]
    assert not sess.busy, "watchdog should have reset busy"

    # TurnCancelled must have been emitted
    assert any(isinstance(e, events.TurnCancelled) and e.session == "main"
               for e in cancelled_events), f"expected TurnCancelled, got {cancelled_events}"

    await eng.stop()


@pytest.mark.asyncio
async def test_watchdog_does_not_fire_while_events_flowing():
    """Watchdog must NOT fire when events keep refreshing last_event_ts."""
    cancelled_events = []
    drv = FakeDriver("main")
    eng = Engine(
        health=HealthModel(),
        on_event=lambda e: cancelled_events.append(e) if isinstance(e, events.TurnCancelled) else None,
        idle_timeout=0.15,
    )
    eng.add_session("main", drv, active=True)
    await eng.start()

    await eng.submit("main", "first")
    await asyncio.sleep(0.02)

    # Feed TextDelta events every 50 ms to keep last_event_ts fresh
    for _ in range(4):
        drv.feed(events.TextDelta(session="main", text="chunk"))
        await asyncio.sleep(0.05)

    # Still busy — no TurnCancelled yet
    assert not cancelled_events, "watchdog should not fire while events are flowing"

    # Now let idle_timeout expire
    drv.feed(events.TurnResult(session="main"))
    await asyncio.sleep(0.02)

    sess = eng._sessions["main"]
    assert not sess.busy, "session should be idle after TurnResult"
    assert not cancelled_events, "TurnCancelled must not fire on clean TurnResult"

    await eng.stop()
