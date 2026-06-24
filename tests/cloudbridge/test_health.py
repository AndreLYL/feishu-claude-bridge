from cloudbridge.health import HealthModel


def test_update_and_snapshot():
    h = HealthModel()
    h.update_session("main", alive=True, busy=False, queue_depth=0, restarts=0)
    snap = h.snapshot()
    assert snap["sessions"]["main"]["alive"] is True


def test_record_error_keeps_last():
    h = HealthModel()
    h.update_session("main", alive=True)
    h.record_error("main", "boom")
    assert h.snapshot()["sessions"]["main"]["last_error"] == "boom"


def test_remove_session():
    h = HealthModel()
    h.update_session("main", alive=True)
    h.remove_session("main")
    assert "main" not in h.snapshot()["sessions"]
