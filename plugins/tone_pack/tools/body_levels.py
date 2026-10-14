"""Make-up gain per chain matched on the 'body' band (150 Hz - 4 kHz RMS) instead of LUFS, at a
typical DI level (-12 dBFS peaks). Writes body_db into levels.json.

  python -I body_levels.py CONFIG_DIR REFERENCE.nam
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_irs import PLAN, run_chain  # noqa: E402
from render_chains import SR, guitar_di, load_wavenet, lufs  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
SOS = butter(4, [150, 4000], btype="band", fs=SR, output="sos")


def body_db(x):
    return 10 * np.log10(np.mean(sosfilt(SOS, x) ** 2))


if __name__ == "__main__":
    cfg, ref_path = Path(sys.argv[1]), Path(sys.argv[2])
    di = guitar_di() / 0.5 * 0.25
    ref = load_wavenet(ref_path)(di)
    rb = body_db(ref)
    table = json.load(open(PLUGIN / "levels.json"))
    table["input_peak_dbfs"] = -12.0
    for cat, (stages, cab, _) in PLAN.items():
        y = di
        for s in stages:
            y = load_wavenet(cfg / "nam_models" / s)(y)
        irp = cfg / "nam_irs" / "tone_pack" / f"{cat}.wav"
        z = run_chain(y, sf.read(str(irp))[0] if irp.exists() else None)
        mk = rb - body_db(z)
        table["categories"][cat]["makeup_db"] = round(mk, 2)
        table["categories"][cat]["lufs_makeup_db"] = round(lufs(ref) - lufs(z), 2)
        print(f"{cat:9} body make-up {mk:+5.1f} dB   (LUFS-matched would be {lufs(ref) - lufs(z):+5.1f})")
    json.dump(table, open(PLUGIN / "levels.json", "w"), indent=1)
