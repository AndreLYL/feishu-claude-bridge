# 飞书 Cloud Bridge · 子项目 1（核心引擎 + stream-json + 稳定性）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用常驻 `claude` stream-json 子进程 + 中央 asyncio Engine 替换现有 tmux 轮询，建立多会话、可自愈、可背压的稳定地基。

**Architecture:** asyncio 单事件循环的 Engine 编排 N 个 `StreamJsonDriver`（各自持有一个常驻 `claude` 子进程）；飞书 `lark-oapi`（同步/线程）隔离在 `FeishuGateway` 线程边缘适配器，经线程安全队列与 Engine 交互；会话清单原子落盘，进程崩溃自动 `--resume`。渲染在 SP1 沿用旧 `formatter.py` 占位，Engine↔渲染以结构化事件模型解耦。

**Tech Stack:** Python 3.11、asyncio、`asyncio.subprocess`、`lark-oapi`、pytest + `pytest-asyncio`、`fcntl.flock`。

## Global Constraints

- Python 3.11；新代码全部放包 `cloudbridge/`，测试放 `tests/cloudbridge/`，不改动现有顶层模块的行为。
- 新增依赖：`pytest-asyncio>=0.23`（加入 `requirements.txt`）。所有异步测试用 `@pytest.mark.asyncio`。
- 启动 `claude` 的 canonical 命令、权限 flag、事件字段，以 **Task 0 spike 的录制结果为准**；本计划中的事件形状是依据官方文档的代表性草案，Task 0 完成后若有差异，先更新 `tests/cloudbridge/fixtures/` 与对应解析代码再继续。
- TDD：每个组件先写失败测试，再最小实现，频繁提交。CI 测试**不依赖真 `claude`**（用 `scripts/fakeclaude.py` 桩进程）。
- 提交信息结尾附：
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012Aa669tE23XxKWjHQPboaE`
- 子进程一律 `start_new_session=True`（独立进程组），关闭杀**进程组**。
- 永不在事件循环里直接 `await` 阻塞的 lark SDK 调用——经 `loop.run_in_executor`。
- 回合进行中**绝不**向 stdin 写排队消息（C2 铁律）。

---

## 文件结构

```
cloudbridge/
  __init__.py
  events.py              # Task 1  内部事件模型（dataclass）
  session_store.py       # Task 2  会话清单原子持久化
  lock.py                # Task 3  单实例锁
  health.py              # Task 4  健康数据模型
  driver.py              # Task 5  SessionDriver 抽象基类
  stream_json_driver.py  # Task 6/7/8  常驻 claude 子进程驱动
  engine.py              # Task 9/10/11  中央编排器
  gateway.py             # Task 12 飞书线程边缘适配器 + 占位渲染
  app.py                 # Task 13 组装入口（被 bridge.py --core stream-json 调用）
scripts/
  spike_stream_json.py   # Task 0  spike 脚本（非 CI）
  fakeclaude.py          # Task 5  桩进程：回放 fixture，供离线测试
tests/cloudbridge/
  __init__.py
  fixtures/              # Task 0  录制的真实 stream-json 行
  test_events.py         # Task 1
  test_session_store.py  # Task 2
  test_lock.py           # Task 3
  test_health.py         # Task 4
  test_stream_json_driver.py        # Task 6/7/8
  test_engine.py         # Task 9/10/11
  test_gateway.py        # Task 12
  test_integration.py    # Task 13
docs/superpowers/specs/
  SPIKE-RESULTS.md       # Task 0 产出
```

---

## Task 0: stream-json 行为 spike（硬 gate，非 TDD）

> 这是引擎设计的前置验证。**不写引擎代码之前必须完成。** 需要本地有真 `claude` CLI。产出录制 fixture + 结论文档，并据此回填 spec §3/§4/§7。

**Files:**
- Create: `scripts/spike_stream_json.py`
- Create: `tests/cloudbridge/fixtures/` （录制的 `.jsonl` 输出）
- Create: `docs/superpowers/specs/SPIKE-RESULTS.md`
- Modify: `docs/superpowers/specs/2026-06-23-subproject-1-core-engine-design.md`（回填结论）

- [ ] **Step 1: 写 spike 脚本**

```python
# scripts/spike_stream_json.py
"""手动 spike：实测 claude stream-json 行为，录制 fixture。非 CI。
用法: python scripts/spike_stream_json.py <out_dir>
"""
import json, os, subprocess, sys, threading, time, uuid

def pump(stream, sink, label):
    for raw in iter(stream.readline, ""):
        sink.write(raw)
        sink.flush()
        print(f"[{label}] {raw.rstrip()}")

def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    sid = str(uuid.uuid4())
    argv = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--permission-prompt-tool", "stdio",   # C1: 实测它到底发不发 control_request
        "--session-id", sid,
    ]
    print("LAUNCH:", " ".join(argv))
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, start_new_session=True,
    )
    out = open(os.path.join(out_dir, "turn1.jsonl"), "w")
    t = threading.Thread(target=pump, args=(proc.stdout, out, "OUT"), daemon=True)
    te = threading.Thread(target=pump, args=(proc.stderr, sys.stderr, "ERR"), daemon=True)
    t.start(); te.start()

    def send(text):
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()

    # C1/M1: 普通回合 + 是否回显 user
    send("Say hello in one word.")
    time.sleep(20)
    # C1: 触发需要权限的工具，观察是否出现 control_request
    send("Create a file /tmp/spike_test.txt with the text 'hi'.")
    time.sleep(20)
    # C2: 回合进行中再发一条，观察是否挂起（先发一个长任务再立即追发）
    send("Count slowly from 1 to 20, one number per line.")
    send("(second message sent mid-turn — does the process hang?)")
    time.sleep(30)
    proc.stdin.close()
    proc.wait(timeout=130)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/cloudbridge/fixtures")
```

- [ ] **Step 2: 跑 spike，逐项记录**

Run: `python scripts/spike_stream_json.py tests/cloudbridge/fixtures`

逐项确认并记入 `SPIKE-RESULTS.md`：
- **[C1]** 加 `--permission-prompt-tool stdio` 后，创建文件时**是否**出现 `type:"control_request"`？若**静默执行**（bug #34046）→ 标记 stdio 不可用，记录改用自定义 MCP 权限工具的方案；记录 `control_request`/`control_response` 的确切字段名。
- **[C2]** 回合进行中追发的第二条消息——进程是否挂起 / 第二条何时被处理？确认"仅空闲时写 stdin"的必要性。
- **[C3]** 是否观察到 `subtype:"compact"/"compaction"` 的中途 `result`？记录 `result` 的确切 `subtype` 取值集合。
- **[M1]** `--replay-user-messages` 回显的 user 事件确切形状。
- **[M2]** 启动日志确认 MCP/Skills 自动加载、未默认 `--bare`。记录 canonical 启动命令。
- **[R3]** `system`/`result` 里报告的 `session_id` 是否等于 `--session-id` 传入值。

- [ ] **Step 3: 整理 fixture**

把 `turn1.jsonl` 拆成可复用片段：`fixtures/turn_text.jsonl`（一个纯文本回合：system init → stream_event deltas → result success）、`fixtures/turn_tool.jsonl`（含 tool_use）、`fixtures/permission.jsonl`（含 control_request，若 stdio 可用）、`fixtures/compact.jsonl`（含中途 compact result，若观察到）。每个文件每行一个 JSON 事件。

- [ ] **Step 4: 回填 spec 并提交**

按结论更新设计文档 §3.2/§4/§7（确切 flag、字段名、权限路径）。

```bash
git add scripts/spike_stream_json.py tests/cloudbridge/fixtures docs/superpowers/specs/SPIKE-RESULTS.md docs/superpowers/specs/2026-06-23-subproject-1-core-engine-design.md
git commit -m "spike: 实测 claude stream-json 行为并录制 fixture，回填SP1设计"
```

> **Gate：** 若 spike 发现承重假设被推翻（如常驻多回合不成立、权限完全无法拦截），**暂停**并回到 brainstorming 修订设计，不继续后续 Task。

---

## Task 1: 内部事件模型 `events.py`

**Files:**
- Create: `cloudbridge/__init__.py`（空）, `cloudbridge/events.py`
- Create: `tests/cloudbridge/__init__.py`（空）, `tests/cloudbridge/test_events.py`

**Interfaces:**
- Produces: frozen dataclasses `TurnStarted, TextDelta, TextDone, Thinking, ToolUse, ToolResult, PermissionRequest, TurnResult, TurnCancelled, SessionRecovered, SessionCrashed`，均有 `.session: str`。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_events.py
from cloudbridge import events

def test_events_carry_session_and_are_immutable():
    e = events.TextDelta(session="main", text="hi")
    assert e.session == "main"
    assert e.text == "hi"
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.text = "x"

def test_turn_result_fields():
    r = events.TurnResult(session="main", usage={"input_tokens": 5}, cost_usd=0.01, duration_ms=1200)
    assert r.cost_usd == 0.01 and r.duration_ms == 1200 and r.usage["input_tokens"] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_events.py -v`
