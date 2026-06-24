"""Tests for Task 16: end-to-end permission y/n flow.

Covers:
1. PermissionRequest event → engine records pending + gateway renders card.
2. route_inbound("y") when permission pending → driver.answer_permission(allow=True).
3. route_inbound("n") → allow=False.
4. Timeout (small permission_timeout) → auto-deny.
5. Non-y/n text while pending still routes normally (submit/command), not swallowed.
"""
import asyncio
import pytest

from cloudbridge import events
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter
from cloudbridge import commands


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDriver:
    """Records send() and answer_permission() calls; events fed manually."""

    def __init__(self, name):
        self.name = name
        self.sends: list[str] = []
        self.perm_calls: list[tuple[str, bool]] = []  # (request_id, allow)
        self._q: asyncio.Queue = asyncio.Queue()

    async def start(self):
        pass

    async def send(self, text: str):
        self.sends.append(text)

    async def answer_permission(self, request_id: str, allow: bool):
        self.perm_calls.append((request_id, allow))

    async def close(self, **kw):
        pass

    async def events(self):
        while True:
            yield await self._q.get()

    def feed(self, ev):
        self._q.put_nowait(ev)


class FakeFeishu:
    """Records send_card / update_card calls."""

    def __init__(self):
        self.sends: list = []
        self.updates: list = []

    def send_card(self, card) -> str:
        self.sends.append(card)
        return f"msg-{len(self.sends)}"

    def update_card(self, mid: str, card) -> bool:
        self.updates.append((mid, card))
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine_and_gateway(permission_timeout: float = 300.0):
    """Build a minimal Engine + FeishuGateway wired together."""
    loop = asyncio.get_event_loop()
    fk = FakeFeishu()

    rendered_events: list = []

    async def on_event_async(ev):
        rendered_events.append(ev)
        # gateway.render is called by the on_event hook below

    gw = FeishuGateway(
        loop,
        submit_coro=lambda name, text: asyncio.sleep(0),
        feishu_client=fk,
        inbound_filter=InboundFilter(start_ts=0.0),
        flush_ms=10_000,
    )

    ev_log: list = []

    def on_event(ev):
        ev_log.append(ev)
        asyncio.ensure_future(gw.render(ev))

    eng = Engine(
        health=HealthModel(),
        on_event=on_event,
        permission_timeout=permission_timeout,
    )
    return eng, gw, fk, ev_log


# ---------------------------------------------------------------------------
# Test 1: PermissionRequest records pending and renders a card
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_request_records_pending_and_renders_card():
    """Feeding a PermissionRequest event → engine records pending + gateway sends card."""
    drv = FakeDriver("s1")
    eng, gw, fk, ev_log = _make_engine_and_gateway()
    eng.add_session("s1", drv, active=True)
    await eng.start()

    perm_ev = events.PermissionRequest(
        session="s1",
        request_id="req-001",
        tool_name="bash",
        input={"command": "ls /"},
        description="List root directory",
    )
    drv.feed(perm_ev)
    await asyncio.sleep(0.05)

    # Engine must record pending
    assert eng.has_pending_permission("s1"), "engine should have recorded pending permission"

    # Gateway must have sent a card
    assert len(fk.sends) >= 1, "gateway should have sent a permission card"

    await eng.stop()
    await gw.aclose()


# ---------------------------------------------------------------------------
# Test 2: route_inbound("y") → answer_permission(allow=True)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_inbound_y_answers_allow_true():
    """route_inbound('y') when permission is pending → driver gets allow=True."""
    drv = FakeDriver("s1")
    eng, gw, fk, ev_log = _make_engine_and_gateway()
    eng.add_session("s1", drv, active=True)
    await eng.start()

    # Set up pending permission
    perm_ev = events.PermissionRequest(
        session="s1",
        request_id="req-002",
        tool_name="bash",
        input={"command": "rm -rf /"},
        description="Delete everything",
    )
    drv.feed(perm_ev)
    await asyncio.sleep(0.05)
    assert eng.has_pending_permission("s1")

    # Route "y" inbound
    await commands.route_inbound(eng, gw, lambda name: FakeDriver(name), "y")
    await asyncio.sleep(0.05)

    # Driver must have been called with allow=True
    assert drv.perm_calls, "driver.answer_permission should have been called"
    assert drv.perm_calls[-1] == ("req-002", True), (
        f"Expected ('req-002', True), got {drv.perm_calls}"
    )

    # Pending must be cleared
    assert not eng.has_pending_permission("s1"), "pending should be cleared after answer"

    # Gateway should have confirmed the action
    assert len(fk.sends) >= 2, "gateway should send a confirmation card"

    await eng.stop()
    await gw.aclose()


