from cloudbridge.inbound_filter import InboundFilter

def test_drops_messages_before_watermark_with_grace():
    f = InboundFilter(start_ts=1000.0, grace_s=2.0)
    # 早于 (1000-2)=998 秒 → 丢弃；create_time 是毫秒
    assert f.accept("m1", create_time_ms=997_000) is False
    # 在宽限窗内 → 接受
    assert f.accept("m2", create_time_ms=999_000) is True

def test_dedup_repeated_msg_id():
    f = InboundFilter(start_ts=0.0)
    assert f.accept("dup", create_time_ms=10_000) is True
    assert f.accept("dup", create_time_ms=10_000) is False
