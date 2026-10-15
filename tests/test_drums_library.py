"""lib/chorus.py (Chorus Encore client) and scripts/drums_library.py (library-wide drum joins).
mattromano custom build, ported from the Slopsmith 0.2.9 tools. The chorus tests need nothing
extra; the end-to-end library run needs librosa/scipy/soundfile and ffmpeg (skipped without)."""
import json
import sys
import zipfile
from pathlib import Path

import pytest

import chorus
import drumchart
from tests.drum_fixtures import write_mid, write_sng

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drums_library  # noqa: E402


def _have_audio_stack():
    try:
        import librosa  # noqa: F401
        import scipy  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        return False
    from tests.drumjoin_fixtures import ffmpeg
    return ffmpeg() is not None


needs_audio = pytest.mark.skipif(not _have_audio_stack(), reason="needs librosa, scipy, soundfile and ffmpeg")


def _sng(tmp_path, *, audio=b"OggS fake drums", seed=7):
    from tests.drumjoin_fixtures import drum_pattern
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    write_mid(src / "notes.mid", drum_pattern(20, seed=seed) + [(1920 * 4, 116, 100, 1920)])
    chart = drumchart.parse_mid(src / "notes.mid")
    if callable(audio):
        audio = audio(chart)
    files = {"notes.mid": (src / "notes.mid").read_bytes(), "song.ogg": b"x" * 100, "video.mp4": b"v" * 50,
             "drums_1.ogg": audio, "drums_2.ogg": audio}
    p = write_sng(tmp_path / "Band - Song.sng", files, {"name": "Song", "artist": "Band", "delay": "0"})
    return p, chart


def test_chorus_fetch_takes_only_chart_and_drum_stems(tmp_path, monkeypatch):
    p, _ = _sng(tmp_path)
    blob = p.read_bytes()
    calls = []

    def fake_range(md5, a, b):
        calls.append((a, b))
        return blob[a:b + 1]
    monkeypatch.setattr(chorus, "_range", fake_range)
    out = chorus.fetch("abc", tmp_path / "cache" / "abc")
    assert sorted(x.name for x in out.iterdir()) == [".complete", "drums_1.ogg", "drums_2.ogg", "notes.mid", "song.ini"]
    assert (out / "drums_1.ogg").read_bytes() == b"OggS fake drums"           # unmasked
    assert "artist = Band" in (out / "song.ini").read_text(encoding="utf-8")
    n = len(calls)
    chorus.fetch("abc", tmp_path / "cache" / "abc")              # cached: no more requests
    assert len(calls) == n


def test_chorus_search_keeps_only_drum_charts(monkeypatch):
    data = {"data": [{"md5": "a", "notesData": {"instruments": ["guitar"]}, "diff_drums": -1},
                     {"md5": "b", "notesData": {"instruments": ["drums", "guitar"]}, "diff_drums": 3},
                     {"md5": "c", "notesData": None, "diff_drums": 0}]}
    monkeypatch.setattr(chorus, "_req", lambda *a, **k: json.dumps(data).encode())
    assert [r["md5"] for r in chorus.search("x")] == ["b", "c"]


def test_online_candidates_rank_by_name_then_length(monkeypatch):
    res = [{"md5": "live", "artist": "Band", "name": "Song (Live)", "song_length": 300000, "pro_drums": True},
           {"md5": "studio", "artist": "Band", "name": "Song", "song_length": 201000, "pro_drums": False},
           {"md5": "other", "artist": "Band", "name": "Other Song", "song_length": 200000},
           {"md5": "studio", "artist": "Band", "name": "Song", "song_length": 201000}]
    monkeypatch.setattr(chorus, "search", lambda q: res)
    c = drums_library.online_candidates("Band", "Song", 200.0, 0.86)
    assert [x["md5"] for x in c] == ["studio", "live"]


def test_clean_title():
    assert drums_library.clean_title("Welcome To The Family (Bass)") == "Welcome To The Family"
    assert drums_library.clean_title("Song [DD] (Remastered 2011)") == "Song"
    assert drums_library.clean_title("Lauren (Track 2)") == "Lauren (Track 2)"


