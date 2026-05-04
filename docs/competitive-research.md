# Claude Code 远程控制方案调研报告

> 调研时间：2026-05-01
> 目的：了解市面上将 Claude Code 桥接到 IM 平台的方案，明确 feishu-claude-bridge 的差异化定位

## 方案总览

| 项目 | 通信方式 | 是否接管已有 session | MCP/记忆保留 | 权限远程审批 | 飞书支持 | 无人值守 |
|------|----------|---------------------|-------------|-------------|---------|---------|
| **Claude Code Channels（官方）** | MCP 协议注入 | ✅ 当前 session | ✅ 完整保留 | ✅ 有限支持 | ❌ | ❌ 需 session 在前台 |
| **Claude Code Remote Control（官方）** | HTTPS 轮询 | ✅ 当前 session | ✅ 完整保留 | ✅ 双端同步 | ❌（web/app） | ❌ 关终端就断 |
| **cc-connect** | 子进程启动 agent | ➖ 新进程，可持久化 | ➖ 不确定 | ✅ 多模式 | ✅ | ✅ |
| **Claude-to-IM-skill** | Claude Agent SDK | ❌ 新建 session | ❌ 不继承 | ✅ 飞书卡片 | ✅ | ✅ |
| **claude-code-telegram** | Anthropic API + CLI fallback | ❌ 新建 session | ❌ 不继承 | ➖ 白名单控制 | ❌ | ✅ |
| **feishu-claude-bridge（本项目）** | tmux send-keys + JSONL | ✅ 当前 session | ✅ 完整保留 | ✅ 飞书卡片 | ✅ | ✅ |

---

## 1. Claude Code Channels（官方）

- **上线时间**：2026 年 3 月，research preview
- **原理**：Channel 本质是一个本地 MCP server，通过 `notifications/claude/channel` 协议把外部消息注入当前运行的 session。消息以 `<channel source="telegram">` XML 标签出现在 Claude 的上下文里。
- **session 处理**：注入当前 session，不新建。Claude 看到的上下文、MCP servers、项目配置全部保留。
- **权限处理**：Channel 可以声明 `claude/channel/permission` 能力来转发权限弹窗。用户在 IM 里回复 `yes <id>` 批准。但如果 channel 没声明这个能力，权限弹窗会**卡在终端等你回去点**。
- **支持平台**：Telegram、Discord、iMessage（仅 macOS）
- **局限**：
  - **不支持飞书**
  - session 必须保持运行，没有离线队列
  - research preview，接口可能变
  - 需要 `claude.ai` 登录，不支持 API key
  - Team/Enterprise 需要管理员手动开启

## 2. Claude Code Remote Control（官方）

- **原理**：本地 Claude Code 通过 HTTPS 轮询与 Anthropic API 建立双向通道。用户在 claude.ai/code 或 Claude 手机 app 上操作，消息同步到本地 session。
- **session 处理**：继续当前 session，所有设备共享同一个对话线程。终端、浏览器、手机可以同时发消息。
- **权限处理**：权限弹窗**同时出现在终端和远程 UI 上**，先回复的生效。
- **支持平台**：任意浏览器（claude.ai/code）、iOS/Android（Claude app）
- **局限**：
  - **不是 IM 平台**，需要专门打开 claude.ai 或 Claude app
  - 关闭终端 session 就断了
  - 网络断开超 10 分钟会超时
  - 部分命令（`/mcp`、`/plugin`、`/resume`）只能在终端执行

## 3. cc-connect

- **GitHub**: github.com/chenhg5/cc-connect
- **原理**：Go 实现，以**子进程**方式启动 Claude Code 等 agent，通过 Agent Client Protocol (ACP) 双向通信。支持 Unix 用户隔离（`run_as_user`）。
- **session 处理**：启动新的 agent 进程。通过 `/new`、`/switch`、`/list` 命令管理多个 session。支持空闲超时后自动轮转到新 session。session 状态持久化到 JSON 文件。
- **权限处理**：三种模式可切 `/mode` 命令，包括 auto-approve 的 yolo 模式。
- **支持平台**：**11 个**——飞书、钉钉、Slack、Telegram、Discord、企业微信、微博、LINE、QQ、QQ Bot、微信个人版
- **特点**：平台覆盖最广，Go 单二进制部署，Web Admin UI
- **局限**：
  - 子进程模式，**不接管已有 session**
  - MCP server 继承情况不明确
  - 上下文靠 `/memory` 指令文件维护，不是原始对话历史

