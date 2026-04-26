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
