"""Native play count + full combos (feedBack#49): the `count_play` /
`full_combo` flags on POST /api/stats, the /api/stats/plays badge map, the
"Most played" library sort, and the one-time back-fill from scored plays."""

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
            getattr(__import__("sys").modules.get("server"), "_join_background_db_threads", lambda: None)()
            conn.close()
        sys.modules.pop("server", None)


@pytest.fixture()
def client(server):
    return TestClient(server.app)


def _put(server, fn, title, artist, arrangements=1):
    server.meta_db.put(fn, 0, 0, {
        "title": title, "artist": artist,
        "arrangements": [{"name": "Lead", "index": i} for i in range(arrangements)],
    })


def _stats(client, fn):
    return client.get("/api/stats/" + fn).json()


# ── counting ──────────────────────────────────────────────────────────────────

def test_count_play_on_unscored_flush_counts_without_a_scored_play(client):
    # The common no-notedetect case: a seconds-only flush at song end.
    r = client.post("/api/stats", json={"filename": "a.archive", "seconds": 45, "count_play": True})
    assert r.status_code == 200
    row = r.json()["stats"]
    assert row["play_count"] == 1 and row["plays"] == 0   # a play, not a scored run
    # A flush without the flag (the rest of the same session) doesn't count.
    client.post("/api/stats", json={"filename": "a.archive", "lastPlayPosition": 50.0, "seconds": 5})
    assert _stats(client, "a.archive")["play_count"] == 1
    # The next load that plays long enough counts again — on a position touch.
    r = client.post("/api/stats", json={"filename": "a.archive", "lastPlayPosition": 80.0,
                                        "seconds": 60, "count_play": True})
    assert r.json()["stats"]["play_count"] == 2
    assert _stats(client, "a.archive")["play_count"] == 2


def test_count_play_on_scored_session_counts_both(client):
    r = client.post("/api/stats", json={"filename": "s.archive", "score": 500, "accuracy": 0.8,
                                        "seconds": 200, "count_play": True})
    row = r.json()["stats"]
    assert row["plays"] == 1 and row["play_count"] == 1


@pytest.mark.parametrize("flag", [1, "true", "yes", {}, [True]])
def test_count_play_requires_a_real_boolean(client, flag):
    r = client.post("/api/stats", json={"filename": "b.archive", "seconds": 40, "count_play": flag})
    assert r.status_code == 200
    assert r.json()["stats"]["play_count"] == 0


def test_play_count_is_summed_across_arrangements(client, server):
    _put(server, "m.archive", "M", "X", arrangements=2)
    client.post("/api/stats", json={"filename": "m.archive", "arrangement": 0, "seconds": 40, "count_play": True})
    client.post("/api/stats", json={"filename": "m.archive", "arrangement": 1, "seconds": 40, "count_play": True})
    body = _stats(client, "m.archive")
    assert body["play_count"] == 2
    assert [a["play_count"] for a in body["arrangements"]] == [1, 1]


# ── full combos ───────────────────────────────────────────────────────────────

def test_full_combo_counts_only_with_perfect_accuracy(client):
    r = client.post("/api/stats", json={"filename": "fc.archive", "score": 1000, "accuracy": 1.0,
                                        "seconds": 180, "count_play": True, "full_combo": True})
    row = r.json()["stats"]
    assert row["full_combos"] == 1 and row["play_count"] == 1
    # A flag that disagrees with the accuracy it rides with is ignored.
    client.post("/api/stats", json={"filename": "fc.archive", "score": 900, "accuracy": 0.97,
                                    "full_combo": True})
    assert _stats(client, "fc.archive")["full_combos"] == 1


def test_full_combo_without_count_play_doesnt_count_a_play(client):
    # The play was already counted by an earlier flush in the same session.
    client.post("/api/stats", json={"filename": "fc2.archive", "lastPlayPosition": 40.0,
                                    "seconds": 40, "count_play": True})
    client.post("/api/stats", json={"filename": "fc2.archive", "score": 800, "accuracy": 1.0,
                                    "full_combo": True})
    body = _stats(client, "fc2.archive")
    assert body["full_combos"] == 1 and body["play_count"] == 1


def test_full_combo_ignored_on_unscored_posts(client):
    client.post("/api/stats", json={"filename": "u.archive", "seconds": 40,
                                    "count_play": True, "full_combo": True})
    client.post("/api/stats", json={"filename": "u.archive", "lastPlayPosition": 3.0,
                                    "count_play": True, "full_combo": True})
    body = _stats(client, "u.archive")
    assert body["full_combos"] == 0 and body["play_count"] == 2


# ── badge map ─────────────────────────────────────────────────────────────────

