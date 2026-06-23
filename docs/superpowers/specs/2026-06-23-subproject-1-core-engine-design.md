# 子项目 1 设计：核心引擎 + stream-json 驱动 + 稳定性地基

> 状态：设计定稿（待用户评审）
> 日期：2026-06-23
> 上位文档：`2026-06-23-feishu-cloud-bridge-vision-design.md`（顶层愿景）
> 本子项目是另外两块（流式工单卡、交互/页面化）的**地基**，目标支柱：**稳**。

---

## 1. 目标与范围

把现有 `tmux send-keys + JSONL 1 秒轮询` 的数据通路，替换为 **stream-json 常驻子进程**驱动，并引入**中央 Engine** 与一整套稳定性机制，使桥接"稳得像后台服务"。

**一上来就支持多会话**（Engine 管理 N 个常驻 `claude` 子进程，保留 `/new /switch /list /delete`）。注意每个常驻 `claude` 还拖一串 MCP 子进程，资源不轻 → `MAX_SESSIONS` 取**现实上限（建议 ≤3）**，见 §11 R4。多会话工作与 §8.0 spike 结果挂钩：spike 若暴露重大障碍，可临时退到单会话先跑通（SP1b 再补多会话）。

**范围内**：Engine 编排器、SessionDriver 抽象 + StreamJsonDriver、SessionStore 原子持久化、FeishuGateway 线程边缘适配、HealthModel 健康数据模型、SingleInstanceLock、稳定性十条、进程崩溃自愈、权限临时策略（文本 y/n + 超时）。

**范围外（YAGNI / 留给后续子项目）**：漂亮的流式工单卡与富产出渲染（SP2）；卡片按钮/页面化控制台/健康仪表卡可视化/主动建议按钮（SP3）；飞书多维表格/文档集成（后期）。SP1 渲染先沿用现有 `formatter.py` 当**占位**。

---

## 2. 并发模型：asyncio 核心 + 线程边缘（方案 C）

- **Engine 跑单一 asyncio 事件循环**：所有状态变更串行发生于此 → 天然规避竞态。超时、取消、背压、有序都用 asyncio 原语实现。
- **`claude` 子进程**用 `asyncio.subprocess` 读写（stdin 写 user JSON、stdout 逐行读事件）。
- **飞书 `lark-oapi` WS 客户端**是同步/线程式的，隔离在 `FeishuGateway` 这个**线程边缘适配器**里：WS 回调在自己的线程接收消息，通过 `asyncio.run_coroutine_threadsafe` / 线程安全队列把入站事件投递进 Engine 的 loop。出站发卡同理跨回线程调用。

> 理由：稳定性要的"超时/取消/背压/有序"在 asyncio 是一等公民；飞书 SDK 的线程特性被封死在一个适配器里，不污染核心。与 CC Connect "中央 Engine 单一编排"一致。

