import fcntl
from pathlib import Path


class SingleInstanceLock:
    def __init__(self, path):
        # expanduser so "~/.feishu-claude-bridge/bridge.lock" lands in $HOME,
        # not a literal "./~/" directory in the cwd.
        self.path = Path(path).expanduser()
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another bridge instance is already running")
        return self

    def __exit__(self, *exc):
        self.release()
