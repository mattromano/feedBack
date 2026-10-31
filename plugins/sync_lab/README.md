# Calibrate (Sync Lab, personal in-tree plugin)

Main menu → **Calibrate** (promoted in `static/v3/shell.js`). One guided test that measures and sets every
offset that decides whether notes are judged when you actually play them, then locks them:

| Setting | What it changes |
|---|---|
| A/V offset (picture vs sound, one per computer) | core `av_offset_ms` via `window.setAvOffsetMs(v, false, 'manual')` (every highway, split panels included) |
| Guitar latency | Note Detection `latencyOffset`, applied to every detector instance (main + split-view panels) |
| Drum kit input offset | drums plugin `drums_input_offset_ms` (`window.__drumsSetInputOffset`) |
| Kick pedal extra offset | drums plugin `drums_kick_offset_ms` (`feedBackDrums.setKickOffset`; manual only) |
| Each drum controller's offset | drums plugin `drums.gamepad.settings.v1` `offsetMs[key]` (`feedBackDrums.setControllerOffset`) |
| Each 5-Fret controller's offset | five_fret plugin `five_fret.settings.v1` `offsetMs[key]` (`feedBackFiveFret.setControllerOffset`) |

## Flow

1. **What are you calibrating?** Tiles: real guitar, real bass, drum kit, and every connected drum or 5-Fret
   controller. Pick one, or two for split view.
2. **Get ready.** The three parts explained, a checklist per instrument and a live "press a button / hit a pad"
   check. Nothing plays until **Start calibration** is clicked.
3. The **Sync Calibration** song (written into the library by `routes.py` as `sloppak/_Sync_Calibration_v<N>.sloppak`,
   pure Python). 100 BPM, one note per beat: open A string on Lead/Bass, snare on Drums, an open strum on the 5-fret
   guitar part. A coach banner shows the step, beat x of N, a hit counter and the last hits as early/late dots per
   input, plus warnings when an input sends nothing.
   - **Listen** (32 notes): the screen is blanked; play on the clicks, by ear (big hit counters only).
   - **Watch** (32 notes): no clicks; play when the gems hit the line.
   - **Play** (24 notes): normal, as a check.
4. **Results**: a summary sentence, then one row per setting with Now, Suggested, Change and a plain-words
   Confidence (great / good / rough / unreliable, from hit count and spread). Apply per row, Apply selected or
   Apply all; the raw ear / eye table is under Details.

The screen also lists **every timing setting** with −/+ and exact entry, the A/V lock, the audio output latency,
the headphone-follow toggle and the last calibration.

During the song drum pad sounds are muted (`window.__drumsMutePads`), and the drum and 5-Fret hit windows are
±250 ms (`window.__drumsCalibrationWindow`, `window.__fiveFretCalibrationWindow`), so far-off hits are measured
instead of missed. Drums' Freestyle play style is off while the window is set.

## The maths

Note Detection, drums and 5-Fret judge on the highway's visual clock (song clock + A/V offset). With the song
clock running R ms ahead of the speakers:

    eye error = display latency + input latency            (A/V offset cancels)
    ear error = input latency + R + A/V offset

Ear and eye agree when **A/V = current A/V + (eye − ear)** (weighted over all players, one value per computer).
The **eye median is the input offset** for that player. Samples are kept offset-free (error + the offset in use).

## Lock

Applying a result locks the calibration: the A/V write is tagged `'manual'` (core then ignores untagged writes,
`window.isAvOffsetLocked()` reports it) and Note Detection's end-of-song A/V auto-calibrate is turned off.
During a run auto-calibrate is off too, so the song end can't move A/V under the results. Unlock on the
Calibrate screen hands A/V back (`'reset'`) and turns auto-calibrate on again.

## A/V auto-follow

`window.__hwtRenderAheadMs` (highway_tweaks' stems clock) is R, measured live. On Apply (or any manual A/V change,
announced by core as `settings:av-offset`) `{av, R}` is saved; when a later song shows R changed by ≥ 10 ms (other
headphones / speakers), the A/V offset is shifted by the same amount (`'follow'`, keeps the lock) and a toast says so.

## Inputs

- Guitar: Note Detection's `notedetect:hit` / `notedetect:miss` events (`timingError`, `latencyMs`).
- Drums: the drums plugin's `drums:judgment` window events (`noteTime`, `errMs`, `inputOffsetMs`, `kickOffsetMs`,
  `controllerKey` (null = the kit), `canvas`).
- 5-Fret: `five_fret:hit` / `five_fret:miss` bus events (`noteTime`, `offsetMs`, `inputOffsetMs`, `controllerKey`, `canvas`).

Only the inputs picked on the Calibrate screen are counted.

## Logs

Every calibration (with all raw samples), every Apply and every auto-follow change is appended to
`<config>/sync_lab/sync_log.jsonl` (`GET /api/plugins/sync_lab/log?n=20`), included in Export Diagnostics.

Tests: `tests/plugins/sync_lab/`, `tests/js/v3_shell_calibrate_nav.test.js`, `tests/js/av_offset_override.test.js`.
