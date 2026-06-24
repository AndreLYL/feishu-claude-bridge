"""cloudbridge.commands — pure command parser and async inbound router.

parse_command is a pure function (no I/O) so it is trivially unit-testable.
route_inbound depends on engine + gateway but not on the real subprocess,
so tests can pass FakeDriver / FakeFeishu without spawning anything.
"""

import asyncio
import re
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------

def parse_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Parse a Feishu text message for a recognised slash command.

    Returns
    -------
    ("new",    name)  for /new <name>
    ("switch", name)  for /switch <name>
    ("list",   None)  for /list
    ("delete", name)  for /delete <name>
    None              for anything else (including plain messages)
    """
    text = text.strip()
    if not text.startswith("/"):
        return None

    m = re.match(r"^/(\w+)(?:\s+(\S+))?$", text)
    if m is None:
        return None

    cmd = m.group(1).lower()
    arg = m.group(2)  # may be None for /list

    if cmd in ("new", "switch", "delete"):
        if arg is None:
            return None  # missing required argument
        return (cmd, arg)
    if cmd == "list":
        return ("list", None)

    return None  # unrecognised command — treat as plain text


# ---------------------------------------------------------------------------
# Async router
# ---------------------------------------------------------------------------

async def route_inbound(engine, gateway, driver_factory, text: str) -> None:
    """Route an inbound Feishu message to the right engine operation.

    - Recognised slash command → execute engine op and optionally send a
      plain-text reply via gateway.
    - Anything else → submit to the active session (never hardcoded "main").
    """
    parsed = parse_command(text)

    if parsed is None:
        # Plain message — dispatch to the currently active session.
        active = engine.active_session_name
        if active is not None:
            await engine.submit(active, text)
        return

    cmd, name = parsed

    if cmd == "new":
        driver = engine.create_session(name, driver_factory)
        await driver.start()
        asyncio.create_task(driver.supervise(on_event=engine.on_event))
        engine.switch_session(name)
        await _send_text(gateway, f"✅ 已创建并切换到会话 '{name}'")

    elif cmd == "switch":
        try:
            engine.switch_session(name)
            await _send_text(gateway, f"✅ 已切换到会话 '{name}'")
        except ValueError as exc:
            await _send_text(gateway, f"❌ {exc}")

    elif cmd == "list":
        sessions = engine.list_sessions()
        if not sessions:
            reply = "（无活跃会话）"
        else:
            lines = []
            for s in sessions:
                marker = "▶" if s["active"] else "  "
                lines.append(f"{marker} {s['name']}")
            reply = "会话列表：\n" + "\n".join(lines)
        await _send_text(gateway, reply)

    elif cmd == "delete":
        await engine.delete_session(name)
        await _send_text(gateway, f"✅ 已删除会话 '{name}'")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

async def _send_text(gateway, text: str) -> None:
    """Send a plain-text reply through the gateway's Feishu client."""
    import formatter
    card = formatter.format_status_notification(text, color="blue")
    await gateway._run(gateway._fs.send_card, card)
