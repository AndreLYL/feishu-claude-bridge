class HealthModel:
    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def update_session(self, name, **fields):
        s = self._sessions.setdefault(
            name, {"alive": False, "busy": False, "queue_depth": 0,
                   "restarts": 0, "last_error": None})
        s.update(fields)

    def record_error(self, name, msg):
        self.update_session(name, last_error=msg)

    def remove_session(self, name):
        self._sessions.pop(name, None)

    def snapshot(self) -> dict:
        return {"sessions": {k: dict(v) for k, v in self._sessions.items()}}
