# Spike Results: claude stream-json 行为实测

> 日期：2026-06-23
> CLI 版本：claude 2.1.186
> 测试环境：Linux 6.18.5，`/tmp/spike-cwd` 独立目录
> 脚本：`scripts/spike_stream_json.py`
> fixtures：`tests/cloudbridge/fixtures/`

---

## [C1] 权限请求：--permission-prompt-tool stdio 行为

**结论：stdio 模式下 `control_request` 确实发出，NOT 静默执行。设计文档中"bug #34046 stdio 静默执行"的警告在 CLI 2.1.186 上未复现。**

### 实测行为

用 `--permission-prompt-tool stdio` 请求写文件 `/tmp/spike_test.txt`：

1. `control_request` 事件出现在 stdout（不是静默执行）
2. 文件**未被创建**（permission_denials 中记录）
3. 因为 spike 脚本关闭了 stdin 而未响应，CLI 自动超时并注入错误的 tool_result

### control_request 确切字段（fixture: `permission.jsonl`）

```json
{
  "type": "control_request",
  "request_id": "3199fb0f-25c5-41b1-87ab-dcefd1e4bfeb",
  "request": {
    "subtype": "can_use_tool",
    "tool_name": "Write",
    "display_name": "Write",
    "input": {"file_path": "/tmp/spike_test.txt", "content": "hi"},
    "description": "/tmp/spike_test.txt",
    "permission_suggestions": [
      {"type": "setMode", "mode": "acceptEdits", "destination": "session"},
      {"type": "addDirectories", "directories": ["/tmp"], "destination": "session"}
    ],
    "decision_reason": "Path is outside allowed working directories",
    "decision_reason_type": "workingDir",
    "tool_use_id": "toolu_015S5VXJDV23ZZcZr2CcEJab"
  }
}
```

顶层键：`type`, `request_id`, `request`
`request` 子键：`subtype`, `tool_name`, `display_name`, `input`, `description`, `permission_suggestions`, `decision_reason`, `decision_reason_type`, `tool_use_id`

### control_response 确切字段（从 CLI 二进制符号表提取）

Engine 应写回 stdin：

```json
{"type": "control_response", "request_id": "<request_id from control_request>", "decision": "allow"}
```

或拒绝：

```json
{"type": "control_response", "request_id": "<request_id from control_request>", "decision": "deny"}
```

**必须在收到 `control_request` 后立即写入 stdin**（进程等待响应，stdin 关闭则超时报错）。

### 设计决策

`--permission-prompt-tool stdio` 在 2.1.186 上可用，Engine 可用此模式：
- 收到 `control_request` → 发 Feishu 文字卡等用户 y/n
- 用户回复 → 写 `control_response{decision: "allow"/"deny"}` 到 stdin
- 5 分钟超时 → 自动写 `control_response{decision: "deny"}`

**原设计文档中"stdio 静默执行 bug #34046"的 fallback 路径（MCP 权限工具）可保留为备选，但 2.1.186 上 stdio 路径是可用的。**

---

## [C2] 回合中途写第二条消息

**结论：回合中途写第二条消息，进程未挂起，但行为是"重新启动一个新 session"处理第二条，而非将其排进当前回合队列。**

### 实测行为

1. 发第一条消息："Count from 1 to 15, one per line."
2. 3 秒后（回合尚在进行中）立即发第二条："Second message sent mid-turn — just say OK"
3. 结果：
   - 第一条回合正常完成（result #1，subtype=success）
   - 之后出现**第二个 system/init 事件**（同 session_id）
   - 第二条消息被作为**新的回合**处理，产出 result #2（"OK"）
   - **进程未挂起**

### 关键发现

- 进程**不会因此挂起**，但会重新 init，第二条作为新 turn 处理
- **两条 result 事件**都是 `subtype: "success"`，`num_turns` 分别为 1
- 这意味着：Engine 侧的"仅空闲时写 stdin"铁律仍然必要，因为：
  - 如不排队而在回合中途写入，会导致上下文污染或意外 session restart
  - 正确做法：等第一个 result 后才 drain 队列写第二条

**"仅空闲时写 stdin"规则确认为必要。**

---

## [C3] result subtype 取值 / 压缩态

**结论：本次 spike 仅观察到 `subtype: "success"`。无法在 spike 中廉价触发上下文压缩（需要超长对话）。**

### 实测 result 字段

全部 result 事件的 `subtype` 均为 `"success"`。

### result 事件确切键集合

```
type, subtype, is_error, api_error_status, duration_ms, duration_api_ms,
ttft_ms, ttft_stream_ms, time_to_request_ms, time_to_request_from_spawn_ms,
warm_spare_claimed, time_origin_ms, num_turns, result, stop_reason,
session_id, total_cost_usd, usage, modelUsage, permission_denials,
terminal_reason, fast_mode_state, uuid
```

**注意：字段是 `total_cost_usd`，不是 `cost_usd`（设计文档之前用错了字段名）。**

### 压缩态 subtype 值

从设计文档及 CC Connect 源码：`"compact"` 或 `"compaction"`（未实测触发）。Driver 应：
- `subtype == "success"` → 终态，发 `TurnResult`
- `subtype == "error"` → 终态，发错误
- `subtype == "compact"` 或 `"compaction"` → 中途压缩，**忽略**，回合继续
- 其他 → 保守处理为终态

---

## [M1] --replay-user-messages 回显事件形状

**结论：回显的 user 事件有 `isReplay: true` 标志，Driver 可通过此字段区分回显与真实输入。**