Expected: FAIL（`ModuleNotFoundError: cloudbridge`）

- [ ] **Step 3: 实现**

```python
# cloudbridge/events.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class _Event:
    session: str

@dataclass(frozen=True)
class TurnStarted(_Event):
    user_text: str

@dataclass(frozen=True)
class TextDelta(_Event):
    text: str

@dataclass(frozen=True)
class TextDone(_Event):
    full_text: str

@dataclass(frozen=True)
class Thinking(_Event):
    text: str

@dataclass(frozen=True)
class ToolUse(_Event):
    tool_id: str
    name: str
    input_summary: str

@dataclass(frozen=True)
class ToolResult(_Event):
    tool_id: str
    name: str
    status: str            # "ok" | "error"
    exit_code: int | None = None

@dataclass(frozen=True)
class PermissionRequest(_Event):
    request_id: str
    tool: str
    tool_input: dict

@dataclass(frozen=True)
class TurnResult(_Event):
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_ms: int = 0

@dataclass(frozen=True)
class TurnCancelled(_Event):
    pass

@dataclass(frozen=True)
class SessionRecovered(_Event):
    pass

@dataclass(frozen=True)
class SessionCrashed(_Event):
    restarts: int
```

Create empty `cloudbridge/__init__.py` and `tests/cloudbridge/__init__.py`.

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_events.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/__init__.py cloudbridge/events.py tests/cloudbridge/__init__.py tests/cloudbridge/test_events.py
git commit -m "feat(cloudbridge): 内部事件模型 events.py"
```

---

## Task 2: 会话清单原子持久化 `session_store.py`

**Files:**
- Create: `cloudbridge/session_store.py`, `tests/cloudbridge/test_session_store.py`

**Interfaces:**
- Produces: `SessionRecord(name, session_id, cwd, created_at, active)` dataclass；`SessionStore(path)` with `.load() -> dict[str, SessionRecord]`、`.save(records: dict[str, SessionRecord]) -> None`（原子）。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_session_store.py
from cloudbridge.session_store import SessionStore, SessionRecord

def test_save_then_load_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    recs = {"main": SessionRecord(name="main", session_id="abc", cwd="/repo",
                                  created_at=123.0, active=True)}
    store.save(recs)
    loaded = store.load()
    assert loaded["main"].session_id == "abc"
    assert loaded["main"].active is True

def test_load_missing_file_returns_empty(tmp_path):
    assert SessionStore(tmp_path / "none.json").load() == {}

def test_save_is_atomic_no_partial_file(tmp_path):
    # 临时文件必须与目标同目录，写完 os.replace
    store = SessionStore(tmp_path / "sessions.json")
    store.save({"a": SessionRecord("a", "id", "/c", 1.0, False)})
    # 目录里只应有最终文件，没有遗留 .tmp
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_session_store.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
# cloudbridge/session_store.py
import json, os
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass
class SessionRecord:
    name: str
    session_id: str
    cwd: str
    created_at: float
    active: bool

class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return {k: SessionRecord(**v) for k, v in data.items()}

    def save(self, records: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in records.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")  # 同目录同文件系统
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)  # 原子替换
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_session_store.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/session_store.py tests/cloudbridge/test_session_store.py
git commit -m "feat(cloudbridge): 会话清单原子持久化 SessionStore"
```

---

## Task 3: 单实例锁 `lock.py`

**Files:**
- Create: `cloudbridge/lock.py`, `tests/cloudbridge/test_lock.py`

**Interfaces:**
- Produces: `SingleInstanceLock(path)` with `.acquire() -> bool`、`.release()`、上下文管理器。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_lock.py
from cloudbridge.lock import SingleInstanceLock

def test_second_acquire_fails_while_held(tmp_path):
    p = tmp_path / "bridge.lock"
    a = SingleInstanceLock(p)
    b = SingleInstanceLock(p)
    assert a.acquire() is True
    assert b.acquire() is False     # 已被占用
    a.release()
    assert b.acquire() is True      # 释放后可重新获取
    b.release()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_lock.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

```python
# cloudbridge/lock.py
import fcntl
from pathlib import Path

class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another bridge instance is already running")
        return self

    def __exit__(self, *exc):
        self.release()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_lock.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/lock.py tests/cloudbridge/test_lock.py
git commit -m "feat(cloudbridge): flock 单实例锁 SingleInstanceLock"
```

---

## Task 4: 健康数据模型 `health.py`

**Files:**
- Create: `cloudbridge/health.py`, `tests/cloudbridge/test_health.py`

**Interfaces:**
- Produces: `HealthModel()` with `.update_session(name, **fields)`、`.remove_session(name)`、`.record_error(name, msg)`、`.snapshot() -> dict`。每会话字段：`alive, busy, queue_depth, restarts, last_error`。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_health.py
from cloudbridge.health import HealthModel

def test_update_and_snapshot():
    h = HealthModel()
    h.update_session("main", alive=True, busy=False, queue_depth=0, restarts=0)
    snap = h.snapshot()
    assert snap["sessions"]["main"]["alive"] is True

def test_record_error_keeps_last():
    h = HealthModel()
    h.update_session("main", alive=True)
    h.record_error("main", "boom")
    assert h.snapshot()["sessions"]["main"]["last_error"] == "boom"

