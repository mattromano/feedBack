"""song_cents (mattromano custom build): offset resolution from the chart, the source PSARC, or a
browser measurement, cached per library file."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.plugins.rs_profile_counts.test_import import song_manifest, write_psarc

ROUTES = Path(__file__).resolve().parents[3] / "plugins" / "song_cents" / "routes.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("song_cents_routes_under_test", ROUTES)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pack(path, *, cent=None, source=None, stems=("guitar",)):
    man = {"title": "T", "artist": "A", "stems": [{"id": s, "file": f"stems/{s}.ogg"} for s in stems],
           "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}]}
    if source:
        man["source"] = source
    lead = {"name": "Lead", "notes": []}
    if cent is not None:
        lead["centOffset"] = cent
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.yaml", yaml.safe_dump(man))
        z.writestr("arrangements/lead.json", json.dumps(lead))
        for s in stems:
            z.writestr(f"stems/{s}.ogg", b"OggS")
    return path


def test_residual(mod):
    assert mod.residual(-1200) == 0.0
    assert mod.residual(45) == 45.0
    assert mod.residual(-155) == 45.0          # -2 semitones + 45c (the tuning carries the semitones)
    assert mod.residual(None) == 0.0


@pytest.fixture()
def client(mod, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    rs = tmp_path / "Rocksmith2014"
    (rs / "dlc").mkdir(parents=True)
    import rspsarc
    monkeypatch.setattr(rspsarc, "rocksmith_dirs", lambda: [rs])
    app = FastAPI()
    mod.setup(app, {"config_dir": tmp_path, "get_dlc_dir": lambda: str(lib)})
    return TestClient(app), lib, rs, tmp_path


def test_offset_from_chart_then_cached(client):
    c, lib, _, cfg = client
    pack(lib / "sharp.sloppak", cent=45.0)
    r = c.get("/api/plugins/song_cents/offset", params={"file": "sharp.sloppak"}).json()
    assert (r["cents"], r["source"]) == (45.0, "chart")
    assert json.loads((cfg / "song_cents.json").read_text())["sharp.sloppak"]["cents"] == 45.0
    assert c.get("/api/plugins/song_cents/known").json() == {"sharp.sloppak": {"cents": 45.0, "source": "chart"}}


def test_offset_from_the_source_psarc(client):
    c, lib, rs, _ = client
    write_psarc(rs / "dlc" / "oasis_p.psarc", {"manifests/x/x.json": song_manifest("Song", "Oasis", [("P", "Lead", 25.0)])})
    pack(lib / "conv.sloppak", source={"kind": "psarc", "file": "oasis_p.psarc"})
    r = c.get("/api/plugins/song_cents/offset", params={"file": "conv.sloppak"}).json()
    assert (r["cents"], r["source"]) == (25.0, "psarc")


def test_unknown_is_measured_by_the_browser(client):
    c, lib, _, _ = client
    pack(lib / "x.sloppak", stems=("full",))
    r = c.get("/api/plugins/song_cents/offset", params={"file": "x.sloppak"}).json()
    assert r == {"file": "x.sloppak", "cents": None, "source": "unmeasured", "stem": "stems/full.ogg"}
    assert c.get("/api/plugins/song_cents/pending").json() == {"files": ["x.sloppak"]}
    r = c.post("/api/plugins/song_cents/measured", json={"file": "x.sloppak", "cents": 31, "confidence": 0.8,
                                                          "raw": 30.6}).json()
    assert (r["cents"], r["source"]) == (31.0, "audio")
    assert c.get("/api/plugins/song_cents/offset", params={"file": "x.sloppak"}).json()["cents"] == 31.0
    assert c.get("/api/plugins/song_cents/pending").json() == {"files": []}


def test_paths_outside_the_library_are_refused(client):
    c, _, _, _ = client
    assert c.get("/api/plugins/song_cents/offset", params={"file": "../x.sloppak"}).json()["error"] == "not found"
    assert c.post("/api/plugins/song_cents/measured", json={"file": "../x.sloppak", "cents": 3}).json() == \
        {"error": "not found"}


def test_find_psarc_rejects_traversal(mod, tmp_path):
    (tmp_path / "a.psarc").write_bytes(b"")
    assert mod.find_psarc("a.psarc", [tmp_path]) == tmp_path / "a.psarc"
    assert mod.find_psarc("../a.psarc", [tmp_path / "sub"]) is None
    assert mod.find_psarc("", [tmp_path]) is None
