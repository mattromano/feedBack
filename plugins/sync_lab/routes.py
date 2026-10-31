"""Sync Lab: builds the calibration song and keeps a log of calibration results.

The calibration song is an ordinary feedpak (sloppak), written into the
library as <dlc>/sloppak/_Sync_Calibration_v<N>.sloppak (or the DLC root when
there's no sloppak folder), so it goes through exactly the same clock,
highway, Note Detection and drum engine as any song, in single player and
split view. 100 BPM, one note per beat (open A string on Lead and Bass, snare
on Drums, an open strum on the 5-fret guitar part), three sections:

  listen  clicks + notes, screen blanked by screen.js: play by ear
  watch   notes, no clicks: play by eye
  play    clicks + notes: normal play, as a check

Ear vs eye timing gives the A/V offset; the eye timing gives each player's
input offset (see screen.js). Pure stdlib.
"""

import array
import io
import json
import math
import threading
import time
import wave
import zipfile
from pathlib import Path

from fastapi import Request

VERSION = 4
NAME = f"_Sync_Calibration_v{VERSION}.sloppak"
TITLE = "Sync Calibration"
ARTIST = "feedBack Sync Lab"
SR = 44100
BPM = 100
BEAT = 60.0 / BPM
T0 = 2.0
# Beat index ranges (inclusive start, exclusive end). Count-ins are clicks
# only (accented), the three test sections carry the notes.
PLAN = [
    ("count", 0, 4, True),
    ("listen", 4, 36, True),
    ("count", 36, 40, True),
    ("watch", 40, 72, False),
    ("count", 72, 76, True),
    ("play", 76, 100, True),
]
END_BEAT = 103
DURATION = round(T0 + END_BEAT * BEAT, 3)
ARRANGEMENTS = [("lead", "Lead"), ("bass", "Bass"), ("drums", "Drums")]
FIVE_FRET_ID = "ff_guitar"
FIVE_FRET_FILE = "five_fret_guitar.json"

MAX_LOG_BYTES = 2 * 1024 * 1024
_log_lock = threading.Lock()


def _bt(k):
    return round(T0 + k * BEAT, 4)


def plan(file=None):
    secs = {}
    for name, a, b, _clicks in PLAN:
        if name != "count":
            secs[name] = [_bt(a), _bt(b)]
    return {
        "version": VERSION,
        "title": TITLE,
        "artist": ARTIST,
        "bpm": BPM,
        "beat": BEAT,
        "t0": T0,
        "duration": DURATION,
        "sections": secs,
        # Screen blanked from the first count-in click to the end of listen.
        "blank": [_bt(0) - 0.3, _bt(36) - 0.15],
        "arrangements": [n for _i, n in ARRANGEMENTS],
        "five_fret_parts": [FIVE_FRET_ID],
        "beats_per_section": {name: b - a for name, a, b, _c in PLAN if name != "count"},
        "file": file or "sloppak/" + NAME,
    }


def click_track() -> bytes:
    n = int(DURATION * SR)
    buf = [0.0] * n
    length = int(0.035 * SR)
    for name, a, b, clicks in PLAN:
        if not clicks:
            continue
        accent = name == "count"
        freq = 1568.0 if accent else 1046.5
        amp = 0.75 if accent else 0.6
        # Woodblock-ish: fundamental + an inharmonic partial, sharp onset at the beat.
        shape = [amp * math.exp(-(i / SR) / 0.010)
                 * (math.sin(2 * math.pi * freq * i / SR) + 0.45 * math.sin(2 * math.pi * freq * 2.76 * i / SR)) / 1.45
                 for i in range(length)]
        for k in range(a, b):
            start = int(round(_bt(k) * SR))
            for i, v in enumerate(shape):
                if start + i >= n:
                    break
                buf[start + i] += v
    pcm = array.array("h", (max(-32767, min(32767, int(v * 32767))) for v in buf))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def note_times():
    out = []
    for name, a, b, _clicks in PLAN:
        if name != "count":
            out += [_bt(k) for k in range(a, b)]
    return out


def _note(t, s, f):
    return {"t": t, "s": s, "f": f, "sus": 0.0, "sl": -1, "slu": -1, "bn": 0, "ho": False, "po": False,
            "hm": False, "hp": False, "pm": False, "mt": False, "vb": False, "tr": False, "ac": False, "tp": False}