def test_remove_session():
    h = HealthModel()
    h.update_session("main", alive=True)
    h.remove_session("main")
    assert "main" not in h.snapshot()["sessions"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_health.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# cloudbridge/health.py
class HealthModel:
    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def update_session(self, name, **fields):
        s = self._sessions.setdefault(
            name, {"alive": False, "busy": False, "queue_depth": 0,
                   "restarts": 0, "last_error": None})
        s.update(fields)

    def record_error(self, name, msg):
        self.update_session(name, last_error=msg)

    def remove_session(self, name):
        self._sessions.pop(name, None)

    def snapshot(self) -> dict:
        return {"sessions": {k: dict(v) for k, v in self._sessions.items()}}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_health.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/health.py tests/cloudbridge/test_health.py
git commit -m "feat(cloudbridge): 健康数据模型 HealthModel"
```

---

## Task 5: SessionDriver 抽象基类 + FakeClaude 桩进程

**Files:**
- Create: `cloudbridge/driver.py`
- Create: `scripts/fakeclaude.py`
- Create: `tests/cloudbridge/test_fakeclaude.py`

**Interfaces:**
- Produces: `SessionDriver` ABC（`async start/send/answer_permission/close`，`events()->AsyncIterator`）。
- Produces: `scripts/fakeclaude.py` —— 读 stdin 的 user JSON，每收到一条就把环境变量 `FAKE_FIXTURE` 指向的 fixture 文件内容按行 echo 到 stdout（把字面量 `__SESSION__` 替换为 `--session-id`），用于离线测试；收到 `control_response` 行则继续。

- [ ] **Step 1: 写失败测试（先测 fakeclaude 桩本身）**

```python
# tests/cloudbridge/test_fakeclaude.py
import json, os, subprocess, sys
from pathlib import Path

def test_fakeclaude_replays_fixture_per_turn(tmp_path):
    fixture = tmp_path / "fix.jsonl"
    fixture.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "S"}) + "\n" +
        json.dumps({"type": "result", "subtype": "success", "result": "hi",
                    "session_id": "S", "usage": {}, "cost_usd": 0.0}) + "\n"
    )
    env = {**os.environ, "FAKE_FIXTURE": str(fixture)}
    proc = subprocess.Popen(
        [sys.executable, "scripts/fakeclaude.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, env=env)
    proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n")
    proc.stdin.flush()
    lines = [proc.stdout.readline() for _ in range(2)]
    proc.stdin.close(); proc.wait(timeout=5)
    assert json.loads(lines[0])["type"] == "system"
    assert json.loads(lines[1])["subtype"] == "success"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_fakeclaude.py -v`
Expected: FAIL（`fakeclaude.py` 不存在）

- [ ] **Step 3: 实现 fakeclaude.py 与 driver ABC**

```python
# scripts/fakeclaude.py
"""离线测试桩：模拟 claude stream-json。每收到一条 user 消息，回放一遍
FAKE_FIXTURE 指向的 fixture（按行）。收到 control_response 则忽略继续。"""
import json, os, sys

def main():
    fixture = os.environ["FAKE_FIXTURE"]
    with open(fixture) as f:
        template_lines = [ln for ln in f.read().splitlines() if ln.strip()]
    for raw in iter(sys.stdin.readline, ""):
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") == "control_response":
            continue  # 权限应答：继续等下一条
        for ln in template_lines:
            sys.stdout.write(ln + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
```

```python
# cloudbridge/driver.py
from abc import ABC, abstractmethod
from typing import AsyncIterator
from cloudbridge.events import _Event

class SessionDriver(ABC):
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def send(self, text: str) -> None: ...
    @abstractmethod
    async def answer_permission(self, request_id: str, allow: bool) -> None: ...
    @abstractmethod
    def events(self) -> AsyncIterator[_Event]: ...
    @abstractmethod
    async def close(self) -> None: ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_fakeclaude.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/driver.py scripts/fakeclaude.py tests/cloudbridge/test_fakeclaude.py
git commit -m "feat(cloudbridge): SessionDriver 抽象基类 + FakeClaude 测试桩"
```

---

## Task 6: StreamJsonDriver —— 启动 + 发送 + 事件解析（C3 subtype）

**Files:**
- Create: `cloudbridge/stream_json_driver.py`
- Create: `tests/cloudbridge/test_stream_json_driver.py`

**Interfaces:**
- Consumes: `cloudbridge.events.*`, `SessionDriver`。
- Produces: `StreamJsonDriver(name, argv, cwd, session_id)`，实现 ABC；内部 `events()` 产出归一化事件；公开 `learned_session_id`。**仅在空闲时写 stdin**（`send()` 由 Engine 保证调用时机；driver 自身不缓存队列）。

- [ ] **Step 1: 写失败测试（用 fakeclaude 回放纯文本回合 + 校验 subtype）**

```python
# tests/cloudbridge/test_stream_json_driver.py
import json, os, sys, asyncio
import pytest
from cloudbridge import events
from cloudbridge.stream_json_driver import StreamJsonDriver

def _fixture(tmp_path, lines):
    p = tmp_path / "fix.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return p

def _argv(fixture):
    return [sys.executable, "scripts/fakeclaude.py"], {**os.environ, "FAKE_FIXTURE": str(fixture)}

@pytest.mark.asyncio
async def test_text_turn_yields_delta_done_and_result(tmp_path):
    fix = _fixture(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "S1"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "result", "subtype": "success", "result": "Hello",
            "session_id": "S1", "usage": {"input_tokens": 3}, "cost_usd": 0.01},
    ])
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S1", env=env)
    await d.start()
    got = []
    async def collect():
        async for e in d.events():
            got.append(e)
            if isinstance(e, events.TurnResult):
                return
    await d.send("hi")
    await asyncio.wait_for(collect(), timeout=5)
    await d.close()
    deltas = [e.text for e in got if isinstance(e, events.TextDelta)]
    assert deltas == ["Hel", "lo"]
    assert any(isinstance(e, events.TextDone) and e.full_text == "Hello" for e in got)
    assert any(isinstance(e, events.TurnResult) for e in got)

@pytest.mark.asyncio
async def test_compact_result_does_not_end_turn(tmp_path):
    # C3: 中途 compact result 必须被忽略，仅终态 success 产出 TurnResult
    fix = _fixture(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "S2"},
        {"type": "result", "subtype": "compact", "session_id": "S2"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "done"}}},
        {"type": "result", "subtype": "success", "result": "done",
            "session_id": "S2", "usage": {}, "cost_usd": 0.0},
    ])
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S2", env=env)
    await d.start()
    results = []
    async def collect():
        async for e in d.events():
            if isinstance(e, events.TurnResult):
                results.append(e); return
    await d.send("hi")
    await asyncio.wait_for(collect(), timeout=5)
    await d.close()
    assert len(results) == 1   # compact 未误触发
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 driver（启动/发送/解析）**

