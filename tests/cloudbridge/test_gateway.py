"""Tests for cloudbridge.gateway.FeishuGateway.

Tests:
1. test_inbound_filtered_and_submitted  — duplicate msg_id is deduplicated by
   InboundFilter; only one submit call reaches the engine.
2. test_text_deltas_are_coalesced_not_per_delta — four TextDelta events produce
   exactly one card update (the final flush on TurnResult), NOT four.
   Uses a very large flush_ms so the periodic flush never fires during the test,
   making the assertion fully deterministic.
"""
import asyncio
import pytest

from cloudbridge import events
from cloudbridge.gateway import FeishuGateway
from cloudbridge.inbound_filter import InboundFilter


class FakeFeishu:
    """Records send_card / update_card calls; simulates a blocking SDK client."""

    def __init__(self):
        self.sends: list = []
        self.updates: list = []

    def send_card(self, card) -> str:
        self.sends.append(card)
        return "msg-1"

    def update_card(self, mid: str, card) -> bool:
        self.updates.append((mid, card))
        return True


# ---------------------------------------------------------------------------
# Test 1: inbound deduplication and engine submission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_filtered_and_submitted():
    """Duplicate msg_id must be dropped; submit reaches engine exactly once."""
    loop = asyncio.get_running_loop()

    submitted: list[tuple[str, str]] = []

    async def submit(name: str, text: str) -> None:
        submitted.append((name, text))

    gw = FeishuGateway(
        loop,
        submit,
        FakeFeishu(),
        InboundFilter(start_ts=0.0),
        flush_ms=10,
    )

    # Same msg_id submitted twice — second should be silently dropped.
    gw.on_inbound("m1", create_time_ms=10_000, text="hello")
    gw.on_inbound("m1", create_time_ms=10_000, text="hello")

    # Let the event-loop drain the run_coroutine_threadsafe futures.
    await asyncio.sleep(0.05)

    assert submitted == [("main", "hello")]


# ---------------------------------------------------------------------------
# Test 2: delta coalescing — never one update per delta
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_deltas_are_coalesced_not_per_delta():
    """Four TextDelta events must produce exactly one card update, not four.

    Strategy: use flush_ms=10000 so the periodic flush task never fires during
    the test.  After emitting four deltas, TurnResult triggers the single final
    flush.  Assertions are fully deterministic — no wall-clock sleeps needed.
    """
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()

    gw = FeishuGateway(
        loop,
        lambda n, t: asyncio.sleep(0),
        fk,
        InboundFilter(start_ts=0.0),
        flush_ms=10_000,  # large enough that the periodic flush never fires
    )

    # Start a turn — sends a placeholder card.
    await gw.render(events.TurnStarted(session="main", user_text="hi"))

    # Emit four deltas in a tight loop (no sleeps — periodic flush cannot fire).
    for ch in ["a", "b", "c", "d"]:
        await gw.render(events.TextDelta(session="main", text=ch))

    # TurnResult triggers the single final flush.
    await gw.render(
        events.TurnResult(
            session="main",
            usage={},
            total_cost_usd=0.0,
            duration_ms=1,
        )
    )
    await gw.aclose()

    # Exactly one send_card for the placeholder card.
    assert len(fk.sends) == 1, (
        f"Expected exactly 1 send_card call, got {len(fk.sends)}"
    )

    # Exactly one update_card: the single final flush coalescing all 4 deltas.
    assert len(fk.updates) == 1, (
        f"Expected exactly 1 update_card call (4 deltas coalesced), "
        f"got {len(fk.updates)} — coalescing is broken"
    )