## 4. Claude-to-IM-skill

- **GitHub**: github.com/op7418/Claude-to-IM-skill
- **原理**：Node.js/TypeScript 实现，通过 **Claude Agent SDK** 的 `sdk.query()` / `sdk.runStreamed()` 与 Claude 通信，返回 SSE 流（text、tool_use、permission_request 等事件）。
- **session 处理**：**新建 session**。每个 IM 聊天映射到一个独立的 Claude session，状态持久化到 `~/.claude-to-im/sessions.json`。
- **权限处理**：SDK 的 `canUseTool()` 阻塞等待（5 分钟超时），bridge 在飞书/QQ/微信上推文本命令让用户回复。Telegram/Discord 有 inline button。
- **支持平台**：Telegram、Discord、飞书、QQ、微信
- **局限**：
  - **新建 session，不继承原有上下文**
  - MCP servers 不继承（SDK 创建的 session 独立于你终端里的那个）
  - 依赖 Claude Agent SDK，需要 API key

## 5. claude-code-telegram

- **GitHub**: github.com/RichardAtCT/claude-code-telegram
- **原理**：Python 实现，主要通过 **Anthropic API** 调用 Claude，备选方案是调本地 `claude` CLI 命令。
- **session 处理**：**新建 session**。SQLite 存储 per-user per-project 的对话历史。"Project Threads Mode" 把 Telegram topic 映射到项目目录。
- **权限处理**：白名单认证 + 目录沙箱 + 速率限制 + 用户级消费上限。
- **支持平台**：仅 Telegram
- **局限**：
  - 走 API，不接管本地 session
  - 不保留原有 MCP servers 和记忆
  - 仅 Telegram

---

## 核心差异分析

### 通信方式分类

市面上的方案按通信方式可以分为四类：

1. **MCP 协议注入**（Channels）：最优雅，直接在协议层注入消息，零侵入。但受限于官方支持的平台。
2. **HTTPS 双向同步**（Remote Control）：通过 Anthropic 服务器中转，不需要任何第三方。但需要开 claude.ai。
3. **SDK 调用**（Claude-to-IM-skill、claude-code-telegram）：通过 Agent SDK 或 API 创建独立 session，灵活但**丢失原有上下文**。
4. **终端模拟**（feishu-claude-bridge）：通过 tmux send-keys 模拟人类输入，通过 JSONL 文件读取输出。"笨"但有效，完全不侵入 Claude Code 本身。

### feishu-claude-bridge 的定位

本项目的独特组合是：**接管已有 session + 飞书支持 + 完整远程审批 + 无人值守自愈**。

- 和官方 Channels 比：Channels 不支持飞书，且 research preview 阶段功能不稳定
- 和官方 Remote Control 比：Remote Control 需要打开 claude.ai，不是 IM 平台集成
- 和 cc-connect 比：cc-connect 启动新进程，不接管已有 session
- 和 Claude-to-IM-skill 比：SDK 新建 session，不继承 MCP servers 和对话历史
- 和 claude-code-telegram 比：API 调用，不保留任何本地上下文

### 技术方案的 tradeoff

| 维度 | tmux + JSONL（本项目） | SDK/API | MCP 注入（Channels） |
|------|----------------------|---------|---------------------|
| session 继承 | ✅ 完整 | ❌ 新建 | ✅ 完整 |
| 实现复杂度 | 低（shell 命令） | 中（SDK 集成） | 高（MCP 协议） |
| 稳定性 | 依赖 JSONL 文件格式 | SDK 保证 | 官方协议保证 |
| 平台扩展性 | 需自己实现 | SDK 抽象好 | 受限官方支持 |
| 侵入性 | 零（不改 Claude Code） | 需要 API key | 需改启动命令 |
