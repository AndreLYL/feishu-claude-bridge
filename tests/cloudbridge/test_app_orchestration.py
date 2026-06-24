"""Tests for cloudbridge.commands and gateway.render(SessionCrashed/Recovered).

Strategy:
- parse_command: pure unit tests, no async.
- route_inbound: async tests with FakeDriver (from test_engine.py) and a
  FakeFeishu / minimal gateway shim so no real subprocess is ever launched.
- gateway.render: use FakeFeishu to verify SessionCrashed/SessionRecovered
  produce send_card calls.
"""

import asyncio
import pytest

from cloudbridge import events
from cloudbridge.commands import parse_command, route_inbound
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeDriver:
    """Minimal driver that records sends and yields events from a queue."""

    def __init__(self, name="test"):
        self.name = name
        self.sends: list[str] = []
        self.started: bool = False
        self.closed: bool = False
        self.supervise_called: bool = False
        self._q: asyncio.Queue = asyncio.Queue()

    async def start(self):
        self.started = True

    async def send(self, text: str):
        self.sends.append(text)

    async def answer_permission(self, rid, allow):
        pass

    async def close(self, **kw):
        self.closed = True

    async def events(self):
        while True:
            yield await self._q.get()

    async def supervise(self, on_event, **kw):
        self.supervise_called = True
        # In tests we return immediately; the task is created but returns fast.

    def feed(self, ev):
        self._q.put_nowait(ev)


class FakeFeishu:
    """Records send_card / update_card calls."""

    def __init__(self):
        self.sends: list = []
        self.updates: list = []

    def send_card(self, card) -> str:
        self.sends.append(card)
        return "msg-fake"

    def update_card(self, mid: str, card) -> bool:
        self.updates.append((mid, card))
        return True


def make_engine(max_sessions=5):
    return Engine(health=HealthModel(), on_event=lambda e: None, max_sessions=max_sessions)


def make_gateway(loop, feishu, engine):
    gw = FeishuGateway(
        loop,
        engine.submit,
        feishu,
        InboundFilter(start_ts=0.0),
        flush_ms=10_000,
    )
    return gw


def make_driver_factory(drivers: dict):
    """Return a factory that creates FakeDrivers and records them by name."""
    def factory(name: str) -> FakeDriver:
        drv = FakeDriver(name)
        drivers[name] = drv
        return drv
    return factory


# ---------------------------------------------------------------------------
# parse_command — pure unit tests
# ---------------------------------------------------------------------------

class TestParseCommand:
    def test_new_with_name(self):
        assert parse_command("/new work2") == ("new", "work2")

    def test_new_without_name_returns_none(self):
        assert parse_command("/new") is None

    def test_switch_with_name(self):
        assert parse_command("/switch main") == ("switch", "main")

    def test_switch_without_name_returns_none(self):
        assert parse_command("/switch") is None

    def test_list_no_arg(self):
        assert parse_command("/list") == ("list", None)

    def test_list_ignores_trailing_spaces(self):
        assert parse_command("  /list  ") == ("list", None)

    def test_delete_with_name(self):
        assert parse_command("/delete old") == ("delete", "old")

    def test_delete_without_name_returns_none(self):
        assert parse_command("/delete") is None

    def test_plain_text_returns_none(self):
        assert parse_command("hello world") is None

    def test_empty_string_returns_none(self):
        assert parse_command("") is None

    def test_unknown_command_returns_none(self):
        assert parse_command("/unknown foo") is None

    def test_plain_text_starting_with_slash_prefix_returns_none(self):
        # Only exact known commands are recognised.
        assert parse_command("/foo bar baz") is None


# ---------------------------------------------------------------------------
# route_inbound — async tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_plain_text_goes_to_active_session():
    """A plain (non-slash) message must be submitted to the active session."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    drv = FakeDriver("main")
    eng.add_session("main", drv, active=True)
    await eng.start()

    await route_inbound(eng, gw, lambda n: FakeDriver(n), "hello world")
    await asyncio.sleep(0.05)

    assert drv.sends == ["hello world"]
    await eng.stop()


@pytest.mark.asyncio
async def test_route_new_creates_session_and_switches():
    """/new <name> creates a session, starts it, spawns supervise, and switches active."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    # Seed the initial "main" session so the engine is in a realistic state.
    main_drv = FakeDriver("main")
    eng.add_session("main", main_drv, active=True)
    await eng.start()

    created: dict[str, FakeDriver] = {}
    factory = make_driver_factory(created)

    await route_inbound(eng, gw, factory, "/new work2")
    await asyncio.sleep(0.05)

    # New session must exist and be the active one.
    assert "work2" in {s["name"] for s in eng.list_sessions()}
    assert eng.active_session_name == "work2"

    # The driver was started.
    assert "work2" in created
    assert created["work2"].started

    # A reply card must have been sent.
    assert len(fk.sends) >= 1

    await eng.stop()


