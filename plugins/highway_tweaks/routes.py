"""Highway Tweaks: append the diagnostics screen.js posts (frame-drop snapshots,
per-song guitar / drum judgment logs) to <config_dir>/highway_tweaks/jank_log.jsonl.

The log lives in the config dir (the plugin folder is read-only in a packaged
app) and is listed in plugin.json's diagnostics.server_files, so Settings →
Export Diagnostics includes it. It rolls over to jank_log.old.jsonl at 5 MB.
"""

import json
import threading
import time
from pathlib import Path

from fastapi import Request

MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def log_path(config_dir) -> Path:
    return Path(config_dir) / "highway_tweaks" / "jank_log.jsonl"


def append(path: Path, entry: dict) -> None:
    entry = dict(entry)
    entry["server_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_name("jank_log.old.jsonl"))
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def tail(path: Path, n: int) -> list:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines()[-max(1, min(500, n)):]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def setup(app, context):
    path = log_path(context["config_dir"])

    @app.post("/api/plugins/highway_tweaks/log")
    async def log_event(request: Request):
        try:
            entry = await request.json()
        except Exception:
            return {"ok": False}
        if not isinstance(entry, dict):
            return {"ok": False}
        append(path, entry)
        return {"ok": True}

    @app.get("/api/plugins/highway_tweaks/log")
    def read_log(n: int = 20, kind: str = ""):
        rows = tail(path, n if not kind else 500)
        if kind:
            rows = [r for r in rows if r.get("kind") == kind][-max(1, min(500, n)):]
        return {"entries": rows}
