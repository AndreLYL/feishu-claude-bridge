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
