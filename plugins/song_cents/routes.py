"""Song Cents: how far each song's recording sits off its chart tuning, in cents.

feedBack 0.3 port of the Slopsmith 0.2.9 user plugin (mattromano custom build). Resolution order:
  1. cache (song_cents.json in the config dir), keyed by library filename and invalidated by
     size/mtime
  2. the chart itself: a non-zero arrangement ``centOffset`` (manifest entry or arrangement JSON)
  3. for a sloppak converted from a PSARC (manifest ``source.kind == psarc``), the source PSARC's
     manifest CentOffset (the conversion dropped it to 0), looked up in the library folder and
     the Rocksmith 2014 folders Steam has (lib/rspsarc.py reads it)
  4. otherwise the browser measures it from the song's stem and POSTs it back (source "audio")

Offsets are the residual after whole semitones (the chart tuning already carries those):
-50 < cents <= 50. Reads the pack's manifest directly (``source`` is an extension key written by
the 0.2.9 converter); writes nothing into any pack.
"""

import json
import threading
import zipfile
from pathlib import Path

PACK_SUFFIXES = (".sloppak", ".feedpak")
SKIP_ARRS = ("Vocals", "ShowLights", "JVocals")


def residual(cents):
    try:
        c = float(cents or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(c - 100 * round(c / 100), 1)


def psarc_cent_offset(psarc_path) -> float:
    """First non-zero CentOffset of a playable arrangement in a PSARC (else 0)."""
    import rspsarc
    for a in rspsarc.psarc_arrangements(psarc_path):
        if a["has_tuning"] and a["cent_offset"]:
            return float(a["cent_offset"])
    return 0.0


class _Pack:
    """Read files from a sloppak (zip or directory)."""

    def __init__(self, path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def names(self):
        if self.zip:
            return set(self.zip.namelist())
        return {p.relative_to(self.path).as_posix() for p in self.path.rglob("*") if p.is_file()}

    def read(self, rel):
        if self.zip:
            return self.zip.read(rel)
        return (self.path / rel).read_bytes()

    def close(self):
        if self.zip:
            self.zip.close()


def pack_info(path):
    """(chart_cent_offset, source_psarc_name, stem_rel_path) for a sloppak."""
    import yaml

    import rspsarc
    sp = _Pack(path)
    try:
        names = sp.names()
        meta = (yaml.safe_load(sp.read("manifest.yaml")) if "manifest.yaml" in names else {}) or {}
        chart_cents = 0.0
        for entry in meta.get("arrangements") or []:
            if not isinstance(entry, dict) or entry.get("name") in SKIP_ARRS:
                continue
            for key in ("centOffset", "cent_offset"):
                if entry.get(key):
                    chart_cents = float(entry[key])
            rel = entry.get("file")
            if not chart_cents and rel and rel in names:
                j = rspsarc.loads_lenient(sp.read(rel))
                if isinstance(j, dict) and j.get("centOffset"):
                    chart_cents = float(j["centOffset"])
            if chart_cents:
                break
        source = meta.get("source") or {}
        src_psarc = source.get("file") if isinstance(source, dict) and source.get("kind") == "psarc" else None
        stems = {s.get("id"): s.get("file") for s in (meta.get("stems") or []) if isinstance(s, dict)}
        stem = None
        for sid in ("guitar", "full"):
            if stems.get(sid) in names:
                stem = stems[sid]
                break
        if stem is None:
            stem = next((n for n in sorted(names) if n.endswith(".ogg")), None)
        return chart_cents, src_psarc, stem
    finally:
        sp.close()


def find_psarc(name: str, roots) -> Path | None:
    """The source PSARC named in a converted pack, under the library or a Rocksmith folder."""
    if not name:
        return None
    rel = Path(str(name).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    for root in roots:
        root = Path(root)
        for cand in (root / rel, root / "dlc" / rel.name, root / rel.name):
            if cand.is_file():
                return cand
    return None


def setup(app, context):
    from fastapi import Body

    import rspsarc

    lock = threading.Lock()
    cache_path = Path(context["config_dir"]) / "song_cents.json"
    log = context.get("log")

    def load():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    cache = load()

    def save():
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(cache_path)

    def stamp(p):
        st = p.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def library():
        dlc = context["get_dlc_dir"]()
        return Path(dlc) if dlc else None

    def song_path(filename):
        root = library()
        if not root:
            return None
        p = (root / filename).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            return None
        return p if p.exists() and p.name.lower().endswith(PACK_SUFFIXES) else None

    _rs_dirs = []

    def psarc_roots():
        if not _rs_dirs:
            try:
                _rs_dirs.extend(rspsarc.rocksmith_dirs())
            except Exception:
                pass
        return [library(), *_rs_dirs]

    def resolve(filename):
        p = song_path(filename)
        if p is None:
            return {"file": filename, "error": "not found"}
        st = stamp(p)
        with lock:
            hit = cache.get(filename)
        if hit and hit.get("stamp") == st:
            return dict(hit, file=filename)
        rec = None
        try:
            chart_cents, src_psarc, stem = pack_info(p)
            if residual(chart_cents):
                rec = {"cents": residual(chart_cents), "source": "chart"}
            else:
                src = find_psarc(src_psarc, [r for r in psarc_roots() if r])
                if src is not None:
                    c = residual(psarc_cent_offset(src))
                    if c:
                        rec = {"cents": c, "source": "psarc"}
            if rec is None:
                # Unknown: the browser measures it. Not cached until it reports back.
                return {"file": filename, "cents": None, "source": "unmeasured", "stem": stem}
        except Exception as e:  # unreadable archive etc.
            if log:
                log.warning("song_cents: %s: %s", filename, e)
            return {"file": filename, "cents": None, "source": "error"}
        rec["stamp"] = st
        with lock:
            cache[filename] = rec
            save()
        return dict(rec, file=filename)

    @app.get("/api/plugins/song_cents/offset")
    def get_offset(file: str):
        return resolve(file)

    @app.post("/api/plugins/song_cents/measured")
    def post_measured(payload: dict = Body(...)):
        filename = str(payload.get("file") or "")
        p = song_path(filename) if filename else None
        if p is None:
            return {"error": "not found"}
        cents = payload.get("cents")
        try:
            rec = {
                "cents": None if cents is None else round(float(cents), 1),
                "confidence": round(float(payload.get("confidence") or 0.0), 2),
                "raw": None if payload.get("raw") is None else round(float(payload["raw"]), 1),
                "source": "audio",
                "stamp": stamp(p),
            }
        except (TypeError, ValueError):
            return {"error": "bad measurement"}
        with lock:
            cache[filename] = rec
            save()
        return dict(rec, file=filename)

    @app.get("/api/plugins/song_cents/pending")
    def get_pending():
        """Library songs with no cached offset (or changed since): the scan resolves these."""
        root = library()
        if not root or not root.is_dir():
            return {"files": []}
        out = []
        for suffix in PACK_SUFFIXES:
            for p in root.rglob("*" + suffix):
                rel = p.relative_to(root).as_posix()
                if any(part.lower().endswith(PACK_SUFFIXES) for part in Path(rel).parts[:-1]):
                    continue        # a file inside a directory-form pack
                with lock:
                    hit = cache.get(rel)
                try:
                    if hit and hit.get("stamp") == stamp(p):
                        continue
                except OSError:
                    continue
                out.append(rel)
        return {"files": sorted(out)}

    @app.get("/api/plugins/song_cents/known")
    def get_known():
        with lock:
            return {k: {"cents": v.get("cents"), "source": v.get("source")} for k, v in cache.items()}
