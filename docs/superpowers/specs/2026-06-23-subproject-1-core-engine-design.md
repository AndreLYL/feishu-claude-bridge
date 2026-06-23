# 子项目 1 设计：核心引擎 + stream-json 驱动 + 稳定性地基

> 状态：设计定稿（待用户评审）
> 日期：2026-06-23
> 上位文档：`2026-06-23-feishu-cloud-bridge-vision-design.md`（顶层愿景）
> 本子项目是另外两块（流式工单卡、交互/页面化）的**地基**，目标支柱：**稳**。

---

## 1. 目标与范围

把现有 `tmux send-keys + JSONL 1 秒轮询` 的数据通路，替换为 **stream-json 常驻子进程**驱动，并引入**中央 Engine** 与一整套稳定性机制，使桥接"稳得像后台服务"。

**一上来就支持多会话**（Engine 管理 N 个常驻 `claude` 子进程，保留 `/new /switch /list /delete`）。

**范围内**：Engine 编排器、SessionDriver 抽象 + StreamJsonDriver、SessionStore 原子持久化、FeishuGateway 线程边缘适配、HealthModel 健康数据模型、SingleInstanceLock、稳定性十条、进程崩溃自愈、权限临时策略（文本 y/n + 超时）。

**范围外（YAGNI / 留给后续子项目）**：漂亮的流式工单卡与富产出渲染（SP2）；卡片按钮/页面化控制台/健康仪表卡可视化/主动建议按钮（SP3）；飞书多维表格/文档集成（后期）。SP1 渲染先沿用现有 `formatter.py` 当**占位**。

---

## 2. 并发模型：asyncio 核心 + 线程边缘（方案 C）

- **Engine 跑单一 asyncio 事件循环**：所有状态变更串行发生于此 → 天然规避竞态。超时、取消、背压、有序都用 asyncio 原语实现。
- **`claude` 子进程**用 `asyncio.subprocess` 读写（stdin 写 user JSON、stdout 逐行读事件）。
- **飞书 `lark-oapi` WS 客户端**是同步/线程式的，隔离在 `FeishuGateway` 这个**线程边缘适配器**里：WS 回调在自己的线程接收消息，通过 `asyncio.run_coroutine_threadsafe` / 线程安全队列把入站事件投递进 Engine 的 loop。出站发卡同理跨回线程调用。

> 理由：稳定性要的"超时/取消/背压/有序"在 asyncio 是一等公民；飞书 SDK 的线程特性被封死在一个适配器里，不污染核心。与 CC Connect "中央 Engine 单一编排"一致。

---

## 3. 组件划分（各自职责单一、可独立测试）

```
┌──────────────────────────────────────────────────────────┐
│                        Engine (asyncio)                     │
│  入站路由 · 回合生命周期 · 去重+水位线 · 有界队列+背压       │
│  持有 SessionRegistry · HealthModel                         │
└───────┬───────────────────────┬──────────────────┬─────────┘
        │                       │                  │
┌───────▼────────┐   ┌──────────▼─────────┐  ┌─────▼──────────┐
│ SessionDriver   │   │  FeishuGateway      │  │ SessionStore    │
│ (接口)          │   │  (线程边缘适配器)    │  │ (原子落盘/恢复)  │
│ └ StreamJson    │   │  WS 收→投递队列      │  │  sessions.json  │
│    Driver       │   │  发卡/更新卡(占位)   │  └────────────────┘
└─────────────────┘   └─────────────────────┘
                      ┌─────────────────────┐
                      │ SingleInstanceLock   │ (flock 防多开)
                      └─────────────────────┘
```

### 3.1 Engine（`engine.py`）
唯一"大脑"。职责：
- 入站消息路由（命令 vs 普通消息 vs 权限回复 vs 图片）；
- 维护 `SessionRegistry`（`{name → driver}` + active 指针）；
- 每会话回合生命周期：未出 `result` 不接新回合，新消息进该会话有界队列；
- 去重 + 启动水位线（与 Gateway 协作）；
- 慢操作埋点；空闲超时判定回合结束；
- 持有 `HealthModel`，汇聚各会话/连接/队列/错误状态。

