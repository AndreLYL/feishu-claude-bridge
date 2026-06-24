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
