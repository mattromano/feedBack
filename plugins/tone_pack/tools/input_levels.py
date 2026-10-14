"""How each chain's needed make-up (vs the reference) changes with the guitar's input level.

  python -I input_levels.py CONFIG_DIR REFERENCE.nam
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_irs import PLAN, run_chain  # noqa: E402
from render_chains import guitar_di, load_wavenet, lufs  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    cfg, ref_path = Path(sys.argv[1]), Path(sys.argv[2])
    models = cfg / "nam_models"
    base = guitar_di()
    ref_m = load_wavenet(ref_path)
    peaks = [0.06, 0.12, 0.25, 0.5, 0.9]          # DI peak level (dBFS about -24 .. -1)
    print("DI peak dBFS:", [round(20 * np.log10(p), 1) for p in peaks])
    refs = [lufs(ref_m(base / 0.5 * p)) for p in peaks]
    print(f"{'reference':9}", [round(r, 1) for r in refs])
    for cat, (stages, cab, _) in PLAN.items():
        ms = [load_wavenet(models / s) for s in stages]
        irp = cfg / "nam_irs" / "tone_pack" / f"{cat}.wav"
        ir = sf.read(str(irp))[0] if irp.exists() else None
        row = []
        for p, rl in zip(peaks, refs):
            y = base / 0.5 * p
            for m in ms:
                y = m(y)
            row.append(round(rl - lufs(run_chain(y, ir)), 1))
        print(f"{cat:9} make-up dB", row)