### 3.2 SessionDriver 接口 + StreamJsonDriver（`drivers/`）
- `SessionDriver` 抽象接口：`start() / send(user_msg) / events() / close() / answer_permission()`。未来可插回 `TmuxDriver`。
- `StreamJsonDriver`：
  - 启动 `claude --session-id <uuid> -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages`（确切 flag 以 spike 实测为准）；
  - 写 stdin：`{"type":"user","message":{"role":"user","content":...}}`；
  - 读 stdout 逐行 JSON，归一化为内部事件（见 §4）；
  - 识别 `result` = 回合结束（带 usage/cost/session_id）；
  - 处理 `control_request`（权限）→ 交 Engine 决策 → 写 `control_response`；
  - 优雅关闭三段式（关 stdin → SIGTERM → SIGKILL）；
  - 进程意外退出 → 退避自动重启 + `--resume <session_id>` 续接 + 重启计数。

### 3.3 FeishuGateway（`gateway.py`）
- 把 `lark-oapi` 线程世界与 Engine asyncio 世界隔开。
- 入站：WS 收消息/图片/卡片回调 → 线程安全投递进 Engine。
- 出站：发卡/更新卡（SP1 用旧 `formatter.py` 占位）。
- 入站做**启动水位线**（丢弃 `create_time < start_ts`）+ msg_id 去重（增强现有 `feishu_client.py` 逻辑）。

### 3.4 SessionStore（`session_store.py`）
- 持久化会话清单：每会话 `name / session_id(uuid) / cwd / created_at / active`。
- **原子落盘**：写临时文件 + `os.replace`。
- 启动恢复：读清单，对每个会话用 `--resume <session_id>` 重建 driver。

### 3.5 HealthModel（`health.py`）
- 聚合：连接状态、各会话进程存活/busy、各会话队列深度、最近 N 条错误、各会话重启计数。
- SP1 只建模型 + 写日志；可视化健康卡留 SP3（签名特色③的数据来源）。

### 3.6 SingleInstanceLock（`lock.py`）
- 启动时 flock `~/.feishu-claude-bridge/bridge.lock`，已锁则拒绝启动并提示。

---

## 4. 内部事件模型（Engine ↔ 渲染的解耦边界）

Driver 把 `claude` 的 stream-json 归一化为稳定的内部事件，Engine 据此驱动渲染。这样 **SP2 换漂亮工单卡时只改 renderer，不动 Engine**。

事件类型（草案，spike 后定稿）：
- `TurnStarted{session, user_text}`
- `TextDelta{session, text}` / `TextDone{session, full_text}`
- `Thinking{session, text}`
- `ToolUse{session, name, input_summary}` / `ToolResult{session, name, status, exit_code?}`
- `PermissionRequest{session, request_id, tool, input}`
- `TurnResult{session, usage, cost, duration_ms}`
- `HealthChanged{snapshot}`
- `SessionRecovered{session}` / `SessionCrashed{session, restarts}`

---

## 5. 稳定性十条 → 落点

| 机制 | 落点 |
|---|---|
| 去重 + **启动水位线** | FeishuGateway：记 `start_ts`，丢弃旧消息 + msg_id 去重 |
| **有界队列 + 背压** | Engine：每会话 `pending` 上限 N，满则回复"队列已满（N 条在排队）" |
| 会话状态**原子落盘** | SessionStore：tmp + `os.replace` |
| **优雅关闭三段式** | Driver.close()：关 stdin → SIGTERM → SIGKILL |
| **重试 + 退避** | 发卡失败 0/0.5/1.5s 重试；token 刷新失败重试 |
| **事件空闲超时** | Engine：回合内 N 秒无事件 → 判回合结束、解锁、提示"似乎卡住" |
| **慢操作埋点** | 发卡>2s、首事件>15s → WARN |
| per-session 互斥 + busy | Driver `busy` 标志；心跳 try-跳过 |
| **单实例锁** | 启动 flock |
| daemon + 日志轮转 | 沿用 launchd，补 systemd + 按大小轮转 |

