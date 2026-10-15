"""Rocksmith 2014 play counts -> feedBack's native play count (mattromano custom build).

0.2.9's play_counts plugin showed "Rocksmith plays + Slopsmith plays" per song. 0.3 counts plays
natively (song_stats.play_count, feedBack#49: Most played sort, badges, table view), so this
plugin only adds what core can't know: how often each song was played in Rocksmith 2014.

Import:
  1. read Stats.Songs from the Rocksmith profile (``*_prfldb`` in Steam userdata; the newest one,
     or the path set in the plugin settings) -> PlayedCount per arrangement persistent ID;
  2. read the arrangement IDs from the Rocksmith PSARCs (songs.psarc + dlc/; cached by
     size/mtime) -> which song (artist + title) and part (Lead/Rhythm/Bass/Combo) each ID is;
  3. match songs to the library by normalised artist + title (one chart per song: the one with
     the most arrangements) and parts to the chart's arrangement by name;
  4. add the DIFFERENCE since the last import to song_stats.play_count, so importing again (or
     on every start, see auto_import) never counts a Rocksmith play twice. The ledger of what
     was added lives in rs_profile_counts.db in the config dir.

Profile and PSARCs are only read. Settings: <config>/rs_profile_counts.json
  {"profile": "" (auto), "psarc_dirs": [] (auto: Steam's Rocksmith2014 folder), "auto_import": true}
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import rspsarc

DEFAULTS = {"profile": "", "psarc_dirs": [], "auto_import": True}
_KINDS = ("bass", "rhythm", "lead", "combo")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def group_key(artist, title) -> str | None:
    t = norm(re.sub(r"[\(\[][^)\]]*(?:songsterr|remaster|version|\bv\d)[^)\]]*[\)\]]", " ", str(title or ""), flags=re.I))
    return (norm(artist) + "|" + t) if t else None


def arr_kind(name) -> str:
    n = str(name or "").lower()
    for k in _KINDS:
        if k in n:
            return k
    return ""


def pick_arrangement(kind: str, arrangements: list) -> int:
    """Index of the chart arrangement for a Rocksmith part kind (0 when nothing matches)."""
    best = None
    for a in arrangements or []:
        if not isinstance(a, dict):
            continue
        if arr_kind(a.get("name")) == kind:
            idx = int(a.get("index", 0) or 0)
            best = idx if best is None else min(best, idx)
    return best if best is not None else 0


def plan(stats: dict, psarc_arrs: dict, library: list) -> dict:
    """{(filename, arrangement index): Rocksmith plays} for the library.

    stats: {PERSISTENT ID: profile stats}; psarc_arrs: {psarc path: [rspsarc.psarc_arrangements
    rows]}; library: [(filename, artist, title, arrangements JSON list)]."""
    by_group: dict[str, dict[str, str]] = {}            # group -> {pid: part kind}
    for arrs in psarc_arrs.values():
        for a in arrs:
            g = group_key(a.get("artist"), a.get("song"))
            if g:
                by_group.setdefault(g, {})[a["persistent_id"]] = arr_kind(a.get("name"))
    charts: dict[str, tuple] = {}                        # group -> chosen (filename, arrangements)
    for fn, artist, title, arrs in library:
        g = group_key(artist, title)
        if not g or g not in by_group:
            continue
        cur = charts.get(g)
        cand = (fn, arrs or [])
        if cur is None or (len(cand[1]), -len(fn), fn) > (len(cur[1]), -len(cur[0]), cur[0]):
            charts[g] = cand
    out: dict[tuple, int] = {}
    for g, (fn, arrs) in charts.items():
        for pid, kind in by_group[g].items():
            try:
                n = int(float((stats.get(pid) or {}).get("PlayedCount") or 0))
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                key = (fn, pick_arrangement(kind, arrs))
                out[key] = out.get(key, 0) + n
    return out


class Importer:
    def __init__(self, config_dir: Path, meta_db, log=None):
        self.config_dir = Path(config_dir)
        self.meta_db = meta_db
        self.log = log
        self.lock = threading.Lock()
        self.status = {"running": False, "last": None, "error": None}

    # settings ---------------------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return self.config_dir / "rs_profile_counts.json"

    def settings(self) -> dict:
        try:
            s = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        out = dict(DEFAULTS)
        if isinstance(s, dict):
            out.update({k: s[k] for k in DEFAULTS if k in s})
        if not isinstance(out["psarc_dirs"], list):
            out["psarc_dirs"] = []
        return out

    def save_settings(self, patch: dict) -> dict:
        s = self.settings()
        if "profile" in patch:
            s["profile"] = str(patch.get("profile") or "").strip()
        if "psarc_dirs" in patch:
            dirs = patch.get("psarc_dirs") or []
            if isinstance(dirs, str):
                dirs = dirs.splitlines()
            s["psarc_dirs"] = [str(d).strip() for d in dirs if str(d).strip()]
        if "auto_import" in patch:
            s["auto_import"] = bool(patch.get("auto_import"))
        self.settings_path.write_text(json.dumps(s, indent=1), encoding="utf-8")
        return s

    def profile(self) -> Path | None:
        p = self.settings()["profile"]
        if p:
            return Path(p).expanduser()
        found = rspsarc.find_profiles()
        return found[0] if found else None

    def psarc_dirs(self) -> list[Path]:
        dirs = [Path(d).expanduser() for d in self.settings()["psarc_dirs"]]
        return dirs or rspsarc.rocksmith_dirs()

    # db --------------------------------------------------------------------
    def db(self):
        conn = sqlite3.connect(str(self.config_dir / "rs_profile_counts.db"), check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS psarc_arrs (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, arrs TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS applied (filename TEXT NOT NULL, arrangement INTEGER NOT NULL,"
                     " plays INTEGER NOT NULL, PRIMARY KEY (filename, arrangement))")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        return conn

    def _psarc_arrs(self, conn) -> dict:
        cached = {r[0]: r[1:] for r in conn.execute("SELECT path, mtime, size, arrs FROM psarc_arrs")}
        out, fresh = {}, []
        for d in self.psarc_dirs():
            if not d.is_dir():
                continue
            for p in d.rglob("*.psarc"):
                key = str(p)
                try:
                    st = p.stat()
                except OSError:
                    continue
                c = cached.get(key)
                if c and c[0] == st.st_mtime and c[1] == st.st_size:
                    out[key] = json.loads(c[2])
                    continue
                try:
                    arrs = rspsarc.psarc_arrangements(p)
                except Exception as e:      # unreadable PSARC: no plays attributed
                    if self.log:
                        self.log.warning("rs_profile_counts: %s: %s", p.name, e)
                    arrs = []
                out[key] = arrs
                fresh.append((key, st.st_mtime, st.st_size, json.dumps(arrs)))
        if fresh:
            conn.executemany("INSERT OR REPLACE INTO psarc_arrs VALUES (?, ?, ?, ?)", fresh)
            conn.commit()
        return out

    def _library(self):
        m = self.meta_db
        with m._lock:
            rows = m.conn.execute("SELECT filename, artist, title, arrangements FROM songs").fetchall()
        out = []
        for fn, artist, title, arrs in rows:
            try:
                a = json.loads(arrs or "[]")
            except ValueError:
                a = []
            out.append((fn, artist, title, a if isinstance(a, list) else []))
        return out

    def _apply(self, conn, target: dict) -> dict:
        """Move song_stats.play_count by (target - already applied) per chart arrangement."""
        applied = {(f, a): n for f, a, n in conn.execute("SELECT filename, arrangement, plays FROM applied")}
        deltas = {}
        for key in set(applied) | set(target):
            d = target.get(key, 0) - applied.get(key, 0)
            if d:
                deltas[key] = d
        m = self.meta_db
        with m._lock:
            for (fn, arr), d in deltas.items():
                if d < 0:       # Rocksmith count went down (other profile): never below 0, no new rows
                    m.conn.execute("UPDATE song_stats SET play_count = MAX(0, play_count + ?) "
                                   "WHERE filename = ? AND arrangement = ?", (d, fn, int(arr)))
                    continue
                m.conn.execute(
                    """INSERT INTO song_stats (filename, arrangement, play_count, updated_at)
                       VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))
                       ON CONFLICT(filename, arrangement) DO UPDATE SET
                           play_count = MAX(0, song_stats.play_count + ?),
                           updated_at = excluded.updated_at""",
                    (fn, int(arr), max(0, d), d))
            m.conn.commit()
        conn.execute("DELETE FROM applied")
        conn.executemany("INSERT INTO applied VALUES (?, ?, ?)", [(f, a, n) for (f, a), n in target.items()])
        conn.commit()
        return {"charts": len({f for f, _ in target}), "added": sum(d for d in deltas.values() if d > 0),
                "removed": -sum(d for d in deltas.values() if d < 0), "changed": len(deltas)}

    def run(self, only_if_changed=False) -> dict:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "error": "an import is already running"}
        self.status["running"] = True
        try:
            prof = self.profile()
            if prof is None or not prof.is_file():
                raise FileNotFoundError("no Rocksmith 2014 profile found; set its path in the plugin settings"
                                        if prof is None else f"profile not found: {prof}")
            conn = self.db()
            try:
                stamp = f"{prof}|{prof.stat().st_mtime}"
                last = conn.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()
                lib_n = len(self._library())
                if only_if_changed and last and last[0] == f"{stamp}|{lib_n}":
                    return {"ok": True, "skipped": "profile and library unchanged since the last import"}
                t0 = time.time()
                stats = rspsarc.profile_song_stats(prof)
                psarcs = self._psarc_arrs(conn)
                target = plan(stats, psarcs, self._library())
                res = self._apply(conn, target)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (f"{stamp}|{lib_n}",))
                conn.commit()
            finally:
                conn.close()
            res.update(ok=True, profile=str(prof), psarcs=len(psarcs), profile_songs=len(stats),
                       rocksmith_plays=sum(target.values()), secs=round(time.time() - t0, 1),
                       at=time.strftime("%Y-%m-%d %H:%M:%S"))
            self.status.update(last=res, error=None)
            if self.log:
                self.log.info("rs_profile_counts: %s Rocksmith plays on %s charts (%+d this import)",
                              res["rocksmith_plays"], res["charts"], res["added"] - res["removed"])
            return res
        except Exception as e:
            self.status["error"] = f"{type(e).__name__}: {e}"
            return {"ok": False, "error": self.status["error"]}
        finally:
            self.status["running"] = False
            self.lock.release()


def setup(app, context):
    from fastapi import Body

    log = context.get("log")
    imp = Importer(Path(context["config_dir"]), context["meta_db"], log)

    def _auto():
        time.sleep(20)          # let the library scan settle first
        if imp.settings().get("auto_import"):
            imp.run(only_if_changed=True)

    threading.Thread(target=_auto, name="rs_profile_counts_auto", daemon=True).start()

    @app.get("/api/plugins/rs_profile_counts/status")
    def status():
        prof = None
        try:
            prof = imp.profile()
        except Exception:
            pass
        return {**imp.status, "settings": imp.settings(),
                "profile": str(prof) if prof else None,
                "profiles_found": [str(p) for p in rspsarc.find_profiles()],
                "psarc_dirs": [str(d) for d in imp.psarc_dirs()]}

    @app.post("/api/plugins/rs_profile_counts/settings")
    def save(payload: dict = Body(...)):
        return imp.save_settings(payload or {})

    @app.post("/api/plugins/rs_profile_counts/import")
    def run_import():
        return imp.run()
