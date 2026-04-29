# feishu-claude-bridge

Remote control your running Claude Code session from Feishu (Lark) on your phone.

**This is NOT another API bot.** It doesn't call the Claude API or start a new session. Instead, it takes over an existing Claude Code CLI session running in tmux — preserving your full context: MCP servers, conversation history, tools, skills, and everything else.

## Why?

Every "Claude + IM" bot out there starts a **fresh** Claude session per conversation. Context? Gone. MCP servers? Gone. Your 30-minute architecture discussion? Gone.

This bridge does the opposite: it connects your phone to the **same** Claude Code session running on your Mac. You pick up exactly where you left off.

## How it works

```
Feishu App ←(WebSocket)→ Bridge ←(tmux send-keys / JSONL)→ Claude Code CLI
```

1. **Feishu → Claude**: Bridge receives messages via Feishu WebSocket, types them into tmux
2. **Claude → Feishu**: Bridge polls Claude's JSONL output file, sends responses as Feishu cards
3. **No API calls**: Claude runs locally under your existing subscription

## Features

- **Session takeover**: Connect to a running Claude Code tmux session with full context
- **Streaming card updates**: Responses update in-place on a single card (no message spam)
- **Thinking visibility**: See Claude's thinking process as grey cards
- **Tool notifications**: See which tools Claude is using (Read, Write, Bash, etc.)
- **Image support**: Send images from Feishu, Claude reads them locally
- **Interactive menus**: AskUserQuestion selection menus forwarded to Feishu
- **Permission control**: Approve/deny tool execution from Feishu (reply y/n)
- **Heartbeat**: "Working..." indicator when Claude is processing
- **Auto-reconnect**: Automatically finds new JSONL when Claude restarts
- **launchd daemon**: Auto-start on boot (macOS)

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/anthropics/feishu-claude-bridge.git
cd feishu-claude-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create Feishu app

1. Go to [Feishu Open Platform](https://open.feishu.cn) → Create enterprise app
2. Add **Bot** capability
3. Permissions → add: `im:message`, `im:message:send_as_bot`, `im:resource`
4. Event subscriptions → select **"Use long connection (WebSocket)"**
5. Add event: `im.message.receive_v1`
6. Publish the app and approve in admin console

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
FEISHU_APP_ID=cli_xxxxx          # From Feishu app credentials
FEISHU_APP_SECRET=xxxxx          # From Feishu app credentials
ALLOWED_CHAT_ID=oc_xxxxx         # Chat ID where the bot lives
```

To find `ALLOWED_CHAT_ID`: send any message to the bot, check bridge logs for the chat ID.

### 4. Start Claude in tmux

```bash
tmux new-session -s claude
# Inside tmux:
cd /your/project
claude
```

### 5. Start bridge

```bash
# Option A: Direct
source .venv/bin/activate
python bridge.py --tmux-session claude

# Option B: One-command
./claude-bridge start

# Option C: launchd daemon (auto-start on boot)
bash scripts/install-service.sh
```

### 6. Chat from Feishu

Open the bot chat in Feishu and start typing. Your messages go directly to the Claude session in tmux.

## Architecture

```
┌─────────────┐     WebSocket      ┌─────────────────────┐     tmux keys     ┌──────────────┐
│  Feishu App  │◄──────────────────►│  feishu-claude-bridge│────────────────►│  Claude Code  │
│  (phone)     │                    │                     │◄────────────────│  (tmux)       │
└─────────────┘     Feishu cards    │  bridge.py          │   JSONL polling  └──────────────┘
                                    │  ├─ feishu_client.py │
                                    │  ├─ session_monitor  │
                                    │  ├─ tmux_controller  │
                                    │  ├─ formatter.py     │
                                    │  └─ hook_server.py   │
                                    └─────────────────────┘
```

| Module | Role |
|--------|------|
| `bridge.py` | Main orchestrator, state machine, card lifecycle |
| `feishu_client.py` | Feishu WebSocket connection, send/update cards, download images |
| `session_monitor.py` | Poll JSONL for new assistant messages, thinking, tool use |
| `tmux_controller.py` | Send keystrokes to tmux, capture pane content |
| `formatter.py` | Format Feishu interactive cards (reply, thinking, tool, permission) |
| `hook_server.py` | HTTP server for Claude Code permission hooks |

## Commands

In Feishu chat:

| Command | Action |
|---------|--------|
| (any text) | Send to Claude |
| (image) | Download and send file path to Claude |
| `y` / `n` | Approve/deny permission request |
| `/esc` | Send Escape key (cancel current operation) |
| `1`-`9` | Select menu option (when selection menu is active) |

## How it compares

| | API Bots (Feishu-OpenAI, etc.) | SDK Bots (ccbot.dev, etc.) | **feishu-claude-bridge** |
|---|---|---|---|
| Approach | Call LLM API | Start new SDK session | **Take over existing session** |
| Context | Per-conversation | Fresh each time | **Full session preserved** |
| Tools | Limited | SDK tools only | **Complete Claude Code toolchain** |
| MCP servers | No | No | **Yes, all connected** |
| Cost | API fees per message | API fees per message | **No extra cost** |
| Requires | API key | API key | **Running tmux + Claude Code** |

## Requirements

- macOS or Linux with tmux
- Python 3.9+
- Active [Claude Code](https://claude.ai/code) subscription
- Feishu enterprise app (free to create)

## License

MIT
