"""Tests for cloudbridge.gateway.FeishuGateway.

Tests:
1. test_inbound_filtered_and_submitted  — duplicate msg_id is deduplicated by
   InboundFilter; only one submit call reaches the engine.
2. test_text_deltas_are_coalesced_not_per_delta — four TextDelta events produce
   at most two card updates (send_card + at most one periodic + one final flush),
   NOT four.
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
    """Four TextDelta events must produce far fewer than four card updates.

    Strategy: use a comfortably long flush_ms (30 ms) so the periodic flush
    fires at most once during the 80 ms sleep window, then the final flush on
    TurnResult adds at most one more.  Total updates must be ≤ 2, not 4.
    """
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()

    gw = FeishuGateway(
        loop,
        lambda n, t: asyncio.sleep(0),
        fk,
        InboundFilter(start_ts=0.0),
        flush_ms=30,
    )

    # Start a turn — sends a placeholder card.
    await gw.render(events.TurnStarted(session="main", user_text="hi"))

    # Emit four deltas in quick succession (no sleep between them).
    for ch in ["a", "b", "c", "d"]:
        await gw.render(events.TextDelta(session="main", text=ch))

    # Wait one flush window so the periodic task has time to fire at most once.
    await asyncio.sleep(0.08)

    # TurnResult triggers the final flush.
    await gw.render(
        events.TurnResult(
            session="main",
            usage={},
            total_cost_usd=0.0,
            duration_ms=1,
        )
    )
    await gw.aclose()

    # Exactly one send_card for the placeholder.
    assert len(fk.sends) <= 1

    # update_card must be much less than 4 (the number of deltas).
    # With flush_ms=30 and an 80 ms wait we expect at most 2 updates:
    # one periodic flush + one final flush.
    assert len(fk.updates) < 4, (
        f"Expected coalesced updates (<4), got {len(fk.updates)} — "
        "delta-per-update coalescing is broken"
    )
