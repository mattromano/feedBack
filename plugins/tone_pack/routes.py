"""Tone pack: ready-made Audio-plugin presets for every Tone Automation category.

feedBack 0.3 port of the Slopsmith 0.2.9 user plugin.

GET /api/plugins/tone_pack/presets
    {version, presets: {name: preset}, targets: {category: name}, missing: [...], kilohearts: bool}
    Each preset is in the Audio plugin's `slopsmith-chain-presets` shape: `nativePreset` (the desktop
    engine's chain JSON: NAM / IR / VST stages by file path, no saved knob state), `items`,
    `inputGain` / `outputGain` (linear), `noiseGate`, `tonePolish`, plus `generatedBy: "tone_pack"`.
    Chains are built only from captures / IRs that exist on this computer (Rig Builder's
    nam_models / nam_irs folders), so a missing file drops that stage or that preset.

The "pack" cab IRs (EQ'd toward Main Lead by tools/build_irs.py) are NOT shipped in this
repo: they are derived from the Rocksmith cab IRs on this computer. build_irs.py writes them to
<config>/nam_irs/tone_pack/<category>.wav; without them each recipe falls back to the plain
Rocksmith cab IR in <config>/nam_irs/rocksmith (and the computed, not measured, level).

screen.js merges them into localStorage (never touching presets the user made) and fills empty
Tone Automation targets.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

VERSION = 3
# Main Lead (an amp+cab capture, no IR stage) sits at about -19.3 dB NAM loudness; every preset is
# matched to that.
TARGET_LOUDNESS_DB = -19.3
# The engine loads IRs with JUCE Convolution Normalise::yes (energy scaled to 0.125), which costs a
# guitar-band signal ~16-22 dB depending on the cab. ir_gains.json has that loss per IR file
# (measured with band-limited pink noise, tools in README); other IRs use the median.
IR_GAIN_DEFAULT_DB = -18.1
VST3_DIRS = [
    Path(os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files")) / "VST3",   # Windows
    Path("/Library/Audio/Plug-Ins/VST3"),                                                    # macOS
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3",
]

# Stage kinds -> engine slot type (ProcessorSlot::Type: VST 0, NAM 1, IR 2)
_TYPES = {"VST": 0, "NAM": 1, "IR": 2}

# Category -> preset recipe. Stages are tried in order; `alt` lists fallbacks for a stage.
# The amp capture's loudness (NAM metadata) sets the output gain.
RECIPES = [
    {"category": "clean", "name": "Auto · Clean (Twin Reverb)", "stages": [
        ("NAM", "amps/Tim R Fender Twin Reverb Ch1 BR G06.nam", ["amps/Tim R JC 120 CH 2 Hi.nam",
                                                                 "amps/1 Orange AD30 Ch2 Cleanest.nam"]),
        ("IR", "pack:clean.wav", ["rocksmith/cab_tw112c_03.wav", "rocksmith/cab_ca112c_03.wav"]),
    ]},
    {"category": "od", "name": "Auto · Crunch (Marshall DSL40)", "stages": [
        ("NAM", "amps/Marshall DSL 40 C Crunch.nam", ["amps/Fender_57CustomDeluxe_crunch_in2_1000epochs.nam",
                                                      "other/RC JCM900 ChA.nam"]),
        ("IR", "pack:od.wav", ["rocksmith/cab_marshall1960a_03.wav"]),
    ]},
    {"category": "dist", "name": "Auto · Distortion (5153)", "stages": [
        ("NAM", "other/5153.nam", ["other/RC JCM900 ChB.nam", "amps/OB1 JCM 900 Ch. A - 272 higher gain.nam"]),
        ("IR", "pack:dist.wav", ["rocksmith/cab_marshall1960ax_04.wav"]),
    ]},
    {"category": "solo", "name": "Auto · Lead (Mesa Mark V)", "stages": [
        ("NAM", "amps/Mesa Boogie Mark V Lead V2.2 Amp Only.nam", ["amps/Hiwatt DR 103 Crancked.nam"]),
        ("IR", "pack:solo.wav", ["rocksmith/cab_marshall1960a_03.wav"]),
    ]},
    {"category": "bass", "name": "Auto · Bass (Ampeg SVT)", "stages": [
        ("NAM", "amps/SVT CLEAN.nam", ["amps/Standard SWR WorkingPro 700 G05.nam",
                                       "amps/Fender Bassman 100T Vintage Channel Vol 5 Flat EQ Master Vol 10.nam"]),
        ("IR", "pack:bass.wav", ["rocksmith/bass_cab_at810bc_03.wav"]),
    ]},
    {"category": "acoustic", "name": "Auto · Acoustic (emulator)", "stages": [
        ("NAM", "pedals/tone3000_29201_m125801_Pedal_AcousticEmulator.nam", []),
    ]},
    {"category": "mod", "name": "Auto · Clean Chorus (Twin)", "stages": [
        ("NAM", "pedals/tone3000_26327_m99790_Pedal_Chorus.nam", []),
        ("NAM", "amps/Tim R Fender Twin Reverb Ch1 BR G06.nam", ["amps/Tim R JC 120 CH 2 Hi.nam"]),
        ("IR", "pack:mod.wav", ["rocksmith/cab_tw112c_03.wav"]),
    ]},
]

# Kilohearts Essentials (free) stand in for the static chorus capture when installed.
KILOHEARTS_MOD = ["kHs Chorus", "kHs Delay", "kHs Reverb"]


def _ir_gains() -> dict:
    try:
        with open(Path(__file__).with_name("ir_gains.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _find_vst3(name: str) -> Path | None:
    for root in VST3_DIRS:
        if not root.is_dir():
            continue
        for p in root.rglob(f"{name}.vst3"):
            return p
    return None


def _loudness(path: Path) -> float | None:
    try:
        with open(path, encoding="utf-8") as f:
            md = (json.load(f).get("metadata") or {})
        v = md.get("loudness")
        return float(v) if v is not None else None
    except (OSError, ValueError, TypeError):
        return None


PLUGIN_DIR = Path(__file__).resolve().parent
PACK_IR_DIR = ("nam_irs", "tone_pack")   # under config_dir


def _resolve(models: Path, irs: Path, kind: str, rel: str, alts) -> Path | None:
    base = models if kind == "NAM" else irs
    for r in [rel, *alts]:
        # "pack:" = the pack's own IRs (tools/build_irs.py: cab + EQ toward Main Lead), in nam_irs/tone_pack
        p = irs / PACK_IR_DIR[1] / r[5:] if r.startswith("pack:") else base / r
        if p.is_file():
            return p
    return None


def _levels() -> dict:
    """tools/build_irs.py + body_levels.py: measured make-up per category for the default chain."""
    try:
        with open(PLUGIN_DIR / "levels.json", encoding="utf-8") as f:
            return json.load(f).get("categories") or {}
    except (OSError, ValueError):
        return {}


def _stage(kind: str, path: Path, sid: int) -> dict:
    label = {"NAM": "NAM", "IR": "IR", "VST": "VST"}[kind]
    return {"id": sid, "type": _TYPES[kind], "name": f"{label}: {path.stem}", "path": str(path), "bypassed": False}


_JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def juce_base64(data: bytes) -> str:
    """juce::MemoryBlock::toBase64Encoding: "<size>." + 6-bit groups taken little-endian from the
    data (not RFC 4648). The engine's loadPreset reads slot state with fromBase64Encoding."""
    n = int.from_bytes(data, "little")
    chars = ((len(data) * 8) + 5) // 6
    return f"{len(data)}." + "".join(_JUCE_B64[(n >> (6 * i)) & 63] for i in range(chars))


