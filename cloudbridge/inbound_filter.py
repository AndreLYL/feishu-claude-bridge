from collections import OrderedDict

class InboundFilter:
    def __init__(self, start_ts: float, grace_s: float = 2.0, max_ids: int = 200):
        self._watermark = start_ts - grace_s   # 秒
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max = max_ids

    def accept(self, msg_id: str, create_time_ms: int) -> bool:
        if create_time_ms / 1000.0 < self._watermark:
            return False                        # 启动前的旧消息
        if msg_id in self._seen:
            return False                        # 重复（WS 重连重发）
        self._seen[msg_id] = None
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True