**线程桥三条硬规则（M3，设计层面钉死，不靠测试兜底）**：
1. **阻塞发卡不进 loop**：`send_card`/`update_card` 是同步阻塞 HTTP 调用（`feishu_client.py:78/95`）。一律走 `loop.run_in_executor`（线程池），**绝不**在事件循环里直接 `await` 一个阻塞调用——否则一个慢飞书 API（§5 自己标的 >2s）会卡住**所有会话**的事件处理。
2. **单 loop 全程存活**：事件循环进程内创建一次、活到进程结束。lark WS 回调线程通过 `run_coroutine_threadsafe`/`call_soon_threadsafe` 投递入站；关闭/替换 loop 时对已关闭 loop 调用会抛 `RuntimeError`，入站投递必须对"loop 已关闭"做保护性判断。
3. **入站队列有界**：线程→loop 的入站交接队列**有界 + 丢弃/拒绝策略**。飞书重连会重发消息（`feishu_client.py:129` 已注明），无界队列会被重连风暴淹没。

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
  - 启动命令（**钉死、由 spike 在固定 CLI 版本上验证**）：
    `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages --replay-user-messages --permission-prompt-tool <MCP权限工具> --session-id <uuid>`
    - `-p/--print` 是 `--input-format`/`--include-partial-messages` 的**必需前置**（不可漏）。
    - `--replay-user-messages`：让 CLI 把我们写入的 user 消息回显到 stdout，作为"回合已开始/写入已被吞下"的确认锚点（`TurnStarted` 的来源，M1）。driver 必须**过滤这些回显的 user 事件**，不当成模型输出渲染。
    - `--permission-prompt-tool`：**决定权限请求到底发不发**（见 §7，C1）。取值由 spike 定，**不用 `stdio`**（stream-json 下已知会静默执行、不发请求，bug #34046）。
    - ⚠️ **`--bare` 陷阱（M2）**：`--bare` 会跳过 `.mcp.json`/settings/Skills/CLAUDE.md 自动发现。我们整个"保留 MCP/工具链/Skills"的卖点依赖自动发现**开启**，spike 必须确认默认未启用 `--bare`，并防范未来 CLI 把它设为 `-p` 默认值。
  - 写 stdin：`{"type":"user","message":{"role":"user","content":...}}`。
  - **🔒 关键不变量（C2，承重）**：**仅在会话空闲时写 stdin**（上一个事件是终态 `result` 或刚启动）。回合进行中到达的消息一律缓存在 Engine 队列，**待 `TurnResult` 后才写**。CLI 会把回合中途的 stdin 当成当前回合的一部分 → 永久挂起等一个永不到来的第二个 result（CC Connect engine.go:3025 实证）。
  - 读 stdout 逐行 JSON，归一化为内部事件（见 §4）。
  - **回合结束判定（C3）**：`type:"result"` **不等于**回合结束——上下文压缩会中途发 `result` 且 `subtype:"compact"/"compaction"`。driver 必须**判 `subtype`，仅终态（success/error 等）才发 `TurnResult`**；压缩态忽略、回合继续。同时处理 `control_cancel_request`（取消）。
  - 处理 `control_request`（权限）→ 交 Engine 决策 → 写 `control_response`（§7）。
  - 优雅关闭三段式（关 stdin → 等 → SIGTERM → 等 → SIGKILL），**杀进程组**而非单进程（否则 MCP 孙进程成 100% CPU 孤儿）。超时见 §5。
  - 进程意外退出 → 退避自动重启 + `--resume <session_id>` 续接 + 重启计数。
  - **session-id 权威（M5/N6）**：以**运行时从 `system`/`result` 事件学到的 `session_id` 为准**（`--resume` 可能派生新 id），落盘的是这个学到的 id；resume 用它，避免接到陈旧 transcript。

### 3.3 FeishuGateway（`gateway.py`）
- 把 `lark-oapi` 线程世界与 Engine asyncio 世界隔开。
- 入站：WS 收消息/图片/卡片回调 → 线程安全投递进 Engine（有界队列，§2 规则 3）。
- 出站：发卡/更新卡（SP1 用旧 `formatter.py` 占位），走 `run_in_executor`（§2 规则 1）。
- **delta 合并（N5）**：stream-json 发细粒度 `TextDelta`，但旧 formatter 是整卡 `update_card`。**绝不**每个 delta PATCH 一次（会打爆飞书 API、触发 §5 >2s 告警）。即便占位渲染也要**按时间窗合并**（如每 ~500ms flush 一次）。
- 入站做**启动水位线**（丢弃 `create_time < start_ts − 2s`，N4）+ msg_id 去重（增强现有 `feishu_client.py` 逻辑）。

### 3.4 SessionStore（`session_store.py`）
- 持久化会话清单：每会话 `name / session_id(uuid) / cwd / created_at / active`。
- **原子落盘**：写临时文件 + `os.replace`（临时文件须与目标**同目录同文件系统**，否则 `os.replace` 非原子，N3）。
- 启动恢复：读清单，对每个会话用 `--resume <session_id>` 重建 driver。
- **session_id 以运行时学到的为准**（见 §3.2 M5/N6）：driver 启动后从 `system`/`result` 事件拿到的 id 回写覆盖落盘值。

### 3.5 HealthModel（`health.py`）
- 聚合：连接状态、各会话进程存活/busy、各会话队列深度、最近 N 条错误、各会话重启计数。
- SP1 只建模型 + 写日志；可视化健康卡留 SP3（签名特色③的数据来源）。

### 3.6 SingleInstanceLock（`lock.py`）
- 启动时 flock `~/.feishu-claude-bridge/bridge.lock`，已锁则拒绝启动并提示。

---

## 4. 内部事件模型（Engine ↔ 渲染的解耦边界）

Driver 把 `claude` 的 stream-json 归一化为稳定的内部事件，Engine 据此驱动渲染。这样 **SP2 换漂亮工单卡时只改 renderer，不动 Engine**。