```python
# cloudbridge/stream_json_driver.py
import asyncio, json, os, signal, time
from typing import AsyncIterator, Optional
from cloudbridge import events
from cloudbridge.driver import SessionDriver

# 终态 result 的 subtype（compact/compaction 等中途态除外）。Task 0 spike 校正此集合。
_MIDTURN_RESULT_SUBTYPES = {"compact", "compaction"}

class StreamJsonDriver(SessionDriver):
    def __init__(self, name, argv, cwd, session_id, env=None):
        self.name = name
        self._argv = list(argv)
        self._cwd = cwd
        self.learned_session_id = session_id
        self._env = env or os.environ.copy()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._out: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._turn_text: list[str] = []
        self._turn_start: float = 0.0

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd, env=self._env,
            start_new_session=True,   # 独立进程组，便于整组终止
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send(self, text: str) -> None:
        # 调用方（Engine）保证仅在会话空闲时调用（C2 铁律）。
        self._turn_text = []
        self._turn_start = time.monotonic()
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    async def answer_permission(self, request_id: str, allow: bool) -> None:
        resp = {"type": "control_response", "request_uuid": request_id,
                "action": "approve" if allow else "deny"}
        self._proc.stdin.write((json.dumps(resp) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
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

    def _translate(self, obj: dict):
        t = obj.get("type")
        if t == "system" and obj.get("subtype") == "init":
            self.learned_session_id = obj.get("session_id", self.learned_session_id)
            return
        if t == "user":   # --replay-user-messages 的回显：作回合起点，吞掉不渲染
            content = obj.get("message", {}).get("content", "")
            text = content if isinstance(content, str) else ""
            yield events.TurnStarted(session=self.name, user_text=text)
            return
        if t == "stream_event":
            yield from self._translate_stream(obj.get("event", {}))
            return
        if t == "control_request":
            yield events.PermissionRequest(
                session=self.name,
                request_id=obj.get("request_uuid") or obj.get("uuid", ""),
                tool=obj.get("tool_name", ""),
                tool_input=obj.get("tool_input", {}))
            return
        if t == "control_cancel_request":
            yield events.TurnCancelled(session=self.name)
            return
        if t == "result":
            if obj.get("subtype") in _MIDTURN_RESULT_SUBTYPES:
                return  # C3: 中途压缩，回合未结束
            if self._turn_text:
                yield events.TextDone(session=self.name, full_text="".join(self._turn_text))
            self.learned_session_id = obj.get("session_id", self.learned_session_id)
            yield events.TurnResult(
                session=self.name,
                usage=obj.get("usage", {}),
                cost_usd=obj.get("cost_usd", 0.0),
                duration_ms=int((time.monotonic() - self._turn_start) * 1000))
            return

    def _translate_stream(self, ev: dict):
        et = ev.get("type")
        if et == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                txt = delta.get("text", "")
                self._turn_text.append(txt)
                yield events.TextDelta(session=self.name, text=txt)
        elif et == "content_block_start":
            blk = ev.get("content_block", {})
            if blk.get("type") == "tool_use":
                yield events.ToolUse(session=self.name, tool_id=blk.get("id", ""),
                                     name=blk.get("name", ""), input_summary="")

    async def events(self) -> AsyncIterator[events._Event]:
        while True:
            yield await self._out.get()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            await self._terminate_group()

    async def _terminate_group(self) -> None:
        # 完整三段式在 Task 8 实现；此处先最小关闭以让 Task 6 测试可收尾。
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py -v`
Expected: PASS（两条均过）

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/stream_json_driver.py tests/cloudbridge/test_stream_json_driver.py
git commit -m "feat(cloudbridge): StreamJsonDriver 启动/发送/事件解析(含C3 subtype守卫)"
```

---

## Task 7: StreamJsonDriver —— 权限请求转发与应答（C1）

**Files:**
- Modify: `cloudbridge/stream_json_driver.py`（复用 Task 6 的 `control_request` 解析；新增"应答后桩进程继续"的端到端验证）
- Modify: `tests/cloudbridge/test_stream_json_driver.py`

**Interfaces:**
- Consumes: `PermissionRequest`、`answer_permission()`（Task 6 已实现）。
- Produces: 验证一次"请求→应答→回合继续→result"闭环。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/cloudbridge/test_stream_json_driver.py
@pytest.mark.asyncio
async def test_permission_request_then_answer_completes_turn(tmp_path):
    fix = _fixture(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "S3"},
        {"type": "control_request", "request_uuid": "req-1",
            "tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"type": "result", "subtype": "success", "result": "ok",
            "session_id": "S3", "usage": {}, "cost_usd": 0.0},
    ])
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S3", env=env)
    await d.start()
    seen = {"perm": None, "result": False}
    async def run():
        async for e in d.events():
            if isinstance(e, events.PermissionRequest):
                seen["perm"] = e
                await d.answer_permission(e.request_id, allow=True)
            if isinstance(e, events.TurnResult):
                seen["result"] = True; return
    await d.send("run ls")
    await asyncio.wait_for(run(), timeout=5)
    await d.close()
    assert seen["perm"].request_id == "req-1"
    assert seen["perm"].tool == "Bash"
    assert seen["result"] is True
```

> 注：本测试用 fakeclaude 桩验证 driver 侧闭环。真实 `claude` 的权限行为（stdio 是否可用 / 改用 MCP 工具）由 Task 0 spike 确认；若 spike 判定 stdio 不可用，则在 `app.py`（Task 13）按 spike 结论注入自定义 MCP 权限工具的 argv，driver 逻辑不变。

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py::test_permission_request_then_answer_completes_turn -v`
Expected: 若 Task 6 解析已正确则 PASS；若失败按报错补 `control_request` 字段映射。

- [ ] **Step 3: 按需修正解析**

若 spike 录制的 `control_request` 字段名与 Task 6 中 `request_uuid`/`tool_name`/`tool_input` 不同，更新 `_translate()` 对应取值与 `answer_permission()` 的 `request_uuid` 字段名。

- [ ] **Step 4: 跑全文件测试确认通过**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/stream_json_driver.py tests/cloudbridge/test_stream_json_driver.py
git commit -m "feat(cloudbridge): 权限请求转发与应答闭环(C1)"
```

---

## Task 8: StreamJsonDriver —— 优雅关闭三段式 + 崩溃自愈

**Files:**
- Modify: `cloudbridge/stream_json_driver.py`
- Modify: `tests/cloudbridge/test_stream_json_driver.py`

**Interfaces:**
- Produces: `close(grace_stop=…, grace_term=…)` 三段式杀进程组；`supervise(on_crash)` 协程：进程非正常退出时按退避重启 + `--resume learned_session_id`，1 分钟内 >3 次置 `failed`；产出 `SessionRecovered`/`SessionCrashed` 事件。

- [ ] **Step 1: 写失败测试（关闭杀进程组 + 重启计数）**

```python
# 追加到 tests/cloudbridge/test_stream_json_driver.py
import os, signal

@pytest.mark.asyncio
async def test_close_terminates_process_group(tmp_path):
    fix = _fixture(tmp_path, [
        {"type": "system", "subtype": "init", "session_id": "S4"},
        {"type": "result", "subtype": "success", "result": "x",
            "session_id": "S4", "usage": {}, "cost_usd": 0.0},
    ])
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S4", env=env)
    await d.start()
    pid = d._proc.pid
    await d.close(grace_stop=0.2, grace_term=0.5)
    # 进程已退出
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

@pytest.mark.asyncio
async def test_restart_storm_marks_failed(tmp_path):
    # 桩进程立刻退出（空 fixture），模拟反复崩溃
    fix = tmp_path / "empty.jsonl"; fix.write_text("")
    argv, env = _argv(fix)
    d = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S5", env=env)
    crashes = []
    await d.start()
    await d.supervise(on_event=lambda e: crashes.append(e), max_restarts=3, window_s=60,
                      backoff_base=0.01, _test_max_loops=5)
    assert d.failed is True
    assert any(isinstance(e, events.SessionCrashed) for e in crashes)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py -k "process_group or restart_storm" -v`
Expected: FAIL（`close()` 无参数、无 `supervise`/`failed`）

- [ ] **Step 3: 实现三段式关闭 + 监督**