def _beats():
    return [{"time": _bt(k), "measure": (k // 4) if k % 4 == 0 else -1} for k in range(END_BEAT)]


def _sections():
    out, num = [], {}
    for name, a, _b, _clicks in PLAN:
        num[name] = num.get(name, 0) + 1
        out.append({"name": name, "number": num[name], "time": _bt(a)})
    return out


def _guitar_arr(name):
    return {
        "name": name, "tuning": [0] * 6, "capo": 0,
        "notes": [_note(t, 1, 0) for t in note_times()],   # open A string
        "chords": [], "anchors": [{"time": T0, "fret": 1, "width": 4}], "handshapes": [], "templates": [],
        "phrases": [], "beats": _beats(), "sections": _sections(),
    }


def _drums_arr():
    # A plain "Drums" arrangement whose notes carry GM drum MIDI (s * 24 + f),
    # the drums plugin's legacy chart shape. Deliberately not a feedpak
    # drum_tab: that is song-level (has_drum_tab), so the viz picker's Auto
    # would show the drum highway on Lead and Bass too.
    snare = 38   # GM acoustic snare
    return {
        "name": "Drums", "tuning": [0] * 6, "capo": 0,
        "notes": [_note(t, snare // 24, snare % 24) for t in note_times()],
        "chords": [], "anchors": [], "handshapes": [], "templates": [],
        "phrases": [], "beats": _beats(), "sections": _sections(),
    }


def _five_fret_part():
    # Open notes (f = 0): a pure strum on a plastic guitar, the most precise
    # input. Every level carries the same notes so any difficulty measures.
    notes = [{"t": t, "f": 0} for t in note_times()]
    return {
        "version": 1, "name": "Guitar (5-fret)", "instrument": "guitar",
        "source": {"kind": "chart", "origin": "local"},
        "notes": notes,
        "levels": {lv: {"notes": list(notes)} for lv in ("easy", "medium", "hard")},
    }


def _manifest():
    lines = [
        f"title: {TITLE}",
        f"artist: {ARTIST}",
        "album: Calibration",
        "year: 2026",
        f"duration: {DURATION}",
        "stems:",
        "- id: full",
        "  file: stems/click.wav",
        "  default: true",
        "arrangements:",
    ]
    for aid, name in ARRANGEMENTS:
        lines += [f"- id: {aid}", f"  name: {name}", f"  file: arrangements/{aid}.json",
                  "  tuning: [0, 0, 0, 0, 0, 0]", "  capo: 0"]
    lines += [f"- id: {FIVE_FRET_ID}", "  type: five_fret", "  name: Guitar (5-fret)",
              "  instrument: guitar", f"  five_fret: {FIVE_FRET_FILE}"]
    return "\n".join(lines) + "\n"


def build(dest: Path):
    tmp = dest.with_name(dest.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.yaml", _manifest())
        for aid, name in ARRANGEMENTS:
            arr = _drums_arr() if aid == "drums" else _guitar_arr(name)
            z.writestr(f"arrangements/{aid}.json", json.dumps(arr))
        z.writestr(FIVE_FRET_FILE, json.dumps(_five_fret_part()))
        z.writestr("stems/click.wav", click_track())
    tmp.replace(dest)


def song_folder(dlc_dir) -> Path | None:
    if not dlc_dir:
        return None
    root = Path(dlc_dir)
    if not root.is_dir():
        return None
    sub = root / "sloppak"
    return sub if sub.is_dir() else root


def ensure_song(dlc_dir, log=None):
    """Write the calibration sloppak into the library if this version is missing.
    Returns its library-relative filename (or None without a library)."""
    folder = song_folder(dlc_dir)
    if folder is None:
        return None
    dest = folder / NAME
    for old in folder.glob("_Sync_Calibration_v*.sloppak"):
        if old.name != NAME:
            try:
                old.unlink()
            except OSError:
                pass
    if not dest.exists():
        build(dest)
        if log:
            log.info("sync_lab: wrote %s", dest)
    return dest.relative_to(Path(dlc_dir)).as_posix()


def log_path(config_dir) -> Path:
    return Path(config_dir) / "sync_lab" / "sync_log.jsonl"


def setup(app, context):
    log = context.get("log")
    get_dlc = context.get("get_dlc_dir")
    path = log_path(context["config_dir"])

    def dlc():
        try:
            return get_dlc() if callable(get_dlc) else None
        except Exception:
            return None

    try:
        ensure_song(dlc(), log)
    except Exception as e:   # never block startup
        if log:
            log.warning("sync_lab: could not write the calibration song: %s", e)

    @app.get("/api/plugins/sync_lab/info")
    def info():
        try:
            rel = ensure_song(dlc(), log)
        except Exception as e:
            return {"ok": False, "error": str(e), "plan": plan()}
        return {"ok": bool(rel), "plan": plan(rel)}

    @app.post("/api/plugins/sync_lab/log")
    async def log_result(request: Request):
        try:
            entry = await request.json()
        except Exception:
            return {"ok": False}
        if not isinstance(entry, dict):
            return {"ok": False}
        entry["server_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_name("sync_log.old.jsonl"))
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return {"ok": True}

    @app.get("/api/plugins/sync_lab/log")
    def read_log(n: int = 20):
        if not path.exists():
            return {"entries": []}
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines()[-max(1, min(200, n)):]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return {"entries": out}
