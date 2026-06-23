import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class SessionRecord:
    name: str
    session_id: str
    cwd: str
    created_at: float
    active: bool


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return {k: SessionRecord(**v) for k, v in data.items()}

    def save(self, records: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in records.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")  # 同目录同文件系统
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)  # 原子替换
