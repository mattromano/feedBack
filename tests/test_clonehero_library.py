"""The Clone Hero / YARG songs folder in the library (lib/clonehero.py).

Songs are indexed from song.ini (the chart is only sniffed for parts song.ini does
not mention), listed under ``clonehero-cache/<path>.feedpak`` and converted into
that folder of the DLC on first play.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

import clonehero
import drumchart
import fivefretchart
import scan_worker
import sloppak as sloppak_mod
import vocalchart
from tests.drum_fixtures import FAKE_OGG, TPQ, drum_pattern, write_chart, write_ini, write_mid, write_sng

ALL_DIFFS = dict(diff_drums=3, diff_guitar=2, diff_rhythm=-1, diff_bass=1, diff_vocals=2)


def _mid_song(folder, *, drums=True, guitar=True, bass=True, vocals=True, ini=ALL_DIFFS, stems=("song",)):
    folder.mkdir(parents=True)
    extra = []
    if guitar:
        extra.append(("PART GUITAR", [(i * TPQ, 96 + i % 5, 100, 10) for i in range(16)], []))
    if bass:
        extra.append(("PART BASS", [(i * TPQ, 96 + i % 3, 100, 10) for i in range(16)], []))
    if vocals:
        extra.append(("PART VOCALS", [(TPQ * 2, 60, 100, TPQ // 2), (TPQ * 3, 62, 100, TPQ // 2)],
                      [(TPQ * 2, "Hel-"), (TPQ * 3, "lo")], "lyrics"))
    write_mid(folder / "notes.mid", drum_pattern(4) if drums else [],
              track_name="PART DRUMS" if drums else None, sections=[(0, "Intro")], extra_tracks=extra)
    write_ini(folder / "song.ini", name="Mid Song", artist="Band", album="LP", year="2004", genre="Rock",
              song_length=9000, **(ini or {}))
    for s in stems:
        (folder / f"{s}.ogg").write_bytes(FAKE_OGG)
    (folder / "album.png").write_bytes(b"\x89PNG-art")
    return folder


def _chart_song(folder, ini=None):
    folder.mkdir(parents=True)
    write_chart(folder / "notes.chart", [(i * 192, "N", 0, 0) for i in range(8)], name="Chart Song",
                artist="Band", extra_sections={"ExpertDoubleBass": [(i * 192, "N", i % 5, 0) for i in range(8)]})
    write_ini(folder / "song.ini", name="Chart Song", artist="Band", **(ini or {}))
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    return folder


def _sng_song(path, tmp_path, ini=ALL_DIFFS):
    src = _mid_song(tmp_path / "sng_src", stems=("guitar", "drums"))
    files = {p.name: p.read_bytes() for p in src.iterdir() if p.name != "song.ini"}
    meta = {"name": "Sng Song", "artist": "Band", **{k: str(v) for k, v in (ini or {}).items()}}
    return write_sng(path, files, meta)


# ── listing + keys ───────────────────────────────────────────────────────────

def test_list_songs_finds_folders_and_sng(tmp_path):
    root = tmp_path / "Songs"
    a = _mid_song(root / "Pack" / "A")
    b = _chart_song(root / "B")
    (root / "Pack" / "A" / "nested").mkdir()                    # never descended into
    (root / "Highways").mkdir()                                 # CH assets: no song.ini
    (root / "Highways" / "x.png").write_bytes(b"")
    (root / ".hidden").mkdir()
    _mid_song(root / ".hidden" / "C")
    sng = _sng_song(root / "Pack" / "s.sng", tmp_path)
    assert clonehero.list_songs(root) == sorted([a, b, sng])


def test_key_round_trip_and_containment(tmp_path):
    root = tmp_path / "Songs"
    a = _mid_song(root / "Artist - Song")
    key = clonehero.library_key(a, root)
    assert key == "clonehero-cache/Artist - Song.feedpak"
    assert sloppak_mod.is_sloppak(tmp_path / key)
    assert clonehero.source_for_key(key, root) == a
    for bad in ("clonehero-cache/../x.feedpak", "clonehero-cache/Artist - Song", "other/Artist - Song.feedpak",
                "clonehero-cache/missing.feedpak", "clonehero-cache//etc.feedpak"):
        assert clonehero.source_for_key(bad, root) is None, bad
    assert clonehero.source_for_key(key, None) is None


def test_songs_dir_setting_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(clonehero, "DEFAULT_DIRS", ("~/Documents/Clone Hero/Songs", "~/Clone Hero/Songs"))
    assert clonehero.songs_dir({}) is None
    default = tmp_path / "Documents" / "Clone Hero" / "Songs"
    default.mkdir(parents=True)
    assert clonehero.songs_dir({}) == default
    other = tmp_path / "mine"
    other.mkdir()
    assert clonehero.songs_dir({"clonehero_dir": str(other)}) == other
    assert clonehero.songs_dir({"clonehero_dir": str(tmp_path / "gone")}) is None


# ── indexing ─────────────────────────────────────────────────────────────────

def test_index_from_song_ini_never_parses_the_chart(tmp_path, monkeypatch):
    song = _mid_song(tmp_path / "S", stems=("guitar", "bass", "drums_1", "drums_2", "vocals", "crowd"))

    def boom(*a, **kw):
        raise AssertionError("chart parsed")
    for mod in (drumchart, fivefretchart, vocalchart):
        monkeypatch.setattr(mod, "parse_mid", boom)
    monkeypatch.setattr(clonehero, "mid_track_names", boom)
    m = scan_worker._extract_meta_clonehero(song)
    assert (m["title"], m["artist"], m["album"], m["year"], m["genre"]) == ("Mid Song", "Band", "LP", "2004", "Rock")
    assert m["duration"] == 9.0
    assert m["has_drum_tab"] is True
    assert m["five_fret_parts"] == ["guitar:chart", "bass:chart"]       # diff_rhythm = -1
    assert m["five_fret_only"] is False
    assert m["has_lyrics"] and m["has_vocal_pitch"]
    assert m["stem_ids"] == ["guitar", "bass", "drums", "vocals"]       # crowd dropped
    assert [a["name"] for a in m["arrangements"]] == ["Drums"]
    assert m["format"] == "sloppak" and m["playable"] is True


def test_index_sniffs_mid_track_names_without_diff_keys(tmp_path):
    song = _mid_song(tmp_path / "S", drums=False, vocals=False, ini={})
    m = clonehero.index_meta(song)
    assert m["has_drum_tab"] is False
    assert m["five_fret_parts"] == ["guitar:chart", "bass:chart"]
    assert m["five_fret_only"] is True
    assert m["has_lyrics"] is False
    assert [a["name"] for a in m["arrangements"]] == ["Guitar (5-fret)"]
    assert m["stem_ids"] == ["full"]


def test_mid_track_names(tmp_path):
    p = write_mid(tmp_path / "n.mid", [], track_name=None,
                  extra_tracks=[("PART BASS", [(0, 96, 100, 10)], []), ("PART VOCALS", [], [], "lyrics")])
    assert clonehero.mid_track_names(p.read_bytes()) == {"sync", "EVENTS", "PART BASS", "PART VOCALS"}


def test_index_chart_sections_and_no_vocals(tmp_path):
    song = _chart_song(tmp_path / "C", ini={"diff_vocals": 3})
    m = clonehero.index_meta(song)
    assert m["has_drum_tab"] is True
    assert m["five_fret_parts"] == ["bass:chart"]
    assert m["has_lyrics"] is False                 # vocals only come from notes.mid


def test_index_unplayable_song(tmp_path):
    song = _mid_song(tmp_path / "V", drums=False, guitar=False, bass=False,
                     ini=dict(diff_drums=-1, diff_guitar=-1, diff_rhythm=-1, diff_bass=-1, diff_vocals=1))
    assert clonehero.index_meta(song)["playable"] is False


def test_index_sng_header_and_sniff(tmp_path):
    sng = _sng_song(tmp_path / "a.sng", tmp_path, ini=None)
    m = clonehero.index_meta(sng)
    assert m["title"] == "Sng Song"
    assert m["has_drum_tab"] is True
    assert m["five_fret_parts"] == ["guitar:chart", "bass:chart"]
    assert m["has_lyrics"] is True
    assert m["stem_ids"] == ["guitar", "drums"]


def test_source_cover_folder_and_sng(tmp_path):
    assert clonehero.source_cover(_mid_song(tmp_path / "S")) == (b"\x89PNG-art", "image/png")
    assert clonehero.source_cover(_sng_song(tmp_path / "a.sng", tmp_path)) == (b"\x89PNG-art", "image/png")
    assert clonehero.source_cover(_chart_song(tmp_path / "C")) is None


# ── conversion ───────────────────────────────────────────────────────────────

def test_ensure_converted_builds_once_and_rebuilds_when_source_changes(tmp_path):
    root, dlc = tmp_path / "Songs", tmp_path / "dlc"
    song = _mid_song(root / "S")
    key = clonehero.library_key(song, root)
    pack, built = clonehero.ensure_converted(key, song, dlc)
    assert built and pack == dlc / key and pack.is_dir()
    meta = sloppak_mod.extract_meta(pack)
    idx = clonehero.index_meta(song)
    for k in ("title", "has_drum_tab", "five_fret_parts", "has_lyrics", "has_vocal_pitch"):
        assert meta[k] == idx[k], k
    assert clonehero.ensure_converted(key, song, dlc) == (pack, False)
    later = time.time() + 5
    os.utime(song / "song.ini", (later, later))
    assert clonehero.ensure_converted(key, song, dlc) == (pack, True)
    assert not [p for p in (dlc / "clonehero-cache").iterdir() if p.name.startswith(".convert-")]


def test_failed_conversion_leaves_no_pack(tmp_path):
    root, dlc = tmp_path / "Songs", tmp_path / "dlc"
    song = _mid_song(root / "S", stems=())                      # no audio: the converter refuses
    key = clonehero.library_key(song, root)
    with pytest.raises(ValueError, match="no audio"):
        clonehero.ensure_converted(key, song, dlc)
    assert not (dlc / key).exists()
    assert list((dlc / "clonehero-cache").iterdir()) == []


def test_ensure_converted_sng(tmp_path):
    root, dlc = tmp_path / "Songs", tmp_path / "dlc"
    root.mkdir()
    sng = _sng_song(root / "a.sng", tmp_path)
    pack, _ = clonehero.ensure_converted(clonehero.library_key(sng, root), sng, dlc)
    ff = json.loads((pack / "five_fret_guitar.json").read_text())
    assert ff["source"]["md5"] == clonehero._md5(sng)
    assert sorted(p.name for p in (pack / "stems").iterdir()) == ["drums.ogg", "guitar.ogg"]


def test_prune_cache(tmp_path):
    dlc = tmp_path / "dlc"
    keep = dlc / "clonehero-cache" / "Pack" / "A.feedpak"
    gone = dlc / "clonehero-cache" / "Pack" / "B.feedpak"
    gone_nested = dlc / "clonehero-cache" / "Old" / "#DLC" / "C.feedpak"
    fresh_tmp = dlc / "clonehero-cache" / ".convert-new"
    old_tmp = dlc / "clonehero-cache" / ".convert-old"
    for d in (keep, gone, gone_nested, fresh_tmp, old_tmp):
        d.mkdir(parents=True)
    os.utime(old_tmp, (time.time() - 7200,) * 2)
    assert clonehero.prune_cache(dlc, {"clonehero-cache/Pack/A.feedpak"}) == 3
    assert keep.exists() and fresh_tmp.exists()
    assert not (dlc / "clonehero-cache" / "Old").exists()           # emptied folders go too
    assert not gone.exists() and not old_tmp.exists()


# ── the app: scan, library filters, play ─────────────────────────────────────

@pytest.fixture()
def app(tmp_path, monkeypatch, isolate_logging, reset_scan_state):
    """Fresh server with a DLC folder and a Clone Hero songs folder configured, the
    scan pool swapped for threads (the spawn pool can't run in-process)."""
    cfg_dir, dlc, root = tmp_path / "config", tmp_path / "dlc", tmp_path / "Songs"
    for d in (cfg_dir, dlc, root):
        d.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"dlc_dir": str(dlc), "clonehero_dir": str(root)}))
    monkeypatch.setenv("CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("DLC_DIR", raising=False)
    monkeypatch.setenv("FEEDBACK_SYNC_STARTUP", "1")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "load_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(server, "startup_scan", lambda: None)
    import appstate
    monkeypatch.setattr(appstate, "sloppak_cache_dir", tmp_path / "cache")
    import scan
    monkeypatch.setattr(scan, "_make_scan_executor", lambda: concurrent.futures.ThreadPoolExecutor(max_workers=2))
    import builtin_content
    monkeypatch.setattr(builtin_content, "seed_builtin_diagnostic_sloppaks", lambda *a: None)
    monkeypatch.setattr(builtin_content, "seed_builtin_starter_content", lambda *a: None)
    server._test_paths = (dlc, root)
    yield server
    conn = getattr(getattr(server, "meta_db", None), "conn", None)
    if conn is not None:
        getattr(server, "_join_background_db_threads", lambda: None)()
        conn.close()


def _songs(client, **params):
    return {s["filename"]: s for s in client.get("/api/library", params={"size": 100, **params}).json()["songs"]}


def test_scan_lists_songs_with_parts_and_filters(app):
    import scan
    dlc, root = app._test_paths
    _mid_song(root / "Full")
    _chart_song(root / "Chart")
    _mid_song(root / "GuitarOnly", drums=False, bass=False, vocals=False,
              ini=dict(diff_drums=-1, diff_guitar=1, diff_rhythm=-1, diff_bass=-1, diff_vocals=-1))
    _mid_song(root / "VocalsOnly", drums=False, guitar=False, bass=False,
              ini=dict(diff_drums=-1, diff_guitar=-1, diff_rhythm=-1, diff_bass=-1, diff_vocals=1))
    scan.background_scan()
    with TestClient(app.app) as client:
        songs = _songs(client)
        full, chart, guitar = ("clonehero-cache/Full.feedpak", "clonehero-cache/Chart.feedpak",
                               "clonehero-cache/GuitarOnly.feedpak")
        assert set(songs) == {full, chart, guitar}           # vocals-only can't be played
        assert songs[full]["title"] == "Mid Song"
        assert set(_songs(client, has_drums="1")) == {full, chart}
        assert set(_songs(client, has_drums="0")) == {guitar}
        assert set(_songs(client, five_fret="guitar")) == {full, guitar}
        assert set(_songs(client, five_fret="bass")) == {full, chart}
        assert set(_songs(client, has_lyrics="1")) == {full}
        # nothing was converted by the scan
        assert not (dlc / "clonehero-cache").exists() or not any((dlc / "clonehero-cache").rglob("*.json"))
        # art comes straight from the song folder before the first play
        r = client.get(f"/api/song/{full}/art")
        assert r.status_code == 200 and r.content == b"\x89PNG-art"
        # song info before play: the indexed row
        info = client.get(f"/api/song/{full}").json()
        assert info["five_fret_parts"] == ["guitar:chart", "bass:chart"]


def test_rescan_fast_path_sees_new_and_removed_songs(app):
    import scan
    dlc, root = app._test_paths
    _mid_song(root / "Pack" / "A")
    scan.background_scan()
    assert [r["filename"] for r in app.meta_db.query_page(size=10)[0]] == ["clonehero-cache/Pack/A.feedpak"]
    _mid_song(root / "Pack" / "B")
    scan.background_scan()                                  # the Pack dir mtime changed
    rows = {r["filename"] for r in app.meta_db.query_page(size=10)[0]}
    assert rows == {"clonehero-cache/Pack/A.feedpak", "clonehero-cache/Pack/B.feedpak"}
    # a converted pack whose song is removed is pruned, and so is its row
    a_src = root / "Pack" / "A"
    clonehero.ensure_converted("clonehero-cache/Pack/A.feedpak", a_src, dlc)
    import shutil
    shutil.rmtree(a_src)
    scan.background_scan()
    rows = {r["filename"] for r in app.meta_db.query_page(size=10)[0]}
    assert rows == {"clonehero-cache/Pack/B.feedpak"}
    assert not (dlc / "clonehero-cache/Pack/A.feedpak").exists()


def test_converted_packs_are_not_listed_twice(app):
    import scan
    dlc, root = app._test_paths
    song = _mid_song(root / "S")
    clonehero.ensure_converted("clonehero-cache/S.feedpak", song, dlc)
    scan.background_scan(force=True)
    rows = [r["filename"] for r in app.meta_db.query_page(size=10)[0]]
    assert rows == ["clonehero-cache/S.feedpak"]


def _frames(client, path, limit=600):
    out = []
    with client.websocket_connect(path) as ws:
        for _ in range(limit):
            msg = ws.receive_json()
            assert not msg.get("error"), msg
            out.append(msg)
            if msg.get("type") == "ready":
                break
    return out


def test_first_play_converts_and_streams_drums_five_fret_and_lyrics(app):
    import scan
    dlc, root = app._test_paths
    # song.ini claims rhythm, the chart has none: corrected by the first play
    _mid_song(root / "S", ini={**ALL_DIFFS, "diff_rhythm": 1})
    scan.background_scan()
    key = "clonehero-cache/S.feedpak"
    with TestClient(app.app) as client:
        assert _songs(client)[key]["five_fret_parts"] == ["guitar:chart", "rhythm:chart", "bass:chart"]
        frames = _frames(client, f"/ws/highway/{key}")
        stages = [f["stage"] for f in frames if f.get("type") == "loading"]
        assert "Converting Clone Hero song..." in stages
        info = next(f for f in frames if f.get("type") == "song_info")
        assert info["title"] == "Mid Song"
        assert [p["instrument"] for p in info["five_fret_parts"]] == ["guitar", "bass"]
        types = {f.get("type") for f in frames}
        assert {"drum_tab", "five_fret", "five_fret_notes", "lyrics", "ready"} <= types, types
        assert (dlc / key / "manifest.yaml").is_file()
        assert _songs(client)[key]["five_fret_parts"] == ["guitar:chart", "bass:chart"]
        assert app.meta_db.conn.execute("SELECT genre FROM songs WHERE filename = ?", (key,)).fetchone()[0] == "Rock"
        # second play reuses the pack
        built = (dlc / key / "manifest.yaml").stat().st_mtime_ns
        _frames(client, f"/ws/highway/{key}")
        assert (dlc / key / "manifest.yaml").stat().st_mtime_ns == built
        stems = client.get(f"/api/song/{key}", params={"stems": 1}).json()
        assert stems["five_fret_parts"] == ["guitar:chart", "bass:chart"]


def test_play_of_a_song_no_longer_in_the_folder_errors(app):
    with TestClient(app.app) as client:
        with client.websocket_connect("/ws/highway/clonehero-cache/Gone.feedpak") as ws:
            msgs = [ws.receive_json() for _ in range(3)]
        assert any("no longer in the Clone Hero songs folder" in (m.get("error") or "") for m in msgs), msgs


def test_settings_round_trip(app, tmp_path):
    other = tmp_path / "Other"
    other.mkdir()
    with TestClient(app.app) as client:
        assert client.get("/api/settings").json()["clonehero_dir_active"] == str(app._test_paths[1])
        r = client.post("/api/settings", json={"clonehero_dir": str(tmp_path / "nope")}).json()
        assert "Clone Hero songs folder not found" in r["message"]
        client.post("/api/settings", json={"clonehero_dir": str(other)})
        s = client.get("/api/settings").json()
        assert s["clonehero_dir"] == str(other) and s["clonehero_dir_active"] == str(other)
        assert client.post("/api/settings", json={"clonehero_dir": 5}).json()["error"]
