"""StreamJsonDriver — launches a long-lived claude stream-json subprocess,
reads stdout line-by-line, and normalizes events into the internal event model.

Field-name mapping follows CLI 2.1.186 recorded fixtures (spike 2026-06-23).
The full 3-phase graceful close + process-group kill is a clear seam left for Task 8;
Task 6 provides a minimal close() that terminates the subprocess.
"""

import asyncio
import json
import os
import time
from typing import AsyncGenerator, Optional

from cloudbridge import events
from cloudbridge.driver import SessionDriver

# result subtypes that mean "mid-turn compaction, turn continues" (C3 guard)
_MIDTURN_RESULT_SUBTYPES = {"compact", "compaction"}


class StreamJsonDriver(SessionDriver):
    """Drives a long-lived ``claude --output-format stream-json`` subprocess.

    Parameters
    ----------
    name:        logical session name (used as ``session`` field in events)
    argv:        full argv list for the subprocess (e.g. [sys.executable, "scripts/fakeclaude.py"])
    cwd:         working directory for the subprocess
    session_id:  initial session ID (overwritten once system/init is received)
    env:         environment dict; defaults to os.environ copy
    """

    def __init__(
        self,
        name: str,
        argv: list[str],
        cwd: str,
        session_id: str,
        env: Optional[dict] = None,
    ) -> None:
        self.name = name
        self._argv = list(argv)
        self._cwd = cwd
        self.learned_session_id: str = session_id
        self._env = env if env is not None else os.environ.copy()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        # Accumulate text blocks within a single assistant message
        self._assistant_text: list[str] = []
        self._turn_start: float = 0.0

    # ------------------------------------------------------------------
    # Public interface (SessionDriver ABC)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the subprocess and start the stdout reader task."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,  # isolate process group for clean teardown
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send(self, text: str) -> None:
        """Write a user message to stdin.

        The Engine (C2 invariant) guarantees this is only called when idle —
        the driver itself does NOT queue outgoing messages.
        """
        self._assistant_text = []
        self._turn_start = time.monotonic()
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def answer_permission(self, request_id: str, allow: bool) -> None:
        """Write a control_response back to stdin.

        Fields follow the spike-verified CLI 2.1.186 format (§7 of the spec):
        ``request_id`` (top-level) and ``decision`` ("allow"/"deny").
        """
        resp = {
            "type": "control_response",
            "request_id": request_id,
            "decision": "allow" if allow else "deny",
        }
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(resp) + "\n").encode())
        await self._proc.stdin.drain()

    async def events(self) -> AsyncGenerator[events._Event, None]:
        """Async-iterate over normalized events from the subprocess."""
        while True:
            yield await self._out.get()

    async def close(self) -> None:
        """Minimal teardown — close stdin, terminate, wait, kill on timeout.

        Task 8 will extend this into the full 3-phase graceful close
        (stdin close → wait ~120 s for Stop hooks → SIGTERM → SIGKILL) with
        process-group targeting. This method is the seam for that extension.
        """
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc is not None:
            await self._terminate_proc()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _terminate_proc(self) -> None:
        """Terminate the subprocess with a short grace period.

        Full process-group kill (Task 8) replaces this with a 3-phase close.
        """
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    async def _read_loop(self) -> None:
        """Continuously read stdout lines and push translated events to the queue."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                for ev in self._translate(obj):
                    await self._out.put(ev)
        except asyncio.CancelledError:
            raise

    def _translate(self, obj: dict):
        """Translate one parsed JSON object into zero or more internal events.

        Field-name source of truth: CLI 2.1.186 recorded fixtures + §4 of
        2026-06-23-subproject-1-core-engine-design.md (spike-corrected).
        """
        t = obj.get("type")

        # ---- system events ----
        if t == "system":
            sub = obj.get("subtype")
            if sub == "init":
                # Learn the authoritative session_id; emit nothing
                self.learned_session_id = obj.get("session_id", self.learned_session_id)
            # post_turn_summary / status / thinking_tokens → ignore
            return

        # ---- rate_limit_event → ignore ----
        if t == "rate_limit_event":
            return

        # ---- user (--replay-user-messages echo or tool_result carrier) ----
        if t == "user":
            if obj.get("isReplay"):
                # Replayed user message: mark turn start; do NOT render as model output
                content = obj.get("message", {}).get("content", "")
                user_text = content if isinstance(content, str) else ""
                yield events.TurnStarted(session=self.name, user_text=user_text)
            # user without isReplay is a tool_result carrier; minimal: ignore for Task 6
            return

        # ---- assistant message (full content array) ----
        if t == "assistant":
            message = obj.get("message", {})
            content_blocks = message.get("content", [])
            text_parts: list[str] = []
            for block in content_blocks:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    # Emit ToolUse with name and input dict
                    yield events.ToolUse(
                        session=self.name,
                        name=block.get("name", ""),
                        input=block.get("input", {}),
                    )
                elif btype == "thinking":
                    yield events.Thinking(session=self.name, text=block.get("thinking", ""))
            if text_parts:
                full = "".join(text_parts)
                self._assistant_text.append(full)
                yield events.TextDone(session=self.name, full_text=full)
            return

        # ---- stream_event (incremental deltas when --include-partial-messages) ----
        if t == "stream_event":
            yield from self._translate_stream_event(obj.get("event", {}))
            return

        # ---- control_request (permission needed) ----
        if t == "control_request":
            # request_id is top-level; tool_name/input/description are under obj["request"]
            request = obj.get("request", {})
            yield events.PermissionRequest(
                session=self.name,
                request_id=obj.get("request_id", ""),
                tool_name=request.get("tool_name", ""),
                input=request.get("input", {}),
                description=request.get("description", ""),
            )
            return

        # ---- control_cancel_request ----
        if t == "control_cancel_request":
            yield events.TurnCancelled(session=self.name)
            return

        # ---- result ----
        if t == "result":
            subtype = obj.get("subtype")
            if subtype in _MIDTURN_RESULT_SUBTYPES:
                # C3 guard: mid-turn compaction — turn continues, do NOT emit TurnResult
                return
            # Terminal result (success, error, etc.)
            self.learned_session_id = obj.get("session_id", self.learned_session_id)
            yield events.TurnResult(
                session=self.name,
                usage=obj.get("usage", {}),
                total_cost_usd=obj.get("total_cost_usd", 0.0),
                duration_ms=obj.get("duration_ms", 0),
                num_turns=obj.get("num_turns", 0),
            )

    def _translate_stream_event(self, ev: dict):
        """Translate a single stream_event.event dict into events."""
        etype = ev.get("type")
        if etype == "content_block_delta":
            delta = ev.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                yield events.TextDelta(session=self.name, text=text)
            elif dtype == "thinking_delta":
                yield events.Thinking(session=self.name, text=delta.get("thinking", ""))
        # Other stream_event types (message_start, content_block_start/stop,
        # message_delta, message_stop) → ignore