事件类型（草案，spike 后定稿）：
- `TurnStarted{session, user_text}` — 来源是 `--replay-user-messages` 的回显 user 事件（M1）；driver 据此发出，并**吞掉该回显**不再当模型输出。
- `TextDelta{session, text}` / `TextDone{session, full_text}`
- `Thinking{session, text}`
- `ToolUse{session, name, input_summary}` / `ToolResult{session, name, status, exit_code?}`
- `PermissionRequest{session, request_id, tool, input}`
- `TurnResult{session, usage, cost, duration_ms}` — **仅当 `result.subtype` 为终态时发出**；`subtype:"compact"/"compaction"` 的中途 result 忽略（C3）。
- `TurnCancelled{session}` — 来自 `control_cancel_request`。
- `HealthChanged{snapshot}`
- `SessionRecovered{session}` / `SessionCrashed{session, restarts}`

---

## 5. 稳定性十条 → 落点

| 机制 | 落点 |
|---|---|
| 去重 + **启动水位线** | FeishuGateway：记 `start_ts`，丢弃 `create_time < start_ts − 2s` 的旧消息（**−2s 宽限**，避免误杀刚启动瞬间的消息，N4）+ msg_id 去重。注：现有 `feishu_client.py:40` 只有 msg_id 去重、无时间水位线，这是新代码 |
| **有界队列 + 背压（含 C2 铁律）** | Engine：每会话 `pending` 上限 N，满则回复"队列已满（N 条在排队）"。**关键**：队列不仅防溢出，更承担"回合中途不写 stdin"——消息只在收到 `TurnResult` 后才 drain 写入（见 §3.2 C2） |
| 会话状态**原子落盘** | SessionStore：tmp + `os.replace`（同目录） |
| **优雅关闭三段式** | Driver.close()：关 stdin → **等 ~120s 让 Stop hooks 跑完**（如 claude-mem 写 session 摘要，过短会丢摘要）→ SIGTERM → 等 ~5s → SIGKILL；**对进程组**执行（N2） |
| **重试 + 退避** | 发卡失败 0/0.5/1.5s 重试；token 刷新失败重试 |
| **事件空闲超时** | Engine：回合内**连续** N 秒无**任何**事件（含 partial delta）→ 判回合结束、解锁、提示"似乎卡住"。N 要**足够大**（如默认 5 分钟，可配），且**任何事件都重置**计时——否则一个 3 分钟构建会被误判中途解锁（N1）。与"进程级空闲回收"是两回事，勿混 |
| **慢操作埋点** | 发卡>2s、首事件>15s → WARN |
| per-session 互斥 + busy | Driver `busy` 标志；心跳 try-跳过 |
| **单实例锁** | 启动 flock |
| daemon + 日志轮转 | 沿用 launchd，补 systemd + 按大小轮转 |

---

## 6. 进程监督 / 崩溃自愈

- 子进程意外退出（非用户删除）→ 按退避自动重启 + `--resume <运行时学到的 session_id>` 续接（非仅落盘的初始 id，N6）+ 发"会话已自动恢复"占位状态卡。
- 重启风暴保护：1 分钟内 >3 次重启 → 停手并报"会话反复崩溃，请检查"，置该会话为 `failed`。
- 杀进程要杀**进程组**（§5/N2），避免 MCP 孙进程孤儿。
- 信号喂 HealthModel。

---

## 7. 权限处理（SP1 临时策略）—— ⚠️ 头号风险，spike 必须先验

**核心问题（C1）**：我们整套"把权限请求转发飞书 y/n"的前提，是 Claude 真的会在 stdout 发出 `control_request`。但这**只有在加了 `--permission-prompt-tool` 时**才发生，而最显然的取值 `stdio` 在 stream-json 模式下**已知坏掉**——会**静默执行工具、根本不发请求**（bug #34046，CLI 2.1.73+）。若踩中，桥接会让 Claude **无提示执行破坏性操作**，比现状更不安全。