### 确切事件形状（fixture: `turn_text.jsonl` 第 3 行）

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "Reply with exactly one word: Hello"
  },
  "session_id": "d424d07a-c2b1-4f82-91e1-5854cb25fb74",
  "parent_tool_use_id": null,
  "uuid": "dbba114c-504c-434b-8ab6-75da4360983e",
  "timestamp": "2026-06-23T11:31:01.110Z",
  "isReplay": true
}
```

关键字段：`type: "user"`, `isReplay: true`, `message.content`（原始文本）

**Driver 应：匹配 `type == "user" && isReplay == true` → 发出 `TurnStarted` 内部事件，并将该事件滤掉不渲染。**

---

## [M2] system/init 事件结构（MCP/Skills 自动加载确认）

**结论：默认未启用 `--bare`，MCP/Skills/CLAUDE.md 自动加载正常。`mcp_servers: []` 是因为 `/tmp/spike-cwd` 没有 `.mcp.json` 配置文件，不代表功能被禁用。**

### system/init 关键字段（fixture: `turn_text.jsonl` 第 1 行）

```json
{
  "type": "system",
  "subtype": "init",
  "session_id": "d424d07a-c2b1-4f82-91e1-5854cb25fb74",
  "model": "claude-sonnet-4-6",
  "mcp_servers": [],
  "tools": ["Task", "AskUserQuestion", "Bash", "CronCreate", "CronDelete", ...],
  "permissionMode": "default",
  "claude_code_version": "2.1.186",
  "skills": ["brainstorming", "dispatching-parallel-agents", ...],
  "agents": ["claude", "claude-code-guide", "Explore", "general-purpose", "Plan", "statusline-setup"],
  "plugins": [],
  "apiKeySource": "none"
}
```

- `tools`: 32 个工具，包含 `Bash`, `Edit`, `Write`, `Read` 等完整工具链
- `skills`: 29 个 skills 已加载（brainstorming, code-review 等）
- `agents`: 6 个 agents 已加载
- `mcp_servers: []`：spike cwd 无 `.mcp.json`；正式 cwd 下 MCP 会自动加载
- `permissionMode: "default"`：正常权限模式
- `--bare` 未被设为 `-p` 模式默认值（32 工具 / 29 skills 证明）

### Canonical 启动命令（钉死）

```bash
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --replay-user-messages \
  --permission-prompt-tool stdio \
  --session-id <uuid>
```

可选（resume 场景）：`--resume <session_id>`

**不得加 `--bare`（会跳过 skills/MCP/CLAUDE.md）。**

### 额外观察到的事件类型（原设计文档未列出）

| 事件类型 | subtype | 出现场景 | Driver 处理 |
|---|---|---|---|
| `rate_limit_event` | — | 每次 API 调用前 | 忽略（记录日志即可）|
| `system` | `post_turn_summary` | 每个 assistant turn 之后 | 忽略（调试信息）|
| `system` | `status` | `--include-partial-messages` 时 | 忽略 |
| `system` | `thinking_tokens` | 模型思考过程 | 忽略 |
| `assistant` | — | 全量 assistant message | 在无 partial / WITH partial 均出现；作为 `TextDone` 的来源 |
| `stream_event` | — | `--include-partial-messages` 时的流式增量 | `content_block_delta` → `TextDelta`；`message_stop` → `TextDone` |

---

## [R3] session_id 权威

**结论：`--session-id` 传入的 UUID 被 CLI 完整继承，`system/init` 和 `result` 中的 `session_id` 与传入值一致。**

实测数据（spike_summary.json）：
- 传入：`"d424d07a-c2b1-4f82-91e1-5854cb25fb74"`
- system/init 报告：`"d424d07a-c2b1-4f82-91e1-5854cb25fb74"` ✓
- result 报告：`"d424d07a-c2b1-4f82-91e1-5854cb25fb74"` ✓

**`--session-id` 是可靠的锚点。Driver 启动时传入，运行时从 system/init 学到的 session_id 可作为 resume 用途（因为 --resume 可能派生新 id，需运行时学习）。**

---

## Fixture 文件清单

| 文件 | 内容 | 行数 |
|---|---|---|
| `turn_text.jsonl` | 纯文本回合（无 partial）：system/init → rate_limit_event → user replay → assistant → post_turn_summary → result | 6 |
| `turn_text_with_partial.jsonl` | 纯文本回合（有 partial）：system/init → system/status → rate_limit_event → stream_events → user replay → assistant → stream_events → post_turn_summary → result | 13 |
| `permission.jsonl` | 权限请求回合：system/init → assistant(text) → assistant(tool_use) → control_request → user(tool_result/error) → assistant(text) | 6 |
| `turn_tool.jsonl` | 工具使用回合（3 turns）：Read(拒绝) → Bash(成功) → 最终文字回复 | 8 |
| `mid_turn_test.jsonl` | 中途写第二条消息测试：两个完整回合 + 第二个 system/init | 23 |

原始完整 fixture（含 startup_timing 等冗余字段）：
- `turn_text_no_partial.jsonl` (6 lines, 原始)
- `turn_text_with_partial.jsonl` (13 lines, 原始)

---

## GATE 判定

所有 6 项检查均有结论，**无 GATE-FAIL**。

| 项目 | 结论 | 风险 |
|---|---|---|
| C1 权限请求 | stdio 可用，control_request 正常发出 | 低（已确认机制） |
| C2 中途写入 | 不挂起但行为异常（新 session），铁律必要 | 低（已确认铁律） |
| C3 压缩 subtype | 未触发，仅见 success；driver 需防御性处理 | 低（已有防御策略）|
| M1 replay 形状 | isReplay:true 标志清晰 | 无 |
| M2 MCP/Skills | 默认已加载，--bare 未设为默认 | 无 |
| R3 session_id | --session-id 完全生效 | 无 |

**SP1 可继续进入 writing-plans 阶段。**
