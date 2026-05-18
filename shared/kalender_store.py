import fcntl
import json
import threading
from pathlib import Path
from typing import Callable

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
_lock = threading.Lock()
_cache_data: dict | None = None
_cache_mtime: float = 0.0

# Aufräumen falls voriger Lauf zwischen write_text und replace abgestürzt ist
_tmp = VEREINSTERMINE_FILE.with_suffix(".json.tmp")
if _tmp.exists():
    _tmp.unlink()


class KalenderStore:
    @staticmethod
    def read() -> dict:
        global _cache_data, _cache_mtime
        try:
            mtime = VEREINSTERMINE_FILE.stat().st_mtime
        except OSError:
            return {}
        if _cache_data is not None and mtime == _cache_mtime:
            return _cache_data
        data = json.loads(VEREINSTERMINE_FILE.read_text())
        _cache_data = data
        _cache_mtime = mtime
        return data

    @staticmethod
    def update(mutator: Callable[[dict], None]) -> dict:
        global _cache_data, _cache_mtime
        with _lock:
            with open(VEREINSTERMINE_FILE, "r+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    data = json.load(fh)
                    mutator(data)
                    tmp = VEREINSTERMINE_FILE.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    tmp.replace(VEREINSTERMINE_FILE)
                    _cache_data = None
                    _cache_mtime = 0.0
                    return data
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