```python
# cloudbridge/stream_json_driver.py 内：替换 close/_terminate_group，新增 supervise
    async def close(self, grace_stop: float = 120.0, grace_term: float = 5.0) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if not self._proc:
            return
        # 三段式：关 stdin 等 Stop hooks → SIGTERM 进程组 → SIGKILL 进程组
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            await asyncio.wait_for(self._proc.wait(), timeout=grace_stop)
            return
        except asyncio.TimeoutError:
            pass
        self._signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace_term)
            return
        except asyncio.TimeoutError:
            pass
        self._signal_group(signal.SIGKILL)
        try:
            await self._proc.wait()
        except ProcessLookupError:
            pass

    def _signal_group(self, sig) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)  # 杀整个进程组
        except (ProcessLookupError, PermissionError):
            pass

    async def supervise(self, on_event, max_restarts=3, window_s=60,
                        backoff_base=0.5, _test_max_loops=None) -> None:
        self.failed = False
        restart_times: list[float] = []
        loops = 0
        while True:
            await self._proc.wait()            # 等当前进程结束
            if getattr(self, "_closing", False):
                return
            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < window_s]
            restart_times.append(now)
            if len(restart_times) > max_restarts:
                self.failed = True
                on_event(events.SessionCrashed(session=self.name, restarts=len(restart_times)))
                return
            await asyncio.sleep(backoff_base * (2 ** (len(restart_times) - 1)))
            # 用学到的 session_id resume
            self._argv = self._with_resume(self._argv, self.learned_session_id)
            await self.start()
            on_event(events.SessionRecovered(session=self.name))
            loops += 1
            if _test_max_loops is not None and loops >= _test_max_loops:
                # 测试安全阀，避免无限循环
                continue

    @staticmethod
    def _with_resume(argv, session_id):
        out = [a for a in argv]
        if "--resume" not in out and session_id:
            out += ["--resume", session_id]
        return out
```

在 `__init__` 增加 `self.failed = False` 和 `self._closing = False`，并在 `close()` 开头设 `self._closing = True`。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_stream_json_driver.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/stream_json_driver.py tests/cloudbridge/test_stream_json_driver.py
git commit -m "feat(cloudbridge): 优雅关闭三段式(杀进程组)+崩溃自愈监督"
```

---

## Task 9: Engine —— 单会话回合生命周期 + hold-until-result 背压（C2）

**Files:**
- Create: `cloudbridge/engine.py`
- Create: `tests/cloudbridge/test_engine.py`

**Interfaces:**
- Consumes: `SessionDriver`, `events.*`, `HealthModel`。
- Produces: `Engine(health, on_event, queue_max=…, idle_timeout=…)`；`.add_session(name, driver, active=False)`；`async .submit(name, text)`（入站消息）；内部每会话 `busy` + `pending: deque`；**仅在收到 `TurnResult` 后才 drain 下一条写 stdin**（C2）；`on_event(event)` 回调把归一化事件交给渲染。

- [ ] **Step 1: 写失败测试（C2：第二条消息回合中途不写 stdin）**

```python
# tests/cloudbridge/test_engine.py
import asyncio, pytest
from collections import deque
from cloudbridge import events
from cloudbridge.engine import Engine
from cloudbridge.health import HealthModel

class FakeDriver:
    """记录 send 调用时机的假 driver。手动喂事件。"""
    def __init__(self, name):
        self.name = name
        self.sends = []
        self._q = asyncio.Queue()
    async def start(self): pass
    async def send(self, text): self.sends.append(text)
    async def answer_permission(self, rid, allow): pass
    async def close(self, **k): pass
    async def events(self):
        while True:
            yield await self._q.get()
    def feed(self, e): self._q.put_nowait(e)

@pytest.mark.asyncio
async def test_second_message_not_sent_until_result(tmp_path):
    drv = FakeDriver("main")
    eng = Engine(health=HealthModel(), on_event=lambda e: None)
    eng.add_session("main", drv, active=True)
    await eng.start()
    await eng.submit("main", "first")
    await asyncio.sleep(0.05)
    assert drv.sends == ["first"]          # 第一条已写
    await eng.submit("main", "second")     # 回合进行中
    await asyncio.sleep(0.05)
    assert drv.sends == ["first"]          # C2: 第二条尚未写入
    drv.feed(events.TurnResult(session="main", usage={}, cost_usd=0.0, duration_ms=10))
    await asyncio.sleep(0.05)
    assert drv.sends == ["first", "second"]  # result 后才 drain 第二条
    await eng.stop()

@pytest.mark.asyncio
async def test_queue_full_reports_backpressure():
    reported = []
    drv = FakeDriver("main")
    eng = Engine(health=HealthModel(), on_event=lambda e: None,
                 queue_max=1, on_backpressure=lambda name, depth: reported.append((name, depth)))
    eng.add_session("main", drv, active=True)
    await eng.start()
    await eng.submit("main", "first")    # 占用回合
    await asyncio.sleep(0.02)
    await eng.submit("main", "q1")       # 入队（depth=1）
    await eng.submit("main", "overflow") # 超界 → 背压
    await asyncio.sleep(0.02)
    assert reported and reported[-1][0] == "main"
    await eng.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_engine.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 Engine 单会话核心**

```python
# cloudbridge/engine.py
import asyncio, time
from collections import deque
from cloudbridge import events

class _Session:
    def __init__(self, name, driver, active):
        self.name = name
        self.driver = driver
        self.active = active
        self.busy = False
        self.pending: deque = deque()
        self.event_task = None

class Engine:
    def __init__(self, health, on_event, queue_max=20, idle_timeout=300,
                 on_backpressure=None):
        self.health = health
        self.on_event = on_event
        self.queue_max = queue_max
        self.idle_timeout = idle_timeout
        self.on_backpressure = on_backpressure or (lambda name, depth: None)
        self._sessions: dict[str, _Session] = {}
        self._running = False

    def add_session(self, name, driver, active=False):
        s = _Session(name, driver, active)
        self._sessions[name] = s
        self.health.update_session(name, alive=True, busy=False, queue_depth=0, restarts=0)

    async def start(self):
        self._running = True
        for s in self._sessions.values():
            s.event_task = asyncio.create_task(self._consume(s))

    async def stop(self):
        self._running = False
        for s in self._sessions.values():
            if s.event_task:
                s.event_task.cancel()

    async def submit(self, name, text):
        s = self._sessions[name]
        if not s.busy:
            await self._dispatch(s, text)
        else:
            if len(s.pending) >= self.queue_max:
                self.on_backpressure(name, len(s.pending))
                return
            s.pending.append(text)
            self.health.update_session(name, queue_depth=len(s.pending))

    async def _dispatch(self, s, text):
        s.busy = True
        self.health.update_session(s.name, busy=True)
        await s.driver.send(text)   # 仅在空闲时写 stdin（C2）

    async def _consume(self, s):
        async for ev in s.driver.events():
            self.on_event(ev)
            if isinstance(ev, events.TurnResult):
                s.busy = False
                self.health.update_session(s.name, busy=False)
                if s.pending:                 # result 后才 drain（C2）
                    nxt = s.pending.popleft()
                    self.health.update_session(s.name, queue_depth=len(s.pending))
                    await self._dispatch(s, nxt)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/engine.py tests/cloudbridge/test_engine.py
git commit -m "feat(cloudbridge): Engine 回合生命周期 + hold-until-result 背压(C2)"
```

---

## Task 10: Engine —— 多会话注册与命令（/new /switch /list /delete）

**Files:**
- Modify: `cloudbridge/engine.py`
- Modify: `tests/cloudbridge/test_engine.py`

**Interfaces:**
- Produces: `Engine.create_session(name, driver_factory)`（受 `max_sessions` 限制）、`switch_session(name)`、`list_sessions() -> list[dict]`、`delete_session(name)`（优雅关闭 + 移除 + 健康表清理）、`active_session_name` 属性。`Engine(__init__)` 增 `max_sessions=3`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/cloudbridge/test_engine.py
@pytest.mark.asyncio
async def test_max_sessions_enforced():
    eng = Engine(health=HealthModel(), on_event=lambda e: None, max_sessions=2)
    await eng.start()
    eng.add_session("a", FakeDriver("a"), active=True)
    eng.add_session("b", FakeDriver("b"))
    with pytest.raises(ValueError):
        eng.create_session("c", lambda name: FakeDriver(name))
    await eng.stop()

