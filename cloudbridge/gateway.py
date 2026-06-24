"""FeishuGateway — thread-edge adapter between the lark WS thread and the async Engine.

Responsibilities:
- Inbound: receive messages from the lark WS callback thread, filter them via
  InboundFilter, and dispatch accepted messages into the event-loop via
  asyncio.run_coroutine_threadsafe.
- Outbound: consume Engine events and render them to Feishu cards, coalescing
  many TextDelta events into at most one card update per flush_ms window.
"""

import asyncio
import sys
import os

# formatter.py lives at the project root, which may not be on sys.path when
# running from a subdirectory.  Add it if needed.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import formatter
from cloudbridge import events


class FeishuGateway:
    """Thread-edge adapter connecting the Feishu WS thread to the async Engine.

    Parameters
    ----------
    loop:
        The running asyncio event loop that owns the Engine.
    submit_coro:
        Async callable ``(session_name: str, text: str) -> None``; typically
        ``Engine.submit``.
    feishu_client:
        Object exposing ``send_card(card) -> message_id`` and
        ``update_card(message_id, card) -> bool``.  Both may block (real SDK
        calls); they are always dispatched via ``run_in_executor``.
    inbound_filter:
        ``InboundFilter`` instance used to deduplicate and time-gate inbound
        messages.
    flush_ms:
        How often (in milliseconds) the periodic flush loop PATCHes the card
        with the coalesced text buffer.  Default 500 ms.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        submit_coro,
        feishu_client,
        inbound_filter,
        flush_ms: int = 500,
    ):
        self._loop = loop
        self._submit = submit_coro
        self._fs = feishu_client
        self._filter = inbound_filter
        self._flush_interval = flush_ms / 1000.0

        # Outbound state (all touched only from the event-loop)
        self._buf: list[str] = []
        self._msg_id: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._last_flushed: str = ""  # tracks text sent in last update_card call

    # ------------------------------------------------------------------
    # Inbound — called from the lark WS callback THREAD
    # ------------------------------------------------------------------

    def on_inbound(self, msg_id: str, create_time_ms: int, text: str) -> None:
        """Accept a raw inbound message from the lark WS thread.

        Filters via InboundFilter (dedup + time-gate), then dispatches the
        coroutine into the event-loop via run_coroutine_threadsafe so the
        engine sees it on the correct thread.
        """
        if not self._filter.accept(msg_id, create_time_ms):
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._submit("main", text), self._loop
            )
        except RuntimeError:
            # Loop has been closed — swallow silently.
            pass

    # ------------------------------------------------------------------
    # Outbound — consumes Engine events (called from the event-loop)
    # ------------------------------------------------------------------

    async def render(self, ev) -> None:
        """Dispatch a single Engine event to the appropriate render path."""
        if isinstance(ev, events.TurnStarted):
            await self._on_turn_started()
        elif isinstance(ev, events.TextDelta):
            self._buf.append(ev.text)
        elif isinstance(ev, (events.TextDone, events.TurnResult)):
            await self._flush_now(final=True)
        elif isinstance(ev, events.PermissionRequest):
            # Render a permission-request card (display only — user replies with y/n text)
            input_summary = str(ev.input)
            card = formatter.format_permission_request(ev.tool_name, input_summary, ev.request_id)
            await self._run(self._fs.send_card, card)
        elif isinstance(ev, events.SessionCrashed):
            card = formatter.format_status_notification(
                f"⚠️ 会话 '{ev.session}' 已崩溃（重启次数: {ev.restarts}）",
                color="red",
            )
            await self._run(self._fs.send_card, card)
        elif isinstance(ev, events.SessionRecovered):
            card = formatter.format_status_notification(
                f"✅ 会话 '{ev.session}' 已恢复",
                color="green",
            )
            await self._run(self._fs.send_card, card)

    async def _on_turn_started(self) -> None:
        """Reset state, send a placeholder card, and start the flush loop."""
        self._buf = []
        self._last_flushed = ""
        # Cancel any leftover flush task from a previous turn.
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

        # Send the placeholder status card (blocking SDK call → executor).
        placeholder = formatter.format_status_notification("正在处理…", color="grey")
        self._msg_id = await self._run(self._fs.send_card, placeholder)

        # Start the periodic flush loop.
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Periodically PATCH the card with the coalesced text buffer."""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                await self._flush_now(final=False)
        except asyncio.CancelledError:
            pass

    async def _flush_now(self, *, final: bool) -> None:
        """Send a card update if there is buffered text that differs from the last flush.

        If ``final`` is True, clear the buffer after sending (the turn is
        over so the loop will be cancelled shortly).
        Skips the ``update_card`` call when the buffered text is identical to
        what was already sent, avoiding redundant identical card updates.
        """
        if not self._buf or self._msg_id is None:
            return
        text = "".join(self._buf)
        if text == self._last_flushed:
            # No new content since the last flush — skip the redundant API call.
            return
        card = formatter.format_assistant_reply([text])
        await self._run(self._fs.update_card, self._msg_id, card)
        self._last_flushed = text
        if final:
            self._buf = []

    # ------------------------------------------------------------------
    # Thread-bridge helper
    # ------------------------------------------------------------------

    async def _run(self, fn, *args):
        """Execute a blocking function in the default executor.

        This is the load-bearing thread-bridge: ALL blocking feishu SDK calls
        go through here so we never stall the event-loop.
        """
        return await self._loop.run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel the background flush task."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