**SP1 的策略（待 spike 定锤）**：
1. **首要 spike**（排第一，先于一切）：在**钉死的 CLI 版本**上验证——权限请求**到底发不发**、用哪个 flag、`control_request`/`control_response` 的确切字段。
2. **路径选择**：优先用**自定义 MCP 权限工具**（官方文档支持的路径），而非 `stdio`。该 MCP 工具收到请求时回调进 Engine，Engine 把请求转**文本卡**（沿用现有 `y`/`n` 机制）发飞书。
3. **超时兜底**：5 分钟无回应 → **自动 deny** + 提示，杜绝永久阻塞。
4. **fallback 策略**（若该 CLI 版本无可靠提示路径）：用保守 `--allowedTools` 白名单 + `--permission-mode`，**白名单外一律自动 deny**——宁可拦错也不放过破坏性操作。
5. 漂亮授权按钮 + 多会话安全的回调留 SP3（见 §13）。

> **结论**：引擎的权限设计在该 spike 返回前**不能定稿**。

---

## 8. 测试策略（TDD）

### 8.0 spike 是硬前置（gate，先于写任何引擎代码）
在**钉死的 CLI 版本**上，最小脚本必须实测并录成 fixture，逐项确认：
- **[C1]** 加 `--permission-prompt-tool <?>` 后权限请求**到底发不发**、用什么工具、`control_request`/`control_response` 确切字段；`stdio` 是否真的静默执行（#34046）。
- **[C2]** 回合进行中往 stdin 写第二条消息是否真的挂起 → 验证"仅空闲时写"铁律。
- **[C3]** 触发上下文压缩，确认中途 `result` 的 `subtype`，验证不被误判回合结束。
- **[M1]** `--replay-user-messages` 的回显事件形状，确认能作 `TurnStarted` 锚点。
- **[M2]** 确认默认未启用 `--bare`、MCP/Skills 自动加载；钉死 canonical 启动命令。
- **[R3/M5]** `--resume` 在常驻 stream-json 下的上下文连续性 + 运行时 session_id 是否与落盘 id 一致。

**spike 结论回填 §3/§4/§7 后，SP1 才可进入 writing-plans。**

### 8.1 其余测试
1. 录制的真实 fixture 喂 `FakeClaudeProcess`。
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
- ✅ 权限请求超时自动 deny，不死锁；且 spike 已确认权限请求**确实会发出**（否则走 §7 fallback 白名单）。
- ✅ 回合中途连发两条消息：第二条在首个 `TurnResult` 前**不写 stdin**、不挂起（C2）。
- ✅ 长回合触发压缩：中途 `result` 不被误判为回合结束（C3）。
- ✅ 全部单元 + 集成测试绿（CI 不依赖真 claude）。

---

## 11. 风险

- **R1（高·安全）**：权限请求是否发出、`--permission-prompt-tool` 取值（`stdio` 已知坏）、`control_*` 字段 → §7 头号 spike。踩错 = 无提示执行破坏性操作。
- **R1b（高）**：回合中途写 stdin 会挂起（C2）；`result` 被压缩态误判（C3）→ spike 验证 + 写成测试。
- **R2（中）**：飞书 WS 线程与 asyncio 跨界投递（阻塞发卡入 loop、loop 生命周期、入站无界队列）→ §2 三条硬规则在**设计层面**钉死，非仅靠测试。
- **R3（中）**：`--resume` 在常驻 stream-json 下的上下文连续性 + session_id 权威 → spike + e2e 验证。
- **R4（中）**：多会话 N 个常驻 `claude`（各自拖一串 MCP 子进程，资源不轻）→ 设**现实的 `MAX_SESSIONS` 上限**（建议 ≤3，而非沿用 5）+ 单进程 RAM 预算，HealthModel 监控。

---

## 12. 后续

1. **先做 §8.0 spike**（硬 gate），结论回填 §3/§4/§7。
2. spike 通过后，调用 **writing-plans** 技能产出实现计划（spike → TDD 逐组件）。

## 13. 给 SP3 的交接提醒（M4，非 SP1 阻塞）

- 卡片回调**今天就是通的**：`feishu_client.py:51` 注册了 `CardActionHandler`，`bridge.py:599` 已能从按钮解析 allow/deny。所以 SP3 "先验证打通卡片回调"风险**低于**原估。
- 但现有回调代码**不是多会话安全的**：`bridge.py:606` 有 `# TODO: Map request_id to session_id`，只认单个 active 会话。SP1 多会话落地后，权限事件必须带 `(session, request_id)`（§4 的 `PermissionRequest` 已含——好），SP3 的 Gateway/回调链路要把它**贯穿**，现有代码做不到。
- 据此修正愿景 §R3 的风险措辞：卡片回调"已接线"≠"端到端验证过 + 多会话安全"。