@pytest.mark.asyncio
async def test_switch_and_list_and_delete():
    eng = Engine(health=HealthModel(), on_event=lambda e: None, max_sessions=3)
    await eng.start()
    eng.add_session("a", FakeDriver("a"), active=True)
    eng.add_session("b", FakeDriver("b"))
    eng.switch_session("b")
    assert eng.active_session_name == "b"
    names = {s["name"] for s in eng.list_sessions()}
    assert names == {"a", "b"}
    await eng.delete_session("a")
    assert "a" not in {s["name"] for s in eng.list_sessions()}
    await eng.stop()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_engine.py -k "max_sessions or switch_and_list" -v`
Expected: FAIL

- [ ] **Step 3: 实现多会话方法**

```python
# 在 Engine.__init__ 增加参数 max_sessions=3 并保存：self.max_sessions = max_sessions
# 在 Engine 类内新增：

    @property
    def active_session_name(self):
        for s in self._sessions.values():
            if s.active:
                return s.name
        return None

    def create_session(self, name, driver_factory):
        if name in self._sessions:
            raise ValueError(f"session '{name}' already exists")
        if len(self._sessions) >= self.max_sessions:
            raise ValueError(f"max sessions reached ({self.max_sessions})")
        driver = driver_factory(name)
        self.add_session(name, driver, active=False)
        if self._running:
            self._sessions[name].event_task = asyncio.create_task(
                self._consume(self._sessions[name]))
        return driver

    def switch_session(self, name):
        if name not in self._sessions:
            raise ValueError(f"no such session '{name}'")
        for s in self._sessions.values():
            s.active = (s.name == name)

    def list_sessions(self):
        return [{"name": s.name, "active": s.active, "busy": s.busy,
                 "queue_depth": len(s.pending)} for s in self._sessions.values()]

    async def delete_session(self, name):
        s = self._sessions.pop(name, None)
        if not s:
            return
        if s.event_task:
            s.event_task.cancel()
        await s.driver.close()
        self.health.remove_session(name)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/engine.py tests/cloudbridge/test_engine.py
git commit -m "feat(cloudbridge): Engine 多会话注册/switch/list/delete + MAX_SESSIONS"
```

---

## Task 11: 入站去重 + 启动水位线（−2s 宽限）

**Files:**
- Create: `cloudbridge/inbound_filter.py`
- Create: `tests/cloudbridge/test_inbound_filter.py`

**Interfaces:**
- Produces: `InboundFilter(start_ts, grace_s=2.0, max_ids=200)` with `.accept(msg_id, create_time_ms) -> bool`（旧消息丢弃 + msg_id 去重）。供 Gateway 在投递前调用。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_inbound_filter.py
from cloudbridge.inbound_filter import InboundFilter

def test_drops_messages_before_watermark_with_grace():
    f = InboundFilter(start_ts=1000.0, grace_s=2.0)
    # 早于 (1000-2)=998 秒 → 丢弃；create_time 是毫秒
    assert f.accept("m1", create_time_ms=997_000) is False
    # 在宽限窗内 → 接受
    assert f.accept("m2", create_time_ms=999_000) is True

def test_dedup_repeated_msg_id():
    f = InboundFilter(start_ts=0.0)
    assert f.accept("dup", create_time_ms=10_000) is True
    assert f.accept("dup", create_time_ms=10_000) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_inbound_filter.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

```python
# cloudbridge/inbound_filter.py
from collections import OrderedDict

class InboundFilter:
    def __init__(self, start_ts: float, grace_s: float = 2.0, max_ids: int = 200):
        self._watermark = start_ts - grace_s   # 秒
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max = max_ids

    def accept(self, msg_id: str, create_time_ms: int) -> bool:
        if create_time_ms / 1000.0 < self._watermark:
            return False                        # 启动前的旧消息
        if msg_id in self._seen:
            return False                        # 重复（WS 重连重发）
        self._seen[msg_id] = None
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_inbound_filter.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/inbound_filter.py tests/cloudbridge/test_inbound_filter.py
git commit -m "feat(cloudbridge): 入站去重 + 启动水位线(-2s宽限)"
```

---

## Task 12: FeishuGateway —— 线程边缘 + 占位渲染（delta 合并）

**Files:**
- Create: `cloudbridge/gateway.py`
- Create: `tests/cloudbridge/test_gateway.py`

**Interfaces:**
- Consumes: `events.*`, `InboundFilter`, 现有 `feishu_client.FeishuClient`（仅在真实运行注入；测试用假 client）。
- Produces: `FeishuGateway(loop, submit_coro, feishu_client, inbound_filter, flush_ms=500)`；`.on_inbound(msg_id, create_time_ms, text)`（线程侧调用，过滤后经 `run_coroutine_threadsafe` 投递）；`.render(event)`（消费 Engine 事件，`TextDelta` 按 `flush_ms` 合并后经 `run_in_executor` 发卡，绝不每 delta 一次）。

- [ ] **Step 1: 写失败测试**

```python
# tests/cloudbridge/test_gateway.py
import asyncio, pytest
from cloudbridge import events
from cloudbridge.gateway import FeishuGateway
from cloudbridge.inbound_filter import InboundFilter

class FakeFeishu:
    def __init__(self): self.sends = []; self.updates = []
    def send_card(self, card): self.sends.append(card); return "msg-1"
    def update_card(self, mid, card): self.updates.append((mid, card)); return True

@pytest.mark.asyncio
async def test_inbound_filtered_and_submitted():
    loop = asyncio.get_running_loop()
    submitted = []
    async def submit(name, text): submitted.append((name, text))
    gw = FeishuGateway(loop, submit, FakeFeishu(),
                       InboundFilter(start_ts=0.0), flush_ms=10)
    gw.on_inbound("m1", create_time_ms=10_000, text="hello")
    gw.on_inbound("m1", create_time_ms=10_000, text="hello")  # 重复，丢弃
    await asyncio.sleep(0.05)
    assert submitted == [("main", "hello")]

@pytest.mark.asyncio
async def test_text_deltas_are_coalesced_not_per_delta():
    loop = asyncio.get_running_loop()
    fk = FakeFeishu()
    gw = FeishuGateway(loop, lambda n, t: asyncio.sleep(0), fk,
                       InboundFilter(start_ts=0.0), flush_ms=30)
    await gw.render(events.TurnStarted(session="main", user_text="hi"))
    for ch in ["a", "b", "c", "d"]:
        await gw.render(events.TextDelta(session="main", text=ch))
    await asyncio.sleep(0.08)                     # 等一个 flush 周期
    await gw.render(events.TurnResult(session="main", usage={}, cost_usd=0.0, duration_ms=1))
    await gw.aclose()
    # 不应每个 delta 发一次：发卡+更新次数远小于 4
    assert len(fk.sends) <= 1
    assert len(fk.updates) <= 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_gateway.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 Gateway**