def _nam_state(path: Path, output_level: float) -> str:
    """NAMProcessor state: the level-matching gain goes on the amp's own output level, since the
    preset output slider tops out at +12 dB."""
    js = json.dumps({"modelPath": str(path), "inputLevel": 1.0, "outputLevel": round(output_level, 4)}, indent=2)
    return juce_base64(js.encode("utf-8"))


def build_presets(config_dir: Path) -> dict:
    models, irs = config_dir / "nam_models", config_dir / "nam_irs"
    presets, targets, missing = {}, {}, []
    kh = [_find_vst3(n) for n in KILOHEARTS_MOD]
    ir_gain = _ir_gains()
    measured = _levels()
    have_kh = all(kh)
    for rec in RECIPES:
        chain, level_db, amp_stage, ir_db, defaults = [], None, None, 0.0, True
        stages = rec["stages"]
        if rec["category"] == "mod" and have_kh:
            # real modulation: clean amp + cab, then chorus / delay / reverb VSTs
            stages = [s for s in stages if "Chorus" not in s[1]]
        for kind, rel, alts in stages:
            p = _resolve(models, irs, kind, rel, alts)
            if p is None:
                missing.append(f"{rec['name']}: {rel}")
                defaults = False
                continue
            if not (p == irs / PACK_IR_DIR[1] / rel[5:] if rel.startswith("pack:") else p.name == Path(rel).name):
                defaults = False      # a fallback file: the measured level doesn't apply
            chain.append(_stage(kind, p, len(chain) + 1))
            if kind == "IR":
                ir_db += float(ir_gain.get(p.name, IR_GAIN_DEFAULT_DB))
            # output level follows the amp capture (else the first capture in the chain)
            if kind == "NAM" and (level_db is None or p.parent.name in ("amps", "other")):
                lv = _loudness(p)
                if lv is not None and (level_db is None or p.parent.name in ("amps", "other")):
                    level_db, amp_stage = lv, chain[-1]
        if not any(s["type"] == _TYPES["NAM"] for s in chain):
            chain = None
        if not chain:
            continue
        name = rec["name"]
        if rec["category"] == "mod" and have_kh:
            name = "Auto · Clean Chorus + Delay (Kilohearts)"
            for p in kh:
                chain.append(_stage("VST", p, len(chain) + 1))
        # chain loudness ~ amp capture loudness + the IRs' normalized gain; make up the rest on the amp
        m = measured.get(rec["category"]) if defaults else None
        if m and m.get("makeup_db") is not None:
            make_up_db = float(m["makeup_db"])                # rendered + measured against Main Lead
        else:
            make_up_db = 0.0 if level_db is None else TARGET_LOUDNESS_DB - (level_db + ir_db)
        make_up_db = max(-24.0, min(30.0, make_up_db))
        if amp_stage is None:       # e.g. acoustic: the only capture carries the gain
            amp_stage = next((s for s in chain if s["type"] == _TYPES["NAM"]), None)
        if amp_stage is not None:
            amp_stage["state"] = _nam_state(Path(amp_stage["path"]), 10 ** (make_up_db / 20.0))
        native = {"version": 1, "chain": chain}
        presets[name] = {
            "nativePreset": json.dumps(native, indent=2),
            "items": [{"type": k, "path": s["path"], "name": s["name"]}
                      for s in chain for k in [next(t for t, v in _TYPES.items() if v == s["type"])]],
            "inputGain": 1.0,
            "outputGain": 1.0,
            "makeUpDb": round(make_up_db, 1),
            # same gate + Tone Polish as Main Lead
            "noiseGate": {"enabled": rec["category"] != "bass", "thresholdDb": -60, "releaseMs": 100, "depthDb": -60},
            "tonePolish": {"enabled": True},
            "generatedBy": "tone_pack",
            # what the pack set, so a refresh can tell the user's own tweaks apart
            "packLevels": {"inputGain": 1.0, "outputGain": 1.0,
                           "noiseGate": {"enabled": rec["category"] != "bass", "thresholdDb": -60,
                                         "releaseMs": 100, "depthDb": -60},
                           "tonePolish": {"enabled": True}},
            "category": rec["category"],
            "toneFormVersion": VERSION,
            "created": 0,
        }
        targets[rec["category"]] = name
    return {"version": VERSION, "presets": presets, "targets": targets, "missing": missing, "kilohearts": have_kh}