# ---------------------------------------------------------------------------
# Test 3: route_inbound("n") → answer_permission(allow=False)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_inbound_n_answers_allow_false():
    """route_inbound('n') when permission is pending → driver gets allow=False."""
    drv = FakeDriver("s1")
    eng, gw, fk, ev_log = _make_engine_and_gateway()
    eng.add_session("s1", drv, active=True)
    await eng.start()

    perm_ev = events.PermissionRequest(
        session="s1",
        request_id="req-003",
        tool_name="bash",
        input={"command": "echo hello"},
        description="Print hello",
    )
    drv.feed(perm_ev)
    await asyncio.sleep(0.05)
    assert eng.has_pending_permission("s1")

    await commands.route_inbound(eng, gw, lambda name: FakeDriver(name), "n")
    await asyncio.sleep(0.05)

    assert drv.perm_calls, "driver.answer_permission should have been called"
    assert drv.perm_calls[-1] == ("req-003", False), (
        f"Expected ('req-003', False), got {drv.perm_calls}"
    )
    assert not eng.has_pending_permission("s1")

    await eng.stop()
    await gw.aclose()


# ---------------------------------------------------------------------------
# Test 4: Timeout → auto-deny
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_timeout_auto_denies():
    """With a tiny permission_timeout, the engine auto-denies after the timeout."""
    drv = FakeDriver("s1")
    eng, gw, fk, ev_log = _make_engine_and_gateway(permission_timeout=0.1)
    eng.add_session("s1", drv, active=True)
    await eng.start()

    perm_ev = events.PermissionRequest(
        session="s1",
        request_id="req-timeout",
        tool_name="bash",
        input={"command": "sleep 100"},
        description="Long-running command",
    )
    drv.feed(perm_ev)
    await asyncio.sleep(0.05)
    assert eng.has_pending_permission("s1"), "should be pending right after feed"

    # Wait for timeout to fire (0.1s + buffer)
    await asyncio.sleep(0.3)

    # Pending must be cleared by auto-deny
    assert not eng.has_pending_permission("s1"), "timeout should have cleared pending"

    # Driver must have received auto-deny (allow=False)
    assert drv.perm_calls, "driver.answer_permission should have been called by timeout"
    assert drv.perm_calls[-1][1] is False, (
        f"Timeout must deny (False), got {drv.perm_calls}"
    )
    assert drv.perm_calls[-1][0] == "req-timeout"
    # Regression guard (self-cancel/self-await fix): the timeout path must deny
    # EXACTLY ONCE and leave the engine idempotent — answering again is a no-op.
    assert len(drv.perm_calls) == 1, (
        f"timeout must deny exactly once (no self-cancel double-call), got {drv.perm_calls}"
    )
    assert await eng.answer_permission("s1", allow=True) is False

    await eng.stop()
    await gw.aclose()


# ---------------------------------------------------------------------------
# Test 5: Non-y/n text while permission pending → routes normally (not swallowed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_yn_text_while_pending_routes_normally():
    """Non-y/n text while a permission is pending must not be swallowed."""
    drv = FakeDriver("s1")
    eng, gw, fk, ev_log = _make_engine_and_gateway()
    eng.add_session("s1", drv, active=True)
    await eng.start()

    perm_ev = events.PermissionRequest(
        session="s1",
        request_id="req-004",
        tool_name="bash",
        input={"command": "date"},
        description="Print date",
    )
    drv.feed(perm_ev)
    await asyncio.sleep(0.05)
    assert eng.has_pending_permission("s1")

    # Send a plain text message that is NOT y/n
    await commands.route_inbound(eng, gw, lambda name: FakeDriver(name), "hello world")
    await asyncio.sleep(0.05)

    # The message should have been submitted to the driver (not swallowed)
    assert "hello world" in drv.sends, (
        f"Non-y/n text should be submitted normally; sends={drv.sends}"
    )

    # Permission should still be pending (we did not answer it)
    assert eng.has_pending_permission("s1"), "permission should still be pending"

    # Driver should NOT have been called with answer_permission for this message
    assert not drv.perm_calls, f"No permission answer expected; got {drv.perm_calls}"

    await eng.stop()
    await gw.aclose()


# ---------------------------------------------------------------------------
# Test 6: Various y/n aliases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_yn_aliases_yes_no():
    """'yes', 'no', '是', '否' also work as y/n aliases."""
    for answer, expect_allow in [("yes", True), ("no", False), ("是", True), ("否", False)]:
        drv = FakeDriver("s1")
        eng, gw, fk, ev_log = _make_engine_and_gateway()
        eng.add_session("s1", drv, active=True)
        await eng.start()

        perm_ev = events.PermissionRequest(
            session="s1",
            request_id=f"req-alias-{answer}",
            tool_name="bash",
            input={"command": "date"},
            description="Test alias",
        )
        drv.feed(perm_ev)
        await asyncio.sleep(0.05)
        assert eng.has_pending_permission("s1")

        await commands.route_inbound(eng, gw, lambda name: FakeDriver(name), answer)
        await asyncio.sleep(0.05)

        assert drv.perm_calls, f"answer_permission not called for '{answer}'"
        assert drv.perm_calls[-1][1] is expect_allow, (
            f"'{answer}' should map to allow={expect_allow}, got {drv.perm_calls}"
        )
        assert not eng.has_pending_permission("s1")

        await eng.stop()
        await gw.aclose()
