import pytest
from tmux_controller import parse_selection_menu

SAMPLE_MENU = """
> 6. Chat about this

  Enter to select · ↑/↓ to navigate · ctrl+g to edit in Vim · Esc to cancel
"""

SAMPLE_MENU_WITH_OPTIONS = """
你说的「Claude Code 多窗口控制」具体是指哪种场景？

  1. 一个 orchestrator 同时控制多个 Claude Code 实例
     类似 Agent Teams 一个主控进程同时启动和管理多个 Claude Code session
  2. 手动开多个终端窗口各跑一个 Claude Code, 需要统一管理
     比如你开了 3 个 tmux pane 各跑一个 claude, 想统一查看状态
  3. 扩展 Feishu Bridge 支持多 session
> 4. 用一个 Claude Code session 控制其他终端窗口
  5. Type something.

> 6. Chat about this

  Enter to select · ↑/↓ to navigate · ctrl+g to edit in Vim · Esc to cancel
"""

NO_MENU = """
好，我看到你之前做过 Feishu-Claude Bridge，用 tmux 桥接实现手机远程控制单个 Claude
Code session。现在你说的「多窗口控制」，我需要先搞清楚你具体想解决什么问题。
"""


def test_detect_menu_present():
    result = parse_selection_menu(SAMPLE_MENU)
    assert result is not None
    assert result["is_menu"] is True


def test_detect_menu_absent():
    result = parse_selection_menu(NO_MENU)
    assert result is None


def test_parse_options():
    result = parse_selection_menu(SAMPLE_MENU_WITH_OPTIONS)
    assert result is not None
    assert len(result["options"]) >= 4


def test_parse_selected_index():
    result = parse_selection_menu(SAMPLE_MENU_WITH_OPTIONS)
    assert result is not None
    assert result["selected_index"] >= 0


EDIT_PERMISSION_MENU = """
⏺ Update(~/.claude.json)

────────────────────────────────────────────────────────────────────────────────
 Edit file
 ../.claude.json
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 942        "env": {}
 943      },
 944      "tavily": {
 945 -      "type": "sse",
 945 +      "type": "streamable-http",
 946        "url": "https://mcp.tavily.com/mcp/"
 947      },
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to .claude.json?
 ❯ 1. Yes
   2. Yes, allow all edits in yinglong.li/ during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


def test_detect_edit_permission_menu():
    """Edit permission menus use 'Esc to cancel' instead of 'Enter to select'."""
    result = parse_selection_menu(EDIT_PERMISSION_MENU)
    assert result is not None
    assert result["is_menu"] is True
    assert len(result["options"]) == 3
    assert result["options"][0] == "Yes"
    assert result["selected_index"] == 0


BASH_PERMISSION_MENU = """
⏺ Bash(git push origin main)

 Run command?
 ❯ 1. Yes
   2. Yes, allow all Bash commands during this session
   3. No

 Esc to cancel
"""


def test_detect_bash_permission_menu():
    """Bash permission menus should also be detected."""
    result = parse_selection_menu(BASH_PERMISSION_MENU)
    assert result is not None
    assert result["is_menu"] is True
    assert len(result["options"]) == 3
    assert result["options"][0] == "Yes"


FALSE_POSITIVE_TEXT = """
关于菜单检测的修复说明：

之前的问题是 parse_selection_menu 会在整个 pane 内容中搜索 "Esc to cancel" 和
"Enter to select" 标记。修复方案：

1. 只检查 pane 最后 20 行
2. 避免 assistant 文本中的 "Esc to cancel" 触发误判
3. 菜单选项也只从最后 20 行提取
4. 所有现有测试用例仍然通过
"""


def test_no_false_positive_from_assistant_text():
    """Assistant text mentioning menu markers should NOT trigger menu detection."""
    result = parse_selection_menu(FALSE_POSITIVE_TEXT)
    assert result is None
