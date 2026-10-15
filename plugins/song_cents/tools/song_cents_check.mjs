// Check the song_cents estimator against known offsets (PSARC CentOffset), or just measure.
//   node plugins/song_cents/tools/song_cents_check.mjs <tmp_dir> <file.sloppak[=expectedCents]> ...
// Extracts each sloppak's guitar stem (falls back to full), decodes it with the app's bundled
// ffmpeg (FFMPEG env var, else ffmpeg on PATH) to mono float PCM, and runs assets/estimate.js on it.
import { execFileSync } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { estimateCents } = require('../assets/estimate.js');
const FFMPEG = process.env.FFMPEG || 'ffmpeg';   // e.g. the desktop app's bundled resources/bin/ffmpeg
const SR = 44100;

const [tmp, ...items] = process.argv.slice(2);
if (!tmp || !items.length) {
    console.log('usage: node plugins/song_cents/tools/song_cents_check.mjs <tmp_dir> <file.sloppak[=cents]> ...');
    process.exit(1);
}
let worst = 0;
for (const item of items) {
    const [file, expect] = item.split('=');
    const out = path.join(tmp, 'stem.ogg');
    const py = 'import sys,zipfile\nz=zipfile.ZipFile(sys.argv[1])\nn=set(z.namelist())\n' +
        'm=next((s for s in ("stems/guitar.ogg","stems/full.ogg") if s in n), None) or next(s for s in n if s.endswith(".ogg"))\n' +
        'open(sys.argv[2],"wb").write(z.read(m))\nprint(m)';
    const stem = execFileSync('python', ['-I', '-c', py, file, out]).toString().trim();
    const raw = path.join(tmp, 'stem.f32');
    execFileSync(FFMPEG, ['-v', 'error', '-y', '-i', out, '-ac', '1', '-ar', String(SR), '-f', 'f32le', raw]);
    const buf = readFileSync(raw);
    const pcm = new Float32Array(buf.buffer, buf.byteOffset, buf.length / 4);
    const t0 = Date.now();
    const r = estimateCents(pcm, SR);
    const ms = Date.now() - t0;
    const err = expect !== undefined ? r.cents - Number(expect) : null;
    if (err !== null) worst = Math.max(worst, Math.abs(err));
    console.log(`${path.basename(file).padEnd(48)} ${stem.padEnd(18)} measured ${r.cents.toFixed(1).padStart(6)}c  conf ${r.confidence.toFixed(2)}`
        + (err !== null ? `  expected ${expect}c  err ${err.toFixed(1)}c` : '') + `  (${ms} ms)`);
}
if (worst) console.log(`worst error vs expected: ${worst.toFixed(1)} c`);
