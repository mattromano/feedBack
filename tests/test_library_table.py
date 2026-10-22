"""GET /api/library/table — the Songs table view's payload: every song matching
the library filters as columnar rows joined to the local practice stats."""

import importlib
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


def _put(server, fn, title, artist, album="", year="", arrangements=("Lead",),
         tuning="E Standard", fmt=None, duration=200):
    meta = {
        "title": title, "artist": artist, "album": album, "year": year,
        "duration": duration, "tuning_name": tuning,
        "arrangements": [{"name": n, "index": i} for i, n in enumerate(arrangements)],
    }
    if fmt:
        meta["format"] = fmt
    server.meta_db.put(fn, 0, 0, meta)


def _table(client, **params):
    body = client.get("/api/library/table", params=params).json()
    rows = [dict(zip(body["cols"], r)) for r in body["rows"]]
    return body, {r["filename"]: r for r in rows}


def test_table_rows_carry_metadata_and_practice_stats(client, server):
    _put(server, "a.sloppak", "Song A", "Band", album="LP", year="1999",
         arrangements=("Lead", "Rhythm", "Bass"), duration=187.6)
    _put(server, "b.sloppak", "Song B", "Band")
    client.post("/api/stats", json={"filename": "a.sloppak", "arrangement": 0, "score": 900,
                                    "accuracy": 1.0, "seconds": 190, "count_play": True,
                                    "full_combo": True})
    client.post("/api/stats", json={"filename": "a.sloppak", "arrangement": 2, "score": 300,
                                    "accuracy": 0.6, "seconds": 60, "count_play": True})
    body, rows = _table(client)
    assert body["total"] == 2 and body["truncated"] is False
    a = rows["a.sloppak"]
    assert (a["artist"], a["title"], a["album"], a["year"]) == ("Band", "Song A", "LP", "1999")
    assert a["duration"] == 188 and a["tuning"] == "E Standard"
    assert a["arrangements"] == ["Lead", "Rhythm", "Bass"]
    # Summed across arrangements; best = max across arrangements.
    assert a["plays"] == 2 and a["full_combos"] == 1
    assert a["best_accuracy"] == pytest.approx(1.0)
    assert a["seconds"] == 250 and a["last_played"]
    assert a["play_filename"] == "a.sloppak"
    b = rows["b.sloppak"]
    assert b["plays"] == 0 and b["best_accuracy"] is None and b["last_played"] is None


def test_resume_only_touch_has_no_best(client, server):
    _put(server, "r.sloppak", "R", "X")
    client.post("/api/stats", json={"filename": "r.sloppak", "lastPlayPosition": 12.0, "seconds": 12})
    _, rows = _table(client)
    r = rows["r.sloppak"]
    # A resume-only row has a default best_accuracy of 0 — not a score.
    assert r["best_accuracy"] is None and r["plays"] == 0
    assert r["seconds"] == 12 and r["last_played"]


def test_table_applies_the_library_filters(client, server):
    _put(server, "one.sloppak", "Highway Song", "Alpha")
    _put(server, "two.sloppak", "Other", "Beta")
    _put(server, "three.sloppak", "Third", "Beta", arrangements=("Bass",))
    _, rows = _table(client, q="highway")
    assert set(rows) == {"one.sloppak"}
    _, rows = _table(client, artist="Beta")
    assert set(rows) == {"two.sloppak", "three.sloppak"}
    _, rows = _table(client, arrangements_has="Bass")
    assert set(rows) == {"three.sloppak"}


def test_grouped_table_aggregates_a_works_charts(client, server):
    _put(server, "w1.sloppak", "Same", "Artist", arrangements=("Lead", "Rhythm"))
    _put(server, "w2.sloppak", "Same", "Artist", arrangements=("Lead",))
    _put(server, "x.sloppak", "Different", "Artist")
    client.post("/api/stats", json={"filename": "w2.sloppak", "score": 500, "accuracy": 0.8,
                                    "seconds": 100, "count_play": True})
    client.post("/api/stats", json={"filename": "w1.sloppak", "seconds": 40, "count_play": True})
    body, rows = _table(client, group=1)
    assert body["total"] == 2 and "x.sloppak" in rows
    # Whichever chart the auto-pick keeps as the work's row, it shows the
    # work's whole history — every chart of it — not just its own.
    (keeper,) = set(rows) - {"x.sloppak"}
    w = rows[keeper]
    assert w["plays"] == 2 and w["best_accuracy"] == pytest.approx(0.8) and w["seconds"] == 140


def test_unknown_provider_is_404(client):
    assert client.get("/api/library/table", params={"provider": "nope"}).status_code == 404


def test_table_rows_carry_play_as_facts(client, server):
    """The table's Parts pills and "Play as" menu need each row's arrangement
    indices, drum chart, vocal pitch and 5-fret parts."""
    server.meta_db.put("ff.sloppak", 0, 0, {
        "title": "Full Band", "artist": "X", "duration": 200, "tuning_name": "E Standard",
        "arrangements": [{"name": "Lead", "index": 0}, {"name": "Bass", "index": 1}],
        "has_drum_tab": True, "has_vocal_pitch": True,
        "five_fret_parts": ["guitar:chart", "bass:auto"], "stem_ids": ["drums", "vocals"],
    })
    _put(server, "plain.sloppak", "Plain", "X", arrangements=("Lead", "Rhythm"))
    _, rows = _table(client)
    ff = rows["ff.sloppak"]
    assert ff["arr_index"] == [0, 1]
    assert ff["has_drum_tab"] is True and ff["has_vocal_pitch"] is True
    assert ff["five_fret_parts"] == ["guitar:chart", "bass:auto"]
    assert ff["five_fret_only"] is False and ff["stem_ids"] == ["drums", "vocals"]
    plain = rows["plain.sloppak"]
    assert plain["arr_index"] == [0, 1]
    assert plain["has_drum_tab"] is False and plain["has_vocal_pitch"] is False
    assert plain["five_fret_parts"] == [] and plain["stem_ids"] == []
