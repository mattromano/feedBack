"""Per-part scored stats (drums / 5-fret / vocals via RockHeroSmith `play:result`)
plus run history: POST /api/stats with `part`, GET /api/stats/history.

Real guitar (no `part`) must keep writing song_stats exactly as before.
"""

import importlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def server(tmp_path, monkeypatch, isolate_logging):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("FEEDBACK_SKIP_STARTUP_TASKS", "1")
    sys.modules.pop("server", None)
    srv = importlib.import_module("server")
    try:
        yield srv
    finally:
        conn = getattr(getattr(srv, "meta_db", None), "conn", None)
        if conn is not None:
            getattr(sys.modules.get("server"), "_join_background_db_threads", lambda: None)()
            conn.close()
        sys.modules.pop("server", None)


@pytest.fixture()
def client(server):
    return TestClient(server.app)


def _drums(**over):
    body = {"filename": "song.feedpak", "part": "drums", "difficulty": "expert",
            "arrangement": None, "score": 120000, "accuracy": 0.9,
            "hits": 90, "misses": 10, "bestStreak": 40,
            "session_id": "s1", "player": "drums", "input": "kit"}
    body.update(over)
    return body


def test_part_session_records_part_best_not_song_stats(client, server):
    r = client.post("/api/stats", json=_drums())
    assert r.status_code == 200, r.text
    data = r.json()
    row = data["stats"]
    assert row["part"] == "drums" and row["difficulty"] == "expert"
    assert row["plays"] == 1 and row["best_score"] == 120000
    assert row["best_accuracy"] == pytest.approx(0.9)
    assert row["best_streak"] == 40
    assert data["new_best"] is True
    assert data["run"]["score"] == 120000 and data["run"]["part"] == "drums"
    # Real-guitar song_stats untouched: no guitar best, no badge in the library map.
    assert server.meta_db.get_song_stats("song.feedpak")["arrangements"] == []
    assert client.get("/api/stats/best").json() == {}


def test_part_best_keeps_max_and_counts_plays(client):
    client.post("/api/stats", json=_drums(session_id="a"))
    r = client.post("/api/stats", json=_drums(session_id="b", score=50000, accuracy=0.5,
                                               hits=50, misses=50, bestStreak=12))
    row = r.json()["stats"]
    assert row["plays"] == 2
    assert row["best_score"] == 120000 and row["best_accuracy"] == pytest.approx(0.9)
    assert row["best_streak"] == 40
    assert row["last_score"] == 50000 and row["last_accuracy"] == pytest.approx(0.5)
    assert r.json()["new_best"] is False


def test_difficulties_and_parts_are_separate_buckets(client):
    client.post("/api/stats", json=_drums(session_id="a"))
    client.post("/api/stats", json=_drums(session_id="b", difficulty="easy", score=9))
    client.post("/api/stats", json=_drums(session_id="c", part="five_fret:bass",
                                           player="ctl1", score=7))
    best = client.get("/api/stats/history",
                      params={"filename": "song.feedpak", "part": "drums",
                              "difficulty": "easy"}).json()["best"]
    assert best["best_score"] == 9
    fb = client.get("/api/stats/history",
                    params={"filename": "song.feedpak", "part": "five_fret:bass",
                            "difficulty": "expert"}).json()["best"]
    assert fb["best_score"] == 7


def test_same_session_and_player_is_idempotent(client, server):
    client.post("/api/stats", json=_drums())
    xp1 = server.meta_db.get_xp()
    r = client.post("/api/stats", json=_drums())
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    assert r.json()["stats"]["plays"] == 1
    assert server.meta_db.get_xp() == xp1


def test_part_session_awards_xp_on_the_shared_score_scale(client, server):
    from song_score import score as parity_score
    from xp import xp_for_run
    r = client.post("/api/stats", json=_drums())
    # XP from hits/misses on the guitar recorder's scale, not RHS points, so
    # a 120k-point drum chart doesn't out-earn a guitar run of equal accuracy.
    assert r.json()["progress"]["xp"] == xp_for_run(parity_score(90, 10))
    assert r.json()["progress"]["current_streak"] == 1


def test_part_session_fires_song_completed_with_part_instrument(client, server, monkeypatch):
    seen = []
    real = server.meta_db.record_progression_event

    def spy(etype, payload, content):
        seen.append((etype, dict(payload)))
        return real(etype, payload, content)

    monkeypatch.setattr(server.meta_db, "record_progression_event", spy)
    client.post("/api/stats", json=_drums())
    client.post("/api/stats", json=_drums(session_id="x", part="five_fret:guitar", player="ctl1"))
    assert [p["instrument"] for _, p in seen] == ["drums", "five_fret"]
    assert all(e == "song_completed" for e, _ in seen)
    assert seen[0][1]["accuracy"] == pytest.approx(0.9)


def test_part_full_combo_needs_perfect_accuracy(client):
    r = client.post("/api/stats", json=_drums(full_combo=True))   # 0.9 accuracy
    assert r.json()["stats"]["full_combos"] == 0
    r = client.post("/api/stats", json=_drums(session_id="fc", full_combo=True,
                                               accuracy=1.0, hits=100, misses=0))
    assert r.json()["stats"]["full_combos"] == 1
    assert r.json()["run"]["full_combo"] is True


