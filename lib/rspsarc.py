"""Rocksmith 2014 files, read-only: PSARC entries and the Steam profile (mattromano custom build).

feedBack 0.3 has no PSARC support and does not ship pycryptodome, but two personal plugins
still need a little of Rocksmith's own data:

- ``rs_profile_counts`` reads the play counts in the Rocksmith 2014 Steam profile
  (``<steam>/userdata/<id>/221680/remote/*_prfldb``) and the arrangement ids in the DLC PSARCs'
  manifests, to feed the native play count.
- ``song_cents`` reads a source PSARC's ``CentOffset`` (the PSARC->sloppak conversion dropped it).

Both files are AES-encrypted with keys that are public knowledge (every Rocksmith tool ships
them; the 0.2.9 app's psarc reader and play_counts plugin carried the same two). This module
carries a small pure-Python AES-256 (ECB decrypt for the profile, CFB-128 for the PSARC table of
contents) and uses pycryptodome instead when it happens to be installed. Nothing here writes to
a PSARC or a profile.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import struct
import zlib
from pathlib import Path

log = logging.getLogger("feedBack.lib.rspsarc")

# PSARC table-of-contents key / IV, and the profile key (all public, see the docstring).
PSARC_KEY = bytes.fromhex("C53DB23870A1A2F71CAE64061FDD0E1157309DC85204D4C5BFDF25090DF2572C")
PSARC_IV = bytes.fromhex("E915AA018FEF71FC508132E4BB4CEB42")
PROFILE_KEY = bytes.fromhex("728B369E24ED0134768511021812AFC0A3C25D02065F166B4BCC58CD2644F29E")
RS2014_APP_ID = "221680"


# ── AES-256 (FIPS-197), table driven ────────────────────────────────────────

def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _build_tables():
    sbox = [0] * 256
    p = q = 1
    while True:                       # multiplicative inverse via the 3 / 0xF6 generator walk
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) \
            ^ ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF)
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    te = [[0] * 256 for _ in range(4)]
    td = [[0] * 256 for _ in range(4)]
    for i in range(256):
        s = sbox[i]
        w = (_mul(s, 2) << 24) | (s << 16) | (s << 8) | _mul(s, 3)
        v = inv[i]
        u = (_mul(v, 14) << 24) | (_mul(v, 9) << 16) | (_mul(v, 13) << 8) | _mul(v, 11)
        for k in range(4):
            te[k][i] = ((w >> (8 * k)) | (w << (32 - 8 * k))) & 0xFFFFFFFF
            td[k][i] = ((u >> (8 * k)) | (u << (32 - 8 * k))) & 0xFFFFFFFF
    return sbox, inv, te, td


_SBOX, _INV, _TE, _TD = _build_tables()


class _AES:
    """AES with a 128/192/256-bit key: encrypt_block / decrypt_block on 16-byte blocks."""

    def __init__(self, key: bytes):
        nk = len(key) // 4
        if len(key) not in (16, 24, 32):
            raise ValueError("AES key must be 16, 24 or 32 bytes")
        self.nr = nk + 6
        w = list(struct.unpack(">%dI" % nk, key))
        rcon = 1
        for i in range(nk, 4 * (self.nr + 1)):
            t = w[i - 1]
            if i % nk == 0:
                t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
                t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16) \
                    | (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF]
                t ^= rcon << 24
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 0xFF] << 16) \
                    | (_SBOX[(t >> 8) & 0xFF] << 8) | _SBOX[t & 0xFF]
            w.append(w[i - nk] ^ t)
        self.ek = w
        # Equivalent inverse cipher: round keys reversed, InvMixColumns on the middle ones.
        dk = []
        for r in range(self.nr, -1, -1):
            rk = w[4 * r:4 * r + 4]
            if 0 < r < self.nr:
                rk = [_TD[0][_SBOX[x >> 24]] ^ _TD[1][_SBOX[(x >> 16) & 0xFF]]
                      ^ _TD[2][_SBOX[(x >> 8) & 0xFF]] ^ _TD[3][_SBOX[x & 0xFF]] for x in rk]
            dk.extend(rk)
        self.dk = dk

    def encrypt_block(self, block: bytes) -> bytes:
        te0, te1, te2, te3 = _TE
        k = self.ek
        s0, s1, s2, s3 = struct.unpack(">4I", block)
        s0 ^= k[0]
        s1 ^= k[1]
        s2 ^= k[2]
        s3 ^= k[3]
        for r in range(1, self.nr):
            o = 4 * r
            t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 0xFF] ^ te2[(s2 >> 8) & 0xFF] ^ te3[s3 & 0xFF] ^ k[o]
            t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 0xFF] ^ te2[(s3 >> 8) & 0xFF] ^ te3[s0 & 0xFF] ^ k[o + 1]
            t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 0xFF] ^ te2[(s0 >> 8) & 0xFF] ^ te3[s1 & 0xFF] ^ k[o + 2]
            t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 0xFF] ^ te2[(s1 >> 8) & 0xFF] ^ te3[s2 & 0xFF] ^ k[o + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        sb = _SBOX
        o = 4 * self.nr
        out = (
            ((sb[s0 >> 24] << 24) | (sb[(s1 >> 16) & 0xFF] << 16) | (sb[(s2 >> 8) & 0xFF] << 8) | sb[s3 & 0xFF]) ^ k[o],
            ((sb[s1 >> 24] << 24) | (sb[(s2 >> 16) & 0xFF] << 16) | (sb[(s3 >> 8) & 0xFF] << 8) | sb[s0 & 0xFF]) ^ k[o + 1],
            ((sb[s2 >> 24] << 24) | (sb[(s3 >> 16) & 0xFF] << 16) | (sb[(s0 >> 8) & 0xFF] << 8) | sb[s1 & 0xFF]) ^ k[o + 2],
            ((sb[s3 >> 24] << 24) | (sb[(s0 >> 16) & 0xFF] << 16) | (sb[(s1 >> 8) & 0xFF] << 8) | sb[s2 & 0xFF]) ^ k[o + 3],
        )
        return struct.pack(">4I", *out)

    def decrypt_block(self, block: bytes) -> bytes:
        td0, td1, td2, td3 = _TD
        k = self.dk
        s0, s1, s2, s3 = struct.unpack(">4I", block)
        s0 ^= k[0]
        s1 ^= k[1]
        s2 ^= k[2]
        s3 ^= k[3]
        for r in range(1, self.nr):
            o = 4 * r
            t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 0xFF] ^ td2[(s2 >> 8) & 0xFF] ^ td3[s1 & 0xFF] ^ k[o]
            t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 0xFF] ^ td2[(s3 >> 8) & 0xFF] ^ td3[s2 & 0xFF] ^ k[o + 1]
            t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 0xFF] ^ td2[(s0 >> 8) & 0xFF] ^ td3[s3 & 0xFF] ^ k[o + 2]
            t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 0xFF] ^ td2[(s1 >> 8) & 0xFF] ^ td3[s0 & 0xFF] ^ k[o + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        ib = _INV
        o = 4 * self.nr
        out = (
            ((ib[s0 >> 24] << 24) | (ib[(s3 >> 16) & 0xFF] << 16) | (ib[(s2 >> 8) & 0xFF] << 8) | ib[s1 & 0xFF]) ^ k[o],
            ((ib[s1 >> 24] << 24) | (ib[(s0 >> 16) & 0xFF] << 16) | (ib[(s3 >> 8) & 0xFF] << 8) | ib[s2 & 0xFF]) ^ k[o + 1],
            ((ib[s2 >> 24] << 24) | (ib[(s1 >> 16) & 0xFF] << 16) | (ib[(s0 >> 8) & 0xFF] << 8) | ib[s3 & 0xFF]) ^ k[o + 2],
            ((ib[s3 >> 24] << 24) | (ib[(s2 >> 16) & 0xFF] << 16) | (ib[(s1 >> 8) & 0xFF] << 8) | ib[s0 & 0xFF]) ^ k[o + 3],
        )
        return struct.pack(">4I", *out)


def _crypto():
    try:
        from Crypto.Cipher import AES   # pycryptodome, when present: much faster
        return AES
    except ImportError:
        return None


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """ECB decrypt; a trailing partial block is dropped."""
    data = data[:len(data) - len(data) % 16]
    lib = _crypto()
    if lib is not None:
        return lib.new(key, lib.MODE_ECB).decrypt(data)
    a = _AES(key)
    return b"".join(a.decrypt_block(data[i:i + 16]) for i in range(0, len(data), 16))


def aes_cfb128_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """CFB with 128-bit segments; any length (the last segment may be partial)."""
    lib = _crypto()
    if lib is not None and len(data) % 16 == 0:
        return lib.new(key, lib.MODE_CFB, iv=iv, segment_size=128).decrypt(data)
    a = _AES(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        c = data[i:i + 16]
        ks = a.encrypt_block(prev)
        out += bytes(x ^ y for x, y in zip(c, ks))
        prev = c
    return bytes(out)


# ── PSARC ──────────────────────────────────────────────────────────────────

def _extract_entry(f, entry: dict, block_sizes: list, block_size: int) -> bytes:
    f.seek(entry["offset"])
    if entry["length"] == 0:
        return b""
    num_blocks = (entry["length"] + block_size - 1) // block_size
    out = bytearray()
    for i in range(num_blocks):
        bi = entry["z_index"] + i
        csize = block_sizes[bi] if bi < len(block_sizes) else 0
        if csize == 0:
            out += f.read(min(block_size, entry["length"] - len(out)))
        else:
            blk = f.read(csize)
            try:
                out += zlib.decompress(blk)
            except zlib.error:
                out += blk
    return bytes(out[:entry["length"]])


def _parse_toc(f):
    if f.read(4) != b"PSAR":
        raise ValueError("not a PSARC file")
    _version, _compression, toc_length, entry_size, n_entries, block_size, flags = \
        struct.unpack(">I4sIIIII", f.read(28))
    region = f.read(toc_length - 32)
    if flags == 4:
        region = aes_cfb128_decrypt(PSARC_KEY, PSARC_IV, region)
    toc = region[:entry_size * n_entries]
    bt = region[entry_size * n_entries:]
    entries = []
    for i in range(n_entries):
        ed = toc[i * entry_size:(i + 1) * entry_size]
        entries.append({"z_index": struct.unpack(">I", ed[16:20])[0],
                        "length": int.from_bytes(ed[20:25], "big"),
                        "offset": int.from_bytes(ed[25:30], "big")})
    width = 2 if block_size <= 65536 else 3 if block_size <= 16777216 else 4
    block_sizes = [int.from_bytes(bt[i:i + width], "big") for i in range(0, len(bt) - width + 1, width)]
    names = _extract_entry(f, entries[0], block_sizes, block_size).decode("utf-8", errors="ignore")
    return entries, names.replace("\r\n", "\n").strip().split("\n"), block_sizes, block_size


def read_psarc_entries(path, patterns=None) -> dict:
    """{internal path: bytes} for the entries matching any glob in ``patterns`` (all if None)."""
    out = {}
    with open(path, "rb") as f:
        entries, names, block_sizes, block_size = _parse_toc(f)
        for entry, name in zip(entries[1:], names):
            name = name.strip()
            if not name:
                continue
            if patterns is not None and not any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns):
                continue
            try:
                out[name] = _extract_entry(f, entry, block_sizes, block_size)
            except Exception as e:      # one bad entry shouldn't lose the rest
                log.warning("psarc %s: entry %r unreadable: %s", Path(path).name, name, e)
    return out


def loads_lenient(data):
    """JSON from a PSARC manifest; tolerates the trailing commas some customs carry."""
    import re
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", text))
        except ValueError:
            return None


def psarc_arrangements(path) -> list[dict]:
    """Playable arrangements in a PSARC's manifests:
    [{persistent_id, name, song, artist, cent_offset}] (Vocals / ShowLights skipped)."""
    out, seen = [], set()
    for _name, blob in sorted(read_psarc_entries(path, ["*.json"]).items()):
        j = loads_lenient(blob)
        if not isinstance(j, dict):
            continue
        for v in (j.get("Entries") or {}).values():
            a = (v or {}).get("Attributes") or {}
            pid = str(a.get("PersistentID") or "").upper()
            name = a.get("ArrangementName") or ""
            if not pid or pid in seen or name in ("Vocals", "JVocals", "ShowLights"):
                continue
            seen.add(pid)
            try:
                cents = float(a.get("CentOffset") or 0.0)
            except (TypeError, ValueError):
                cents = 0.0
            out.append({"persistent_id": pid, "name": name, "song": a.get("SongName") or "",
                        "artist": a.get("ArtistName") or "", "cent_offset": cents,
                        "has_tuning": "Tuning" in a})
    return out


# ── Steam / Rocksmith locations ─────────────────────────────────────────────

def steam_roots() -> list[Path]:
    """Likely Steam install folders on this machine (existing ones only)."""
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            cands.append(Path(os.environ[env]) / "Steam")
    cands += [Path(r"C:\Program Files (x86)\Steam"),
              Path.home() / "Library" / "Application Support" / "Steam",   # macOS
              Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]
    out = []
    for c in cands:
        if c.is_dir() and c not in out:
            out.append(c)
    return out


def rocksmith_dirs() -> list[Path]:
    """Rocksmith 2014 install folders (songs.psarc + dlc/) under the known Steam roots,
    including extra Steam libraries listed in steamapps/libraryfolders.vdf."""
    import re
    libs = []
    for root in steam_roots():
        libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    out = []
    for lib in libs:
        d = lib / "steamapps" / "common" / "Rocksmith2014"
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def find_profiles() -> list[Path]:
    """Rocksmith 2014 profile files (``*_prfldb``) under every Steam user, newest first."""
    files = []
    for root in steam_roots():
        ud = root / "userdata"
        if ud.is_dir():
            files += [p for p in ud.glob(f"*/{RS2014_APP_ID}/remote/*") if p.name.lower().endswith("_prfldb")]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def read_profile(path) -> dict:
    """The decrypted profile JSON. Raises ValueError for a file that isn't a profile."""
    data = Path(path).read_bytes()
    if data[:4] != b"EVAS":
        raise ValueError("not a Rocksmith 2014 profile")
    raw = aes_ecb_decrypt(PROFILE_KEY, data[20:])
    try:
        text = zlib.decompressobj().decompress(raw)
    except zlib.error as e:
        raise ValueError(f"profile does not decrypt: {e}") from None
    return json.loads(text.rstrip(b"\0").decode("utf-8", errors="replace"))


def profile_song_stats(path) -> dict:
    """{PERSISTENT ID: stats} from a profile's Stats.Songs (PlayedCount, MasteryPeak, DateLAS…)."""
    songs = (read_profile(path).get("Stats") or {}).get("Songs") or {}
    return {str(pid).upper(): v for pid, v in songs.items() if isinstance(v, dict)}