def test_plays_map_lists_played_songs_only(client, server):
    for fn in ("p.archive", "fc.archive", "touched.archive", "never.archive"):
        _put(server, fn, fn, "A")
    client.post("/api/stats", json={"filename": "p.archive", "seconds": 40, "count_play": True})
    client.post("/api/stats", json={"filename": "p.archive", "seconds": 40, "count_play": True})
    client.post("/api/stats", json={"filename": "fc.archive", "score": 9, "accuracy": 1.0,
                                    "seconds": 40, "count_play": True, "full_combo": True})
    client.post("/api/stats", json={"filename": "touched.archive", "lastPlayPosition": 2.0})
    client.post("/api/stats", json={"filename": "ghost.archive", "seconds": 40, "count_play": True})
    m = client.get("/api/stats/plays").json()
    assert m == {"p.archive": [2, 0], "fc.archive": [1, 1]}   # no resume-only / unplayed / dead songs


# ── "Most played" sort ────────────────────────────────────────────────────────

def _play(client, fn, n):
    for _ in range(n):
        client.post("/api/stats", json={"filename": fn, "seconds": 40, "count_play": True})


def test_plays_sort_most_played_first_unplayed_last(client, server):
    _put(server, "a.archive", "Alpha", "Zed")
    _put(server, "b.archive", "Bravo", "Abe")
    _put(server, "c.archive", "Charlie", "Mid")
    _put(server, "d.archive", "Delta", "Abe")
    _play(client, "c.archive", 3)
    _play(client, "a.archive", 1)
    # A resume-only touch is not a play: d stays with the unplayed.
    client.post("/api/stats", json={"filename": "d.archive", "lastPlayPosition": 9.0})
    order = [s["filename"] for s in client.get("/api/library", params={"sort": "plays"}).json()["songs"]]
    # c (3) > a (1), then the unplayed by artist → title (Abe: Bravo, Delta).
    assert order == ["c.archive", "a.archive", "b.archive", "d.archive"]


def test_plays_sort_pages_consistently(client, server):
    for i in range(7):
        _put(server, f"s{i}.archive", f"T{i}", "A")
        _play(client, f"s{i}.archive", i % 3)
    full = [s["filename"] for s in client.get("/api/library", params={"sort": "plays", "size": 50}).json()["songs"]]
    paged = []
    for p in range(3):
        body = client.get("/api/library", params={"sort": "plays", "size": 3, "page": p}).json()
        paged += [s["filename"] for s in body["songs"]]
    assert paged == full and len(set(full)) == 7


def test_plays_sort_aggregates_across_group(client, server):
    _put(server, "w1.archive", "One", "Artist", arrangements=2)   # keeper (most arrangements)
    _put(server, "w2.archive", "One", "Artist", arrangements=1)   # another chart of the same work
    _put(server, "x.archive", "Two", "Artist")
    _play(client, "w2.archive", 3)
    _play(client, "w1.archive", 1)
    _play(client, "x.archive", 2)
    body = client.get("/api/library", params={"group": 1, "sort": "plays"}).json()
    # Work 1 = 1 + 3 plays across its charts > x's 2, shown under its keeper row.
    assert [s["filename"] for s in body["songs"]] == ["w1.archive", "x.archive"]


# ── migration ─────────────────────────────────────────────────────────────────

def test_play_count_backfilled_from_scored_plays_once(tmp_path):
    from metadata_db import MetadataDB
    # A pre-#49 database: song_stats without play_count / full_combos.
    conn = sqlite3.connect(str(tmp_path / "web_library.db"))
    conn.execute(
        "CREATE TABLE song_stats (filename TEXT NOT NULL, arrangement INTEGER NOT NULL DEFAULT 0, "
        "plays INTEGER NOT NULL DEFAULT 0, best_score INTEGER NOT NULL DEFAULT 0, "
        "best_accuracy REAL NOT NULL DEFAULT 0, last_score INTEGER NOT NULL DEFAULT 0, "
        "last_accuracy REAL NOT NULL DEFAULT 0, last_position REAL NOT NULL DEFAULT 0, "
        "last_played_at TEXT, updated_at TEXT, PRIMARY KEY (filename, arrangement))")
    conn.execute("INSERT INTO song_stats (filename, arrangement, plays) VALUES ('old.archive', 0, 4)")
    conn.commit()
    conn.close()
    db = MetadataDB(tmp_path)
    try:
        row = db._stats_row("old.archive", 0)
        assert row["play_count"] == 4 and row["full_combos"] == 0
        db.count_play("old.archive", 0)
    finally:
        db.conn.close()
    # Re-opening must not re-seed (the ALTER only succeeds the first time).
    db = MetadataDB(tmp_path)
    try:
        assert db._stats_row("old.archive", 0)["play_count"] == 5
    finally:
        db.conn.close()
