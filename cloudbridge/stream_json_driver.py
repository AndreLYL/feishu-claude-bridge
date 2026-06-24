"""StreamJsonDriver — launches a long-lived claude stream-json subprocess,
reads stdout line-by-line, and normalizes events into the internal event model.

Field-name mapping follows CLI 2.1.186 recorded fixtures (spike 2026-06-23).
Task 8 adds 3-phase graceful close (kills process group) + crash-supervision loop.
"""

import asyncio
import json
import os
import signal
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
        self._turn_start: float = 0.0
        self.failed: bool = False
        self._closing: bool = False

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

    async def close(self, grace_stop: float = 120.0, grace_term: float = 5.0) -> None:
        """Three-phase graceful teardown targeting the entire process group.

        Phase 1: close stdin and wait up to ``grace_stop`` for natural exit
                 (allows Stop hooks like claude-mem to finish).
        Phase 2: on timeout, SIGTERM the process group; wait up to ``grace_term``.
        Phase 3: on timeout, SIGKILL the process group.

        Robust to ``ProcessLookupError``/``PermissionError`` (process already gone).
        The reader task is cancelled only after the process has exited so that
        trailing events are not lost.
        """
        self._closing = True
        if self._proc is None:
            return

        # Phase 1: close stdin, let the process (and its Stop hooks) exit naturally
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            await asyncio.wait_for(self._proc.wait(), timeout=grace_stop)
        except asyncio.TimeoutError:
            # Phase 2: SIGTERM the whole process group
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=grace_term)
            except asyncio.TimeoutError:
                # Phase 3: SIGKILL the whole process group
                self._signal_group(signal.SIGKILL)
                try:
                    await self._proc.wait()
                except (ProcessLookupError, PermissionError):
                    pass
        except (ProcessLookupError, PermissionError):
            pass

        # Cancel the reader task now that the process has exited (stdout is closed)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

    def _signal_group(self, sig: signal.Signals) -> None:
        """Send ``sig`` to the entire process group, ignoring errors if already gone."""
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def supervise(
        self,
        on_event,
        max_restarts: int = 3,
        window_s: float = 60,
        backoff_base: float = 0.5,
        _test_max_loops: Optional[int] = None,
    ) -> None:
        """Monitor the subprocess; auto-restart on crash with exponential backoff.

        A restart-storm guard counts restarts within a sliding ``window_s``
        window.  If the count exceeds ``max_restarts``, ``self.failed`` is set
        to ``True``, a ``SessionCrashed`` event is emitted, and the coroutine
        returns without relaunching.

        On each recovery the subprocess is relaunched with
        ``--resume <learned_session_id>`` appended (via :meth:`_with_resume`),
        and a ``SessionRecovered`` event is emitted.

        Parameters
        ----------
        on_event:        callable receiving each ``SessionRecovered`` /
                         ``SessionCrashed`` event
        max_restarts:    maximum number of restarts allowed within ``window_s``
        window_s:        sliding window duration in seconds
        backoff_base:    base sleep time; actual sleep = ``backoff_base * 2**(n-1)``
        _test_max_loops: safety valve for tests — exit the loop after this many
                         restart iterations even if not yet failed
        """
        restart_times: list[float] = []
        loops = 0
        while True:
            # Wait for the current subprocess to exit
            await self._proc.wait()

            # If we initiated the close ourselves, this is not a crash
            if self._closing:
                return

            # Sliding-window restart accounting
            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < window_s]
            restart_times.append(now)

            if len(restart_times) > max_restarts:
                self.failed = True
                on_event(events.SessionCrashed(session=self.name, restarts=len(restart_times)))
                return

            # Exponential backoff before relaunch
            await asyncio.sleep(backoff_base * (2 ** (len(restart_times) - 1)))

            # Cancel the stale reader task before relaunching to avoid leaking it
            if self._reader_task is not None:
                self._reader_task.cancel()

            # Relaunch with --resume so the session context is preserved
            self._argv = self._with_resume(self._argv, self.learned_session_id)
            await self.start()
            on_event(events.SessionRecovered(session=self.name))

            loops += 1
            if _test_max_loops is not None and loops >= _test_max_loops:
                return

    @staticmethod
    def _with_resume(argv: list[str], session_id: str) -> list[str]:
        """Return ``argv`` with ``--resume session_id`` appended, avoiding duplicates."""
        out = list(argv)
        if "--resume" not in out and session_id:
            out += ["--resume", session_id]
        return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

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
