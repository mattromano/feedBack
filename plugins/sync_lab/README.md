# Sync Lab (personal in-tree plugin)

Measured calibration of the three offsets that decide whether notes are judged when you actually play them:

| Offset | Setting it changes |
|---|---|
| A/V offset (picture vs sound, one per computer) | core `av_offset_ms` via `window.setAvOffsetMs` (reaches every highway, split panels included) |
| Guitar latency | Note Detection `latencyOffset`, applied to every detector instance (main + split-view panels) |
| Drums input offset | drums plugin `drums_input_offset_ms` (`window.__drumsSetInputOffset`) |

Nav → **Sync**: pick Guitar / Bass / Drums / Split guitar+drums / Split two guitars. That opens the
**Sync Calibration** song, written into the library by `routes.py` as `sloppak/_Sync_Calibration_v<N>.sloppak`
(pure Python). 100 BPM, one note per beat (open A string on Lead/Bass, snare on Drums):

1. **Listen** (32 notes): the screen is blanked; play on the clicks, by ear.
2. **Watch** (32 notes): no clicks; play when the gems hit the line.
3. **Play** (24 notes): normal, as a check.

During the song drum pad sounds are muted (`window.__drumsMutePads`) and the drum hit window is ±250 ms
(`window.__drumsCalibrationWindow`), so far-off hits are measured instead of missed.

## The maths

Note Detection and the drum engine judge on the highway's visual clock (song clock + A/V offset). With the song
clock running R ms ahead of the speakers:

    eye error = display latency + input latency            (A/V offset cancels)
    ear error = input latency + R + A/V offset

Ear and eye agree when **A/V = current A/V + (eye − ear)** (weighted over all players, one value per computer).
The **eye median is the input offset** for that player. Samples are kept offset-free (error + the offset in use).

## A/V auto-follow

`window.__hwtRenderAheadMs` (highway_tweaks' stems clock) is R, measured live. On Apply (or any manual A/V change)
`{av, R}` is saved; when a later song shows R changed by ≥ 10 ms (other headphones / speakers), the A/V offset is
shifted by the same amount and a toast says so. Toggle on the Sync screen.

## Inputs

- Guitar: Note Detection's `notedetect:hit` / `notedetect:miss` events (`timingError`, `latencyMs`).
- Drums: the drums plugin's `drums:judgment` window events (`noteTime`, `errMs`, `inputOffsetMs`, `canvas`).

## Logs

Every calibration (with all raw samples) and every auto-follow change is appended to
`<config>/sync_lab/sync_log.jsonl` (`GET /api/plugins/sync_lab/log?n=20`), included in Export Diagnostics.

Tests: `tests/plugins/sync_lab/`.
