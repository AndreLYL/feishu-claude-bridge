"""cloudbridge.app — assembles all cloudbridge components into a running bridge.

Entry points:
- build(loop, feishu_client, cwd, start_ts, max_sessions=3) -> (engine, gateway, health)
- run(feishu_client, cwd) — async real entry, called by bridge.py --core stream-json
"""

import asyncio
import time
import uuid

from cloudbridge.commands import route_inbound
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter
from cloudbridge.lock import SingleInstanceLock
from cloudbridge.stream_json_driver import StreamJsonDriver

# Spike-confirmed launch command (CLI 2.1.186, verified 2026-06-23).
# Per-session --session-id <uuid> is appended per session in _argv_for().
CANONICAL_ARGV = [
    "claude", "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--replay-user-messages",
    "--permission-prompt-tool", "stdio",
]


def _argv_for(session_id: str) -> list:
    return CANONICAL_ARGV + ["--session-id", session_id]


def make_inbound_handler(eng, gw, driver_factory, loop):
    """Build the lark ``on_message`` callback for the real run() path.

    The callback runs on the lark WS thread.  It applies the watermark + dedup
    gate (``gw.accept_inbound``) BEFORE dispatching to ``route_inbound`` — so the
    full command/permission router sits behind the same InboundFilter that
    ``gw.on_inbound`` uses.  This closes the DoD #2 replay gap: after a restart,
    Feishu's WS can re-deliver messages older than the process start; those are
    dropped here instead of being replayed into the engine.
    """

    def _on_message(text: str, msg_id: str, create_time_ms: int) -> None:
        if not gw.accept_inbound(msg_id, create_time_ms):
            return
        asyncio.run_coroutine_threadsafe(
            route_inbound(eng, gw, driver_factory, text), loop
        )

    return _on_message


def build(loop, feishu_client, cwd, start_ts, max_sessions=3):
    """Assemble HealthModel, Engine, FeishuGateway and wire them together.

    Parameters
    ----------
    loop:            running asyncio event loop
    feishu_client:   object with send_card(card)->str and update_card(mid, card)->bool
    cwd:             working directory (used by drivers created after build)
    start_ts:        unix timestamp; InboundFilter discards messages older than this
    max_sessions:    Engine.max_sessions cap

    Returns
    -------
    (engine, gateway, health)
    """
    health = HealthModel()

    # Placeholder on_event; overwritten immediately below.
    eng = Engine(health=health, on_event=lambda e: None, max_sessions=max_sessions)

    gw = FeishuGateway(
        loop,
        eng.submit,
        feishu_client,
        InboundFilter(start_ts=start_ts),
    )

    # Wire Engine events into the Gateway renderer (creates a task per event
    # so the on_event callback itself remains synchronous, as Engine requires).
    eng.on_event = lambda e: asyncio.create_task(gw.render(e))

    # Backpressure notice: send a Feishu text card when the per-session queue overflows.
    eng.on_backpressure = lambda name, depth: feishu_client.send_text(
        f"[{name}] 队列已满（{depth} 条在排队），稍后再试"
    )

    return eng, gw, health


async def run(feishu_client, cwd: str) -> None:
    """Real entry point: called by bridge.py --core stream-json.

    Acquires a single-instance lock, assembles the bridge components, spawns
    one StreamJsonDriver for the "main" session with crash self-healing via
    supervise, and blocks until interrupted.
    """
    lock = SingleInstanceLock("~/.feishu-claude-bridge/bridge.lock")
    if not lock.acquire():
        raise SystemExit("another bridge instance is running")

    loop = asyncio.get_running_loop()
    eng, gw, health = build(loop, feishu_client, cwd, start_ts=time.time())

    # driver_factory: constructs a fresh StreamJsonDriver for a given session name.
    # Each session gets its own UUID so --session-id is unique per process.
    def driver_factory(name: str) -> StreamJsonDriver:
        sid = uuid.uuid4().hex
        return StreamJsonDriver(name, _argv_for(sid), cwd=cwd, session_id=sid)

    # Create and start the initial "main" session.
    sid = uuid.uuid4().hex
    drv = StreamJsonDriver("main", _argv_for(sid), cwd=cwd, session_id=sid)
    await drv.start()
    eng.add_session("main", drv, active=True)
    await eng.start()

    # Spawn crash self-healing supervisor for the "main" session.
    asyncio.create_task(drv.supervise(on_event=eng.on_event))

    # FeishuClient.on_message is Callable[[str, str, int], None] = (text, msg_id,
    # create_time_ms).  The handler applies the InboundFilter watermark/dedup gate
    # before routing, so restarts don't replay Feishu's re-delivered old messages.
    feishu_client.on_message = make_inbound_handler(eng, gw, driver_factory, loop)

    # Block until cancelled / interrupted.
    await asyncio.Event().wait()