def test_part_validation(client):
    assert client.post("/api/stats", json=_drums(part="Drums; DROP")).status_code == 400
    assert client.post("/api/stats", json=_drums(difficulty="insane")).status_code == 400
    assert client.post("/api/stats", json=_drums(part=7)).status_code == 400
    # A part needs a scored session — no position-only part touches.
    body = {"filename": "song.feedpak", "part": "drums", "lastPlayPosition": 3.0}
    assert client.post("/api/stats", json=body).status_code == 400
    assert client.post("/api/stats", json=_drums(hits=-1)).status_code == 400


def test_history_lists_newest_first_with_limit(client):
    for i in range(5):
        client.post("/api/stats", json=_drums(session_id=f"s{i}", score=1000 + i))
    h = client.get("/api/stats/history",
                   params={"filename": "song.feedpak", "part": "drums",
                           "difficulty": "expert", "limit": 3}).json()
    assert [r["score"] for r in h["runs"]] == [1004, 1003, 1002]
    assert h["best"]["plays"] == 5
    assert h["filename"] == "song.feedpak" and h["part"] == "drums"


def test_history_any_difficulty_when_omitted(client):
    client.post("/api/stats", json=_drums(session_id="a", difficulty="easy", score=1))
    client.post("/api/stats", json=_drums(session_id="b", difficulty="hard", score=2))
    h = client.get("/api/stats/history",
                   params={"filename": "song.feedpak", "part": "drums"}).json()
    assert [r["score"] for r in h["runs"]] == [2, 1]
    assert sorted(b["difficulty"] for b in h["bests"]) == ["easy", "hard"]
    assert h["best"] is None   # no single bucket without a difficulty


def test_history_is_bounded_per_bucket(client, server):
    server.meta_db.PLAY_RUNS_KEEP = 4
    for i in range(7):
        client.post("/api/stats", json=_drums(session_id=f"s{i}", score=i))
    n = server.meta_db.conn.execute("SELECT COUNT(*) FROM play_runs").fetchone()[0]
    assert n == 4
    # The best survives pruning (it lives in part_stats).
    h = client.get("/api/stats/history", params={"filename": "song.feedpak",
                                                 "part": "drums", "difficulty": "expert"}).json()
    assert h["best"]["best_score"] == 6 and h["best"]["plays"] == 7


def test_real_guitar_runs_also_land_in_history(client):
    client.post("/api/stats", json={"filename": "g.archive", "arrangement": 0,
                                    "score": 400, "accuracy": 0.6,
                                    "hits": 6, "misses": 4})
    h = client.get("/api/stats/history",
                   params={"filename": "g.archive", "arrangement": 0}).json()
    assert h["part"] == "" and len(h["runs"]) == 1
    assert h["runs"][0]["score"] == 400 and h["runs"][0]["arrangement"] == 0
    # Guitar best comes from song_stats for that arrangement.
    assert h["best"]["best_score"] == 400 and h["best"]["plays"] == 1


def test_history_requires_filename_and_valid_part(client):
    assert client.get("/api/stats/history").status_code == 400
    assert client.get("/api/stats/history",
                      params={"filename": "a", "part": "BAD PART"}).status_code == 400


def test_history_route_is_not_shadowed_by_catch_all(client):
    # /api/stats/{filename:path} must not swallow /api/stats/history.
    body = client.get("/api/stats/history", params={"filename": "nothing"}).json()
    assert body["runs"] == [] and "arrangements" not in body


def test_old_db_migrates_and_reopens_idempotently(tmp_path):
    """A db from before the part dimension opens, gains the new tables, keeps
    its song_stats rows, and opening it twice is a no-op."""
    db_path = tmp_path / "web_library.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE song_stats (
        filename TEXT NOT NULL, arrangement INTEGER NOT NULL DEFAULT 0,
        plays INTEGER NOT NULL DEFAULT 0, best_score INTEGER NOT NULL DEFAULT 0,
        best_accuracy REAL NOT NULL DEFAULT 0, last_score INTEGER NOT NULL DEFAULT 0,
        last_accuracy REAL NOT NULL DEFAULT 0, last_position REAL NOT NULL DEFAULT 0,
        last_played_at TEXT, updated_at TEXT, PRIMARY KEY (filename, arrangement))""")
    conn.execute("INSERT INTO song_stats (filename, arrangement, plays, best_score, best_accuracy) "
                 "VALUES ('old.archive', 0, 3, 500, 0.7)")
    conn.commit()
    conn.close()

    from metadata_db import MetadataDB
    db = MetadataDB(tmp_path)
    try:
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"part_stats", "play_runs"} <= tables
        old = db._stats_row("old.archive", 0)
        assert old["plays"] == 3 and old["best_score"] == 500
        db.record_part_session("old.archive", "drums", "hard", score=10, accuracy=0.5,
                               hits=5, misses=5, best_streak=3)
    finally:
        db.conn.close()
    db2 = MetadataDB(tmp_path)   # second open: nothing raises, data intact
    try:
        assert db2.part_best("old.archive", "drums", "hard")["plays"] == 1
        assert db2._stats_row("old.archive", 0)["plays"] == 3
    finally:
        db2.conn.close()