---

## 6. 进程监督 / 崩溃自愈

- 子进程意外退出（非用户删除）→ 按退避自动重启 + `--resume <session_id>` 续接 + 发"会话已自动恢复"占位状态卡。
- 重启风暴保护：1 分钟内 >3 次重启 → 停手并报"会话反复崩溃，请检查"，置该会话为 `failed`。
- 信号喂 HealthModel。

---

## 7. 权限处理（SP1 临时策略）

- `control_request` 不响应会**死锁**，必须处理。
- SP1：权限请求转**文本卡**（沿用现有 `y`/`n` 回复机制）转发飞书；设**超时**（默认 5 分钟无回应 → 自动 deny + 提示），杜绝永久阻塞。
- 漂亮授权按钮留 SP3。
- `--permission-mode` 默认值与 `control_request`/`control_response` 确切字段由 spike 实测确认（风险 R1）。

---

## 8. 测试策略（TDD）

1. **先 spike**：最小脚本实测 stream-json 真实 flag / `result` / `control_request` 字段，录制 fixture（消除 R1）。
2. **`FakeClaudeProcess`**：driver 面向"吐 stream-json 行的进程"编程；测试用回放 fixture 的桩进程代替真 `claude` → 多数测试**不依赖真 Claude、可进 CI**。
3. **单元测试**（红/绿）：
   - Engine：路由、去重+水位线、有界队列+背压、回合生命周期、空闲超时
   - SessionStore：原子落盘、崩溃恢复、并发写
   - StreamJsonDriver：fixture 驱动解析、优雅关闭、自愈重启计数
   - HealthModel：状态聚合
4. **集成测试**：FakeClaude + 内存版 Gateway，跑通"消息→回合→result→占位渲染"；模拟崩溃验证 resume 自愈；模拟队列打满验证背压。
5. **e2e 冒烟**（需真 `claude`，本地手动）：真实完整回合 + 一次权限 y/n。

---

## 9. 迁移路径（不破坏现状、可回退）

- 新代码放新模块（`engine.py` / `drivers/stream_json.py` / `session_store.py` / `health.py` / `gateway.py` / `lock.py`），**不动**现有 `bridge.py` 主流程。
- 入口开关：`python bridge.py --core stream-json`（新）vs 现有 tmux 行为（默认保留）。SP1 验收通过前旧路径始终可跑。
- `.env` / 多会话配置 / launchd 沿用。
- 旧 tmux 代码暂不删（`TmuxDriver` 素材 + 回退保险，愿景 R4）；新核心稳了再清理。

---

## 10. 验收标准（Definition of Done）

- ✅ 能稳定收发一个完整回合（多会话各自独立、串行不交叉）。
- ✅ 杀掉 bridge 重启：不丢消息、不重放旧消息（水位线生效）。
- ✅ 杀掉某个 `claude` 子进程：自动 `--resume` 恢复并通知。
- ✅ 队列打满：可见背压提示，不崩、不静默丢。
- ✅ 权限请求超时自动 deny，不死锁。
- ✅ 全部单元 + 集成测试绿（CI 不依赖真 claude）。

---

## 11. 风险

- **R1（高）**：stream-json 确切 flag / 权限事件字段版本相关 → spike 优先实测。
- **R2（中）**：飞书 WS 线程与 asyncio 跨界投递的正确性/背压 → 集成测试覆盖。
- **R3（中）**：`--resume` 在常驻 stream-json 下的上下文连续性 → spike + e2e 验证。
- **R4（低）**：多会话下 N 个常驻 `claude` 进程的资源占用 → 受 `MAX_SESSIONS` 约束，HealthModel 监控。

---

## 12. 后续

设计经用户评审通过后，调用 **writing-plans** 技能产出实现计划（spike → TDD 逐组件）。