```python
# cloudbridge/gateway.py
import asyncio, json
from cloudbridge import events
import formatter  # 复用现有占位渲染

class FeishuGateway:
    def __init__(self, loop, submit_coro, feishu_client, inbound_filter,
                 flush_ms: int = 500):
        self._loop = loop
        self._submit = submit_coro
        self._fs = feishu_client
        self._filter = inbound_filter
        self._flush = flush_ms / 1000.0
        self._buf: list[str] = []
        self._msg_id = None
        self._flush_task = None

    # ---- 入站（lark 线程侧调用）----
    def on_inbound(self, msg_id, create_time_ms, text):
        if not self._filter.accept(msg_id, create_time_ms):
            return
        # 投递进事件循环；对已关闭 loop 做保护
        try:
            asyncio.run_coroutine_threadsafe(self._submit("main", text), self._loop)
        except RuntimeError:
            pass

    # ---- 出站（Engine 事件 → 渲染）----
    async def render(self, ev):
        if isinstance(ev, events.TurnStarted):
            self._buf = []
            self._msg_id = await self._run(self._fs.send_card,
                                           formatter.format_status_card("正在处理…"))
            self._schedule_flush()
        elif isinstance(ev, events.TextDelta):
            self._buf.append(ev.text)
        elif isinstance(ev, (events.TextDone, events.TurnResult)):
            await self._flush_now(final=True)

    def _schedule_flush(self):
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        try:
            while True:
                await asyncio.sleep(self._flush)
                await self._flush_now(final=False)
        except asyncio.CancelledError:
            pass

    async def _flush_now(self, final: bool):
        if not self._buf or self._msg_id is None:
            return
        text = "".join(self._buf)
        card = formatter.format_assistant_reply(text)
        await self._run(self._fs.update_card, self._msg_id, card)
        if final:
            self._buf = []

    async def _run(self, fn, *args):
        # 阻塞 SDK 调用一律走线程池，绝不卡事件循环
        return await self._loop.run_in_executor(None, fn, *args)

    async def aclose(self):
        if self._flush_task:
            self._flush_task.cancel()
```

> 注：`formatter.format_status_card` / `format_assistant_reply` 若现有签名不同，按 `formatter.py` 实际函数名调整（Task 13 接线时核对）。本 Task 测试用 `FakeFeishu`，不依赖真飞书。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/cloudbridge/test_gateway.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/gateway.py tests/cloudbridge/test_gateway.py
git commit -m "feat(cloudbridge): FeishuGateway 线程边缘+占位渲染(delta合并)"
```

---

## Task 13: 组装入口 + 端到端集成测试

**Files:**
- Create: `cloudbridge/app.py`
- Create: `tests/cloudbridge/test_integration.py`
- Modify: `bridge.py`（新增 `--core stream-json` 分支，调用 `cloudbridge.app.run`；默认仍走现有 tmux 路径）
- Modify: `requirements.txt`（加 `pytest-asyncio>=0.23`）

**Interfaces:**
- Consumes: 全部上述组件。
- Produces: `cloudbridge.app.build_engine(...)`（组装 Engine + Gateway + Store + Lock，供测试与真实入口共用）；`cloudbridge.app.run(config)`（真实入口）。

- [ ] **Step 1: 加依赖并写端到端失败测试（全程 FakeClaude，不依赖真 claude / 真飞书）**

先在 `requirements.txt` 追加一行 `pytest-asyncio>=0.23`，安装：`pip install -r requirements.txt`。

```python
# tests/cloudbridge/test_integration.py
import asyncio, json, os, sys, pytest
from cloudbridge import events
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter
from cloudbridge.stream_json_driver import StreamJsonDriver

class FakeFeishu:
    def __init__(self): self.sends=[]; self.updates=[]
    def send_card(self, card): self.sends.append(card); return "m1"
    def update_card(self, mid, card): self.updates.append((mid, card)); return True

@pytest.mark.asyncio
async def test_message_to_rendered_reply_end_to_end(tmp_path):
    fix = tmp_path / "fix.jsonl"
    fix.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Hello!"}}},
        {"type": "result", "subtype": "success", "result": "Hello!",
            "session_id": "S", "usage": {}, "cost_usd": 0.0},
    ]) + "\n")
    env = {**os.environ, "FAKE_FIXTURE": str(fix)}
    argv = [sys.executable, "scripts/fakeclaude.py"]

    loop = asyncio.get_running_loop()
    health = HealthModel()
    fk = FakeFeishu()
    rendered = []
    eng = Engine(health=health, on_event=lambda e: rendered.append(e))
    gw = FeishuGateway(loop, eng.submit, fk, InboundFilter(start_ts=0.0), flush_ms=10)
    # 让渲染也吃 Engine 事件
    eng.on_event = lambda e: asyncio.create_task(gw.render(e))

    drv = StreamJsonDriver("main", argv, cwd=str(tmp_path), session_id="S", env=env)
    await drv.start()
    eng.add_session("main", drv, active=True)
    await eng.start()

    gw.on_inbound("m1", create_time_ms=10_000, text="hi")
    # 等回合完成
    for _ in range(50):
        await asyncio.sleep(0.05)
        if any(isinstance(e, events.TurnResult) for e in rendered):
            break
    await eng.stop(); await drv.close(grace_stop=0.2, grace_term=0.5); await gw.aclose()

    assert any(isinstance(e, events.TextDelta) and e.text == "Hello!" for e in rendered)
    assert any(isinstance(e, events.TurnResult) for e in rendered)
    assert fk.sends    # 至少发过一张占位卡
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/cloudbridge/test_integration.py -v`
Expected: FAIL（`cloudbridge.app` 缺失 / 接线缺口）。按报错补齐 `app.py`。

- [ ] **Step 3: 实现 app.py 组装 + bridge.py 开关**

```python
# cloudbridge/app.py
import asyncio, time, uuid
from cloudbridge.engine import Engine
from cloudbridge.gateway import FeishuGateway
from cloudbridge.health import HealthModel
from cloudbridge.inbound_filter import InboundFilter
from cloudbridge.lock import SingleInstanceLock
from cloudbridge.session_store import SessionStore, SessionRecord
from cloudbridge.stream_json_driver import StreamJsonDriver

CANONICAL_ARGV = [
    "claude", "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose", "--include-partial-messages", "--replay-user-messages",
    # 权限 flag 由 Task 0 spike 结论填入（stdio 或自定义 MCP 权限工具）
]

def _argv_for(session_id, cwd):
    return CANONICAL_ARGV + ["--session-id", session_id]

def build(loop, feishu_client, cwd, start_ts, max_sessions=3):
    health = HealthModel()
    eng = Engine(health=health, on_event=lambda e: None, max_sessions=max_sessions)
    gw = FeishuGateway(loop, eng.submit, feishu_client,
                       InboundFilter(start_ts=start_ts))
    eng.on_event = lambda e: asyncio.create_task(gw.render(e))
    eng.on_backpressure = lambda name, depth: feishu_client.send_text(
        f"[{name}] 队列已满（{depth} 条在排队），稍后再试")
    return eng, gw, health

async def run(feishu_client, cwd):
    """真实入口：被 bridge.py --core stream-json 调用。"""
    lock = SingleInstanceLock("~/.feishu-claude-bridge/bridge.lock")
    if not lock.acquire():
        raise SystemExit("another bridge instance is running")
    loop = asyncio.get_running_loop()
    eng, gw, health = build(loop, feishu_client, cwd, start_ts=time.time())
    sid = str(uuid.uuid4())
    drv = StreamJsonDriver("main", _argv_for(sid, cwd), cwd=cwd, session_id=sid)
    await drv.start()
    eng.add_session("main", drv, active=True)
    await eng.start()
    # 飞书 WS 在自己的线程 start()，回调里调用 gw.on_inbound(...)
    await asyncio.Event().wait()   # 常驻
```

在 `bridge.py` 的 `main()` / 参数解析处新增分支（伪代码，按现有 argparse 风格落实）：

```python
# bridge.py（新增，不动现有默认路径）
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--core", choices=["tmux", "stream-json"], default="tmux")
args, _ = parser.parse_known_args()
if args.core == "stream-json":
    import asyncio
    from cloudbridge import app
    # feishu_client 用现有 FeishuClient 构造，on_message 回调改为 gw.on_inbound
    asyncio.run(app.run(feishu_client, cwd=os.getcwd()))
    return
