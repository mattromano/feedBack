"""tone_pack: presets built from the captures / IRs in the config dir, gear classes from a song."""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parents[3] / "plugins" / "tone_pack"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"tone_pack_{name}_under_test", PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def routes():
    return _load("routes")


@pytest.fixture(scope="module")
def gear():
    return _load("gear_class")


def _nam(path: Path, loudness):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"metadata": {"loudness": loudness}}))


def test_build_presets_uses_pack_irs_from_config(routes, tmp_path):
    _nam(tmp_path / "nam_models/amps/Marshall DSL 40 C Crunch.nam", -20.0)
    (tmp_path / "nam_irs/tone_pack").mkdir(parents=True)
    (tmp_path / "nam_irs/tone_pack/od.wav").write_bytes(b"RIFF")
    out = routes.build_presets(tmp_path)
    od = out["presets"]["Auto · Crunch (Marshall DSL40)"]
    chain = json.loads(od["nativePreset"])["chain"]
    assert [s["type"] for s in chain] == [1, 2]                       # NAM, IR
    assert chain[1]["path"] == str(tmp_path / "nam_irs/tone_pack/od.wav")
    assert od["makeUpDb"] == round(routes._levels()["od"]["makeup_db"], 1)   # measured level applies
    assert out["targets"]["od"] == "Auto · Crunch (Marshall DSL40)"
    assert any("Distortion" in m for m in out["missing"])             # no 5153 capture here: skipped, reported


def test_fallback_cab_uses_computed_level(routes, tmp_path):
    _nam(tmp_path / "nam_models/amps/Marshall DSL 40 C Crunch.nam", -20.0)
    (tmp_path / "nam_irs/rocksmith").mkdir(parents=True)
    (tmp_path / "nam_irs/rocksmith/cab_marshall1960a_03.wav").write_bytes(b"RIFF")
    od = routes.build_presets(tmp_path)["presets"]["Auto · Crunch (Marshall DSL40)"]
    ir_db = routes._ir_gains().get("cab_marshall1960a_03.wav", routes.IR_GAIN_DEFAULT_DB)
    want = routes.TARGET_LOUDNESS_DB - (-20.0 + ir_db)
    assert od["makeUpDb"] == round(want, 1)


def test_juce_base64_round_trip_shape(routes):
    enc = routes.juce_base64(b"hello")
    assert enc.startswith("5.")
    assert len(enc) == 2 + (5 * 8 + 5) // 6


def test_gear_class(gear):
    amp = lambda g: {"GearList": {"Amp": {"Key": "Amp_X", "KnobValues": {"Amp_Gain": g}}}}
    assert gear.classify(amp(80)) == "dist"
    assert gear.classify(amp(60)) == "od"
    assert gear.classify(amp(30)) == "clean"
    assert gear.classify(amp(30), "Lead tone") == "solo"
    assert gear.classify(amp(30), bass_arrangement=True) == "bass"
    assert gear.classify(None) is None


def test_song_tones_route(routes, gear, tmp_path):
    dlc = tmp_path / "dlc"
    dlc.mkdir()
    song = dlc / "a.sloppak"
    tones = {"base": "Tone 1", "changes": [{"name": "Tone 2", "t": 3}], "definitions": [
        {"Key": "Tone 1", "GearList": {"Amp": {"Key": "Amp_X", "KnobValues": {"Amp_Gain": 85}}}},
        {"Key": "Tone 2", "GearList": {"Amp": {"Key": "Amp_X", "KnobValues": {"Amp_Gain": 20}}}}]}
    with zipfile.ZipFile(song, "w") as z:
        z.writestr("manifest.yaml", "title: a\nartist: b\nduration: 1\nstems: []\narrangements:\n- id: lead\n  name: Lead\n  file: arrangements/lead.json\n")
        z.writestr("arrangements/lead.json", json.dumps({"notes": [], "tones": tones}))
    app = FastAPI()
    routes.setup(app, {"config_dir": str(tmp_path), "get_dlc_dir": lambda: dlc,
                       "load_sibling": lambda name: gear})
    c = TestClient(app)
    assert c.get("/api/plugins/tone_pack/song_tones", params={"filename": "a.sloppak", "arrangement": "Lead"}).json() == \
        {"tones": {"Tone 1": "dist", "Tone 2": "clean"}}
    assert c.get("/api/plugins/tone_pack/song_tones", params={"filename": "../x.sloppak", "arrangement": "Lead"}).json() == {"tones": {}}
    assert "presets" in c.get("/api/plugins/tone_pack/presets").json()
