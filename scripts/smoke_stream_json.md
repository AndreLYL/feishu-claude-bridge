# SP1 e2e 冒烟清单（手动，需真 claude + 真飞书测试群）

> 这些场景无法在 CI 离线验证（CI 用 `scripts/fakeclaude.py` 桩）。
> 在配好 `.env` 的真实环境里手动逐条打勾。CLI 基线：claude 2.1.186。

启动新核心：

```bash
python bridge.py --core stream-json
```

（旧 tmux 路径不受影响：`python bridge.py --core tmux --tmux-session <name> ...` 仍照常工作。）

## 逐条验证

- [ ] **1. 基本回合**：飞书群发"列出当前目录文件" → 占位卡流式更新出回复，回合结束有 `TurnResult`（含 `total_cost_usd`）。
- [ ] **2. 富文本/工具**：发"读取 README.md 的前几行" → 工具调用与文本回复正常呈现。
- [ ] **3. 权限 allow**：发一个需要权限的操作（如写文件到工作目录外） → 收到 `control_request` 转成的文字提示；回 `y` → 写 `control_response{decision:"allow"}`，操作执行、回合完成。
- [ ] **4. 权限 deny**：再触发一次权限 → 回 `n` → 操作被拒、回合继续/结束，进程不卡死。
- [ ] **5. 权限超时**：触发权限后 5 分钟不回应 → 自动 `deny` + 提示，进程不永久阻塞。
- [ ] **6. C2 背压（回合中途连发）**：在一个长回合进行中再连发两条 → 第二、三条排队，第一条 `TurnResult` 后才依次处理，进程不挂起、不交叉。
- [ ] **7. 队列打满**：快速连发超过 `queue_max` 条 → 收到"队列已满"背压提示，不崩、不静默丢。
- [ ] **8. 崩溃自愈**：`kill` 掉 `claude` 子进程 → 自动 `--resume <学到的 session_id>` 恢复 + "会话已自动恢复"提示卡。
- [ ] **9. 重启不重放**：`kill` 掉 bridge 再 `--core stream-json` 重启 → 不重放启动前的旧消息（水位线 −2s 生效）。
- [ ] **10. 多会话**：`/new work2`、`/switch work2`、`/list`、`/delete` → 各会话独立、串行不交叉；超过 `MAX_SESSIONS`（默认 ≤3）时 `/new` 回"已达上限"。
- [ ] **11. 优雅关闭**：`Ctrl-C` / SIGTERM → stdin 关闭后等 Stop hooks（~120s 上限）跑完，再 SIGTERM/SIGKILL 进程组；无 MCP 孙进程残留（`pgrep -f claude` 确认）。
- [ ] **12. 单实例锁**：在已运行时再启动一个 `--core stream-json` → 第二个被 flock 拒绝并提示。

## 记录

每条勾选后，把异常现象（若有）记到本文件下方，作为 SP2 接手前的已知问题清单。