# 否则走现有 tmux 逻辑（保持不变）
```

- [ ] **Step 4: 跑全部测试确认通过**

Run: `pytest tests/cloudbridge/ -v`
Expected: PASS（全绿）

并确认现有测试未被破坏：
Run: `pytest tests/ -q`
Expected: 现有用例仍 PASS（新代码未改动旧模块行为）

- [ ] **Step 5: 提交**

```bash
git add cloudbridge/app.py tests/cloudbridge/test_integration.py bridge.py requirements.txt
git commit -m "feat(cloudbridge): 组装入口 app.py + bridge --core 开关 + 端到端集成测试"
```

---

## Task 14: e2e 冒烟脚本（需真 claude，本地手动，非 CI）

**Files:**
- Create: `scripts/smoke_stream_json.md`（手动步骤清单）

**Interfaces:** 无（文档）。

- [ ] **Step 1: 写冒烟清单**

```markdown
# SP1 e2e 冒烟（手动，需真 claude + 真飞书测试群）
1. 配置 .env（沿用现有）。
2. 启动：python bridge.py --core stream-json
3. 在飞书群发"列出当前目录文件" → 期望：占位卡流式更新出文件列表，回合结束有 result 脚注。
4. 发一个需要权限的操作（如写文件）→ 期望：收到 y/n 文本提示；回 y 后执行，回合完成。
5. 不回应权限 5 分钟 → 期望：自动 deny + 提示，进程不卡死。
6. 回合进行中连发两条 → 期望：第二条排队，第一条 result 后才处理，进程不挂起。
7. 手动 kill 掉 claude 子进程 → 期望：自动 --resume 恢复 + "会话已自动恢复"卡。
8. 重启 bridge → 期望：不重放启动前的旧消息（水位线生效）。
```

- [ ] **Step 2: 本地执行清单并记录结果**（开发者手动，逐条打勾）

- [ ] **Step 3: 提交**

```bash
git add scripts/smoke_stream_json.md
git commit -m "docs(cloudbridge): SP1 e2e 冒烟清单"
```

---

## 自检对照（spec 覆盖）

- §2 并发模型(asyncio+线程边缘) → Task 6/9/12（asyncio.subprocess、run_coroutine_threadsafe、run_in_executor）。
- §3.1 Engine → Task 9/10/11；§3.2 StreamJsonDriver → Task 6/7/8；§3.3 Gateway(delta合并) → Task 12；§3.4 SessionStore → Task 2；§3.5 HealthModel → Task 4；§3.6 Lock → Task 3。
- §4 事件模型 → Task 1；TurnStarted 来源(replay) → Task 6；result subtype 守卫(C3) → Task 6。
- §5 稳定性十条 → 水位线/去重 Task 11；背压(C2) Task 9；原子落盘 Task 2；优雅关闭(120s+进程组) Task 8；单实例锁 Task 3；空闲超时 → 见下方补遗。
- §6 崩溃自愈(+学到的session_id) → Task 8。
- §7 权限(C1) → Task 7 + Task 0 spike + Task 13 注入 argv。
- §8 测试(spike gate + FakeClaude + 单元/集成/e2e) → Task 0/5/各 Task/Task 13/Task 14。
- §9 迁移(--core 开关、不删旧码) → Task 13。
- §10 验收 → Task 13 集成 + Task 14 e2e 覆盖各条。

**补遗（自检发现的缺口）：空闲超时（§5）尚未单独成 Task。** 在 Task 9 的 `_consume` 中补一个 per-session 看门狗：每收到任意事件刷新 `last_event_ts`；一个后台协程每秒检查，`now - last_event_ts > idle_timeout` 且 `busy` 时，发出"似乎卡住"提示并将 `busy` 置回（解锁、drain 队列）。实现时作为 Task 9 的附加 step（写一条 `test_idle_timeout_unlocks_busy_session` 测试：feed 一个 TurnStarted 后不再 feed，快进 `idle_timeout`，断言 busy 复位）。**执行 Task 9 时一并完成此附加 step。**

---

## 追加任务（最终全分支审查后：接通 run() 真实路径）

> 最终审查发现：组件全部建好且测试通过，但 `run()` 没接通 supervise 崩溃自愈、权限 y/n、多会话命令。用户决定全部接通，使 SP1 真正满足 §10 DoD。以下两任务均 TDD。

### Task 15: run() 编排 —— 崩溃自愈接线 + 多会话命令 + 活跃会话路由

**Files:** Modify `cloudbridge/app.py`, `cloudbridge/gateway.py`, `cloudbridge/engine.py`（仅按需新增方法）；Test `tests/cloudbridge/test_app_orchestration.py`

**要点：**
- `app.run()`：每个会话 `asyncio.create_task(driver.supervise(on_event=engine.on_event))`，让崩溃自动 `--resume` 重启在真实路径生效。
- `driver_factory(name)`：构造 `StreamJsonDriver(name, CANONICAL_ARGV+["--session-id", uuid4], cwd, session_id=uuid4)`，供 `engine.create_session` 用。
- 命令解析（在 app 层或一个 `cloudbridge/commands.py`）：入站文本以 `/` 开头时解析 `/new <name>` / `/switch <name>` / `/list` / `/delete <name>` → 调 `engine.create_session(name, driver_factory)`（并 `await driver.start()` + 起 supervise）/ `switch_session` / `list_sessions`（回 Feishu 文本）/ `delete_session`；非命令文本 → dispatch 到 `engine.active_session_name`（不再硬编码 `"main"`）。
- `gateway.render`：新增 `SessionCrashed` / `SessionRecovered` 分支 → 发 `formatter.format_status_notification(...)` 状态卡。
- **测试**（用 FakeDriver / FakeFeishu）：`/new` 创建会话且后续文本路由到它；`/switch` 改活跃；`/list` 返回；`/delete` 移除；普通文本进活跃会话；`SessionCrashed` 渲染一张卡。

### Task 16: 权限 y/n 端到端（文本 + 超时；按钮留 SP3）

**Files:** Modify `cloudbridge/engine.py`, `cloudbridge/gateway.py`, `cloudbridge/app.py`；Test `tests/cloudbridge/test_permission_flow.py`

**要点：**
- Engine 持有 `pending_permission: dict[session → request_id]`。`_consume` 见到 `PermissionRequest` 事件时记录 pending，并启动一个 `permission_timeout`（默认 300s，可配）的定时任务：到点未答 → 自动 `answer(deny)` + 清 pending + 通知。
- `Engine.answer_permission(session, allow)`：调 `session.driver.answer_permission(request_id, allow)`，清 pending，取消超时任务。
- 入站路由（app 层）：若目标会话有 pending permission 且文本是 `y`/`yes`/`n`/`no`（大小写不敏感）→ `engine.answer_permission(session, allow)`；否则照常 submit。
- `gateway.render`：新增 `PermissionRequest` 分支 → 发 `formatter.format_permission_request(tool_name, input_summary, request_id)` 卡（纯展示，回复走文本 y/n；漂亮按钮 §7 留 SP3）。
- **测试**：PermissionRequest 记录 pending 并渲染；随后 `y` → answer(allow) 被调、pending 清；`n` → deny；超时（小 timeout 值）→ 自动 deny；pending 期间普通非 y/n 文本不误触发（按 submit 处理或提示）。

每个任务遵循 TDD（红/绿）、频繁提交，提交信息附 Co-Authored-By/Claude-Session 行。

---

## Execution Handoff（见下）