def song_tone_categories(song: Path, arrangement: str, classify) -> dict:
    """{tone name: category} for the tones an arrangement uses, from each tone's gear
    (gear_class.classify, passed in). Every tone of a Bass arrangement is 'bass'."""
    import re
    import zipfile

    import yaml

    def read(rel):
        if song.is_dir():
            return (song / rel).read_bytes()
        with zipfile.ZipFile(song) as z:
            return z.read(rel)

    man = yaml.safe_load(read("manifest.yaml")) or {}
    out = {}
    for a in man.get("arrangements", []):
        if (a.get("name") or "") != arrangement or not a.get("file"):
            continue
        try:
            t = (json.loads(read(a["file"])) or {}).get("tones") or {}
        except (KeyError, ValueError, OSError):
            continue
        defs = {}
        for d in t.get("definitions") or []:
            if isinstance(d, dict):
                for k in (d.get("Key"), d.get("Name")):
                    if k:
                        defs[str(k).lower()] = d          # names differ in case from the definitions
        names = [t.get("base")] + [c.get("name") for c in (t.get("changes") or []) if isinstance(c, dict)]
        is_bass = bool(re.search(r"\bbass\b", arrangement, re.I))
        if not any(names) and defs:
            # single-tone arrangement (no base / changes): the player's Tone Automation classifies
            # the song file name instead, so report that tone under "$song"
            d = next(iter(defs.values()))
            cat = classify(d, d.get("Name") or "", is_bass)
            if cat:
                out.setdefault("$song", cat)
        for n in names:
            if n and n not in out:
                cat = classify(defs.get(n.lower()), n, is_bass)
                if cat:
                    out[n] = cat
    return out


def setup(app, context):
    config_dir = Path(context["config_dir"])
    get_dlc_dir = context.get("get_dlc_dir")
    classify = context["load_sibling"]("gear_class").classify

    @app.get("/api/plugins/tone_pack/presets")
    def tone_pack_presets():
        return build_presets(config_dir)

    @app.get("/api/plugins/tone_pack/song_tones")
    def tone_pack_song_tones(filename: str, arrangement: str):
        dlc = Path(get_dlc_dir()) if callable(get_dlc_dir) else None
        if dlc is None:
            return {"tones": {}}
        song = (dlc / filename).resolve()
        if dlc.resolve() not in song.parents or song.suffix.lower() != ".sloppak" or not song.exists():
            return {"tones": {}}          # outside the library / not a sloppak (PSARCs: names only)
        try:
            return {"tones": song_tone_categories(song, arrangement, classify)}
        except Exception as e:            # malformed song: no overrides
            return {"tones": {}, "error": str(e)}