@pytest.mark.asyncio
async def test_route_new_then_plain_text_goes_to_new_session():
    """/new work2 then a plain message must route to work2, not main."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    main_drv = FakeDriver("main")
    eng.add_session("main", main_drv, active=True)
    await eng.start()

    created: dict[str, FakeDriver] = {}
    factory = make_driver_factory(created)

    await route_inbound(eng, gw, factory, "/new work2")
    await asyncio.sleep(0.05)

    # Plain message after /new — should go to work2.
    await route_inbound(eng, gw, factory, "do the thing")
    await asyncio.sleep(0.05)

    work2_drv = created["work2"]
    assert work2_drv.sends == ["do the thing"], (
        f"expected send to work2, got main={main_drv.sends} work2={work2_drv.sends}"
    )
    assert main_drv.sends == [], "main must NOT have received the plain text message"

    await eng.stop()


@pytest.mark.asyncio
async def test_route_switch_changes_active_session():
    """/switch <name> must change the active session."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    drv_a = FakeDriver("a")
    drv_b = FakeDriver("b")
    eng.add_session("a", drv_a, active=True)
    eng.add_session("b", drv_b, active=False)
    await eng.start()

    assert eng.active_session_name == "a"

    await route_inbound(eng, gw, lambda n: FakeDriver(n), "/switch b")
    await asyncio.sleep(0.05)

    assert eng.active_session_name == "b"
    # A reply card must have been sent confirming the switch.
    assert len(fk.sends) >= 1

    await eng.stop()


@pytest.mark.asyncio
async def test_route_switch_nonexistent_sends_error():
    """/switch <unknown> must send an error reply (not raise)."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    drv = FakeDriver("main")
    eng.add_session("main", drv, active=True)
    await eng.start()

    # Should not raise — sends an error card instead.
    await route_inbound(eng, gw, lambda n: FakeDriver(n), "/switch ghost")
    await asyncio.sleep(0.05)

    # Active session must be unchanged.
    assert eng.active_session_name == "main"
    # Error card was sent.
    assert len(fk.sends) >= 1

    await eng.stop()


@pytest.mark.asyncio
async def test_route_list_sends_reply():
    """/list must send a Feishu text reply listing session names."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    eng.add_session("alpha", FakeDriver("alpha"), active=True)
    eng.add_session("beta", FakeDriver("beta"), active=False)
    await eng.start()

    await route_inbound(eng, gw, lambda n: FakeDriver(n), "/list")
    await asyncio.sleep(0.05)

    assert len(fk.sends) >= 1, "expected at least one send_card for /list"

    await eng.stop()


@pytest.mark.asyncio
async def test_route_delete_removes_session():
    """/delete <name> must remove the named session."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    eng.add_session("main", FakeDriver("main"), active=True)
    eng.add_session("work", FakeDriver("work"), active=False)
    await eng.start()

    await route_inbound(eng, gw, lambda n: FakeDriver(n), "/delete work")
    await asyncio.sleep(0.05)

    remaining = {s["name"] for s in eng.list_sessions()}
    assert "work" not in remaining
    assert "main" in remaining
    assert len(fk.sends) >= 1  # confirmation card

    await eng.stop()


# ---------------------------------------------------------------------------
# gateway.render — SessionCrashed / SessionRecovered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_session_crashed_sends_card():
    """gateway.render(SessionCrashed(...)) must send a status card via send_card."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    ev = events.SessionCrashed(session="main", restarts=4)
    await gw.render(ev)
    await asyncio.sleep(0.05)

    assert len(fk.sends) == 1, (
        f"Expected 1 send_card for SessionCrashed, got {len(fk.sends)}"
    )


@pytest.mark.asyncio
async def test_render_session_recovered_sends_card():
    """gateway.render(SessionRecovered(...)) must send a status card via send_card."""
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    eng = make_engine()
    gw = make_gateway(loop, fk, eng)

    ev = events.SessionRecovered(session="main")
    await gw.render(ev)
    await asyncio.sleep(0.05)

    assert len(fk.sends) == 1, (
        f"Expected 1 send_card for SessionRecovered, got {len(fk.sends)}"
    )
