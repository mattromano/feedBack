"""rs_profile_counts (mattromano custom build): Rocksmith profile + PSARC reading (lib/rspsarc.py)
and the import into the native play count, on synthetic files (no real profile data)."""

import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest

import rspsarc
from metadata_db import MetadataDB

ROUTES = Path(__file__).resolve().parents[3] / "plugins" / "rs_profile_counts" / "routes.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("rs_profile_counts_routes_under_test", ROUTES)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── synthetic Rocksmith files ───────────────────────────────────────────────

def _ecb_encrypt(key, data):
    a = rspsarc._AES(key)
    return b"".join(a.encrypt_block(data[i:i + 16]) for i in range(0, len(data), 16))


def _cfb_encrypt(key, iv, data):
    a = rspsarc._AES(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        c = bytes(x ^ y for x, y in zip(data[i:i + 16], a.encrypt_block(prev)))
        out += c
        prev = c
    return bytes(out)


def write_profile(path, songs: dict):
    body = zlib.compress(json.dumps({"Stats": {"Songs": songs}}).encode())
    body += b"\0" * (-len(body) % 16)
    path.write_bytes(b"EVAS" + b"\0" * 16 + _ecb_encrypt(rspsarc.PROFILE_KEY, body))
    return path


def write_psarc(path, files: dict, encrypt=True):
    """Minimal PSARC (zlib blocks, 64 KiB block size); entry 0 is the file list."""
    names = list(files)
    blobs = ["\n".join(names).encode()] + [files[n] for n in names]
    entry_size, block = 30, 65536
    n = len(blobs)
    comp = [zlib.compress(b) for b in blobs]           # all tiny: one block each
    toc_len = 32 + entry_size * n + 2 * n
    pad = -(toc_len - 32) % 16
    off = toc_len + pad
    toc, sizes = b"", b""
    for i, (raw, c) in enumerate(zip(blobs, comp)):
        toc += b"\0" * 16 + struct.pack(">I", i) + len(raw).to_bytes(5, "big") + off.to_bytes(5, "big")
        sizes += struct.pack(">H", len(c))
        off += len(c)
    region = toc + sizes + b"\0" * pad
    if encrypt:
        region = _cfb_encrypt(rspsarc.PSARC_KEY, rspsarc.PSARC_IV, region)
    head = b"PSAR" + struct.pack(">I4sIIIII", 0x10004, b"zlib", toc_len + pad, entry_size, n, block,
                                 4 if encrypt else 0)
    path.write_bytes(head + region + b"".join(comp))
    return path


def song_manifest(song, artist, arrs):
    return json.dumps({"Entries": {pid: {"Attributes": {"PersistentID": pid, "ArrangementName": name,
                                                       "SongName": song, "ArtistName": artist, "Tuning": {},
                                                       "CentOffset": cents}}
                                   for pid, name, cents in arrs}}).encode()


# ── lib/rspsarc.py ──────────────────────────────────────────────────────────

def test_aes_matches_fips197():
    a = rspsarc._AES(bytes(range(32)))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = a.encrypt_block(pt)
    assert ct.hex() == "8ea2b7ca516745bfeafc49904b496089"
    assert a.decrypt_block(ct) == pt


def test_profile_round_trip(tmp_path):
    p = write_profile(tmp_path / "abc_prfldb", {"aa11": {"PlayedCount": 3}})
    assert rspsarc.profile_song_stats(p) == {"AA11": {"PlayedCount": 3}}
    (tmp_path / "x_prfldb").write_bytes(b"nope")
    with pytest.raises(ValueError):
        rspsarc.read_profile(tmp_path / "x_prfldb")


@pytest.mark.parametrize("encrypt", [True, False])
def test_psarc_arrangements(tmp_path, encrypt):
    p = write_psarc(tmp_path / "s.psarc", {
        "manifests/songs_dlc_s/s_lead.json": song_manifest("Wonderwall", "Oasis", [("P1", "Lead", 45.0)]),
        "manifests/songs_dlc_s/s_vocals.json": song_manifest("Wonderwall", "Oasis", [("P9", "Vocals", 0)]),
        "songs/bin/generic/s_lead.sng": b"\x01" * 40,
    }, encrypt=encrypt)
    rows = rspsarc.psarc_arrangements(p)
    assert [(r["persistent_id"], r["name"], r["song"], r["cent_offset"]) for r in rows] == \
        [("P1", "Lead", "Wonderwall", 45.0)]


# ── plan + import ───────────────────────────────────────────────────────────

def test_plan_matches_songs_and_parts(mod):
    stats = {"L1": {"PlayedCount": 5}, "B1": {"PlayedCount": 2}, "R2": {"PlayedCount": 1}, "Z": {"PlayedCount": 9}}
    psarcs = {"a.psarc": [{"persistent_id": "L1", "name": "Lead", "song": "Wonderwall", "artist": "Oasis"},
                          {"persistent_id": "B1", "name": "Bass", "song": "Wonderwall", "artist": "Oasis"}],
              "b.psarc": [{"persistent_id": "R2", "name": "Rhythm", "song": "Song 2 (Remastered)", "artist": "Blur"}]}
    lib = [("oasis_wonderwall.sloppak", "Oasis", "Wonderwall",
            [{"index": 0, "name": "Lead"}, {"index": 1, "name": "Rhythm"}, {"index": 2, "name": "Bass"}]),
           ("wonderwall_lead_only.sloppak", "Oasis", "Wonderwall", [{"index": 0, "name": "Lead"}]),
           ("blur.sloppak", "Blur", "Song 2", [{"index": 0, "name": "Lead"}])]
    assert mod.plan(stats, psarcs, lib) == {("oasis_wonderwall.sloppak", 0): 5, ("oasis_wonderwall.sloppak", 2): 2,
                                           ("blur.sloppak", 0): 1}      # no Rhythm chart: first arrangement


def test_import_adds_only_new_plays(mod, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    db = MetadataDB(cfg)
    db.put("oasis.sloppak", 0, 0, {"title": "Wonderwall", "artist": "Oasis",
                                   "arrangements": [{"name": "Lead", "index": 0}, {"name": "Bass", "index": 1}]})
    db.put("other.sloppak", 0, 0, {"title": "Other", "artist": "Band", "arrangements": [{"name": "Lead", "index": 0}]})
    db.count_play("oasis.sloppak", 0)                           # one feedBack play already
    rs = tmp_path / "rs"
    rs.mkdir()
    write_psarc(rs / "oasis_p.psarc", {"manifests/x/x.json": song_manifest(
        "Wonderwall", "Oasis", [("L1", "Lead", 0), ("B1", "Bass", 0)])})
    prof = write_profile(tmp_path / "me_prfldb", {"L1": {"PlayedCount": 4}, "B1": {"PlayedCount": 2}})
    imp = mod.Importer(cfg, db)
    imp.save_settings({"profile": str(prof), "psarc_dirs": str(rs), "auto_import": False})
    r = imp.run()
    assert r["ok"] and r["rocksmith_plays"] == 6 and r["added"] == 6, r
    assert db.play_counts_map()["oasis.sloppak"][0] == 1 + 6
    # nothing new: no change, and the auto path skips outright
    assert imp.run()["added"] == 0
    assert "skipped" in imp.run(only_if_changed=True)
    assert db.play_counts_map()["oasis.sloppak"][0] == 7
    # two more Rocksmith plays: only those are added
    write_profile(prof, {"L1": {"PlayedCount": 6}, "B1": {"PlayedCount": 2}})
    r = imp.run()
    assert r["added"] == 2 and db.play_counts_map()["oasis.sloppak"][0] == 9
    assert "other.sloppak" not in db.play_counts_map()
    db.conn.close()


def test_import_without_profile_reports_it(mod, tmp_path):
    db = MetadataDB(tmp_path)
    imp = mod.Importer(tmp_path, db)
    imp.save_settings({"profile": str(tmp_path / "missing_prfldb")})
    r = imp.run()
    assert not r["ok"] and "profile not found" in r["error"]
    db.conn.close()
