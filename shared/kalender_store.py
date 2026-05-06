import fcntl
import json
import threading
from pathlib import Path
from typing import Callable

VEREINSTERMINE_FILE = Path("/opt/rename-webhook/vereinstermine.json")
_lock = threading.Lock()

# Aufräumen falls voriger Lauf zwischen write_text und replace abgestürzt ist
_tmp = VEREINSTERMINE_FILE.with_suffix(".json.tmp")
if _tmp.exists():
    _tmp.unlink()


class KalenderStore:
    @staticmethod
    def read() -> dict:
        return json.loads(VEREINSTERMINE_FILE.read_text())

    @staticmethod
    def update(mutator: Callable[[dict], None]) -> dict:
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
                    return data
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
