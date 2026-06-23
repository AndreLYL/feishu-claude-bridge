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


def test_path_with_tilde_is_expanded(monkeypatch, tmp_path):
    # ~ must expand to $HOME, not a literal "./~/" dir in cwd.
    monkeypatch.setenv("HOME", str(tmp_path))
    lock = SingleInstanceLock("~/.feishu-claude-bridge/bridge.lock")
    assert str(lock.path).startswith(str(tmp_path))
    assert "~" not in str(lock.path)