def test_ensure_backup(tmp_path):
    src = tmp_path / "a.sloppak"
    src.write_bytes(b"1234")
    bd = tmp_path / "bak"
    b1 = drums_library.ensure_backup(src, bd)
    assert b1.read_bytes() == b"1234"
    assert drums_library.ensure_backup(src, bd) == b1 and len(list(bd.iterdir())) == 1
    src.write_bytes(b"123456")                                     # changed since: keep both
    b2 = drums_library.ensure_backup(src, bd)
    assert b2 != b1 and b1.read_bytes() == b"1234" and len(list(bd.iterdir())) == 2


def _pack(tmp_path, manifest: dict, files: dict) -> Path:
    import yaml
    p = tmp_path / "p.sloppak"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest))
        for rel, data in files.items():
            z.writestr(rel, json.dumps(data))
    return p


def test_current_levels_reads_both_drum_shapes(tmp_path):
    lead = {"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}
    # feedpak drum tab: Easy hand-charted, Medium/Hard reduced by software
    tab = {"version": 1, "hits": [], "levels": {"easy": {"hits": [{"t": 1, "p": "kick"}]},
                                                 "medium": {"hits": [{"t": 1, "p": "kick"}], "generated": True},
                                                 "hard": {"hits": [{"t": 1, "p": "kick"}], "generated": True}}}
    man = {"arrangements": [lead, {"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drum_tab.json"}],
           "drum_tab": "drum_tab.json"}
    p = _pack(tmp_path, man, {"drum_tab.json": tab})
    assert drums_library.has_drums(man) and drums_library.current_levels(p) == 1
    # 0.2.9 tools: guitar-style arrangement with a drums block
    legacy = {"name": "Drums", "notes": [], "drums": {"levels": {"easy": [[1]], "medium": [[1]], "hard": [[1]]},
                                                       "levels_generated": ["hard"]}}
    man = {"arrangements": [lead, {"id": "drums", "name": "Drums", "file": "arrangements/drums.json"}]}
    p.unlink()
    p = _pack(tmp_path, man, {"arrangements/drums.json": legacy})
    assert drums_library.has_drums(man) and drums_library.current_levels(p) == 2
    man = {"arrangements": [lead]}
    p.unlink()
    p = _pack(tmp_path, man, {})
    assert not drums_library.has_drums(man) and drums_library.current_levels(p) == -1


@needs_audio
def test_library_run_end_to_end(tmp_path, monkeypatch):
    from tests.drumjoin_fixtures import drum_audio, make_sloppak, write_audio

    def ogg(chart):
        write_audio(tmp_path / "a.ogg", drum_audio([(h.time, h.pad, h.cymbal) for h in chart.hits], 44, seed=2))
        return (tmp_path / "a.ogg").read_bytes()
    sng, chart = _sng(tmp_path, audio=ogg)
    lib = tmp_path / "lib"
    lib.mkdir()
    hits = [(h.time + 1.5, h.pad, h.cymbal) for h in chart.hits]
    make_sloppak(lib / "song.sloppak", {"drums": drum_audio(hits, 48, seed=5, noise=0.003)},
                 beats=[{"time": 0.5 * i, "measure": -1} for i in range(90)], title="Song", artist="Band")
    make_sloppak(lib / "nomatch.sloppak", {"drums": drum_audio(hits, 48)}, title="Unknown", artist="Nobody")
    bd = tmp_path / "bak"
    bd.mkdir()
    (bd / "placeholder").write_text("x")
    state = tmp_path / "state.json"
    argv = ["drums_library.py", str(lib), "--backup-dir", str(bd), "--local", str(tmp_path), "--no-online",
            "--workers", "1", "--state", str(state)]
    monkeypatch.setattr(sys, "argv", argv)
    drums_library.main()                                           # dry run first
    s = json.loads(state.read_text())
    assert s["song.sloppak"]["status"] == "would-join" and s["nomatch.sloppak"]["status"] == "no-chart"
    assert "drum_tab.json" not in zipfile.ZipFile(lib / "song.sloppak").namelist()
    monkeypatch.setattr(sys, "argv", argv + ["--write"])
    drums_library.main()                                           # would-join songs are redone with --write
    s = json.loads(state.read_text())
    assert s["song.sloppak"]["status"] == "joined"
    assert "drum_tab.json" in zipfile.ZipFile(lib / "song.sloppak").namelist()
    assert (bd / "song.sloppak").exists()                          # backed up before the write
    assert "drum_tab.json" not in zipfile.ZipFile(bd / "song.sloppak").namelist()
    assert (state.with_suffix(".csv")).read_text().count("\n") == 3
