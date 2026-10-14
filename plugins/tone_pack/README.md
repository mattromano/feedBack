# Tone Pack (personal in-tree plugin)

Gives the desktop app's Audio plugin (Chain panel) **Tone Automation** a preset for every category, built from the NAM captures and
cab IRs already on this computer (Rig Builder's `slopsmith-config/nam_models` and `nam_irs`).

| Category | Preset | Chain |
|---|---|---|
| Clean | Auto · Clean (Twin Reverb) | Fender Twin Reverb Ch1 → Rocksmith TW 1x12 cab (SM57 cone) |
| OD | Auto · Crunch (Marshall DSL40) | Marshall DSL40 crunch → Marshall 1960A |
| Dist | Auto · Distortion (5153) | EVH 5153 → Marshall 1960AX (SM57 edge) |
| Solo | Auto · Lead (Mesa Mark V) | Mesa Mark V lead → Marshall 1960A |
| Bass | Auto · Bass (Ampeg SVT) | Ampeg SVT clean → Ampeg 8x10 |
| Acoustic | Auto · Acoustic (emulator) | acoustic-emulator pedal capture |
| Mod | Auto · Clean Chorus (Twin) | chorus capture → Twin → cab; with Kilohearts Essentials installed it becomes **Auto · Clean Chorus + Delay (Kilohearts)**: Twin → cab → kHs Chorus → kHs Delay → kHs Reverb |
| Idle | your own preset (e.g. Main Lead) | — |

- Missing files fall back to alternatives (see `RECIPES` in `routes.py`); a preset without any capture is skipped.
- **Levels** are matched to Main Lead (an amp+cab capture at unity gain, NAM loudness -19.3): chain
  loudness = amp capture loudness + the IRs' level after the engine's IR normalization (JUCE
  `Normalise::yes` costs a guitar-band signal 16-22 dB; measured per IR in `ir_gains.json`). The make-up
  gain goes on the amp stage's own output level (NAM slot state, JUCE base64), since the preset output
  slider stops at +12 dB. Your own level / gate changes to an Auto preset survive pack updates.
- `screen.js` merges at startup: adds missing pack presets, refreshes pack presets when the pack changes
  (keeping your gain / gate tweaks), never touches presets you made, never overrides targets you set,
  and keeps a pack preset you deleted deleted. First run with no Tone Automation config turns it on.
- No knob state is stored: NAM / IR stages have none, and the Kilohearts plugins start at their defaults.

Tests: `tests/plugins/tone_pack/` (`node --test tests/plugins/tone_pack/js/*.test.js`, pytest).

**Pack IRs are not in this repo.** `tools/build_irs.py` makes them from the Rocksmith cab IRs on this
computer and writes `<config>/nam_irs/tone_pack/<category>.wav`; copy the old plugin's `irs/*.wav` there to
keep the 0.2.9 tone. Without them each recipe uses the plain Rocksmith cab from `nam_irs/rocksmith`.

## Every song: categories from the song's gear

Tone Automation sorts tones by *name*. For a sloppak, `GET /api/plugins/tone_pack/song_tones` classifies
each tone of the playing arrangement from its gear (`gear_class.py`: amp gain >= 70 or a distortion/fuzz
pedal = Dist, gain >= 55 or a drive pedal = OD, chorus/phaser/trem/wah on a clean amp = Mod, acoustic
emulator = Acoustic, Bass amp or a Bass arrangement = Bass, "lead"/"solo" anywhere in the name = Solo;
thresholds fitted on tones whose names say clean / crunch / dist). On `song:ready` those become the Audio
plugin's session overrides for tones the name classifier can't place (they'd fall back to Idle), for all
tones of a Bass part, and for single-tone arrangements (keyed by the song file, which is what Tone
Automation classifies when there's no tone base). Manual per-tone picks in the Chain panel win.
PSARC songs keep name-only classification.

## Levels and tone (v1.2)

`tools/` (dev only: torch / numpy / scipy / soundfile) renders the chains offline (a NAM WaveNet forward pass from the .nam weights + the engine's
IR normalisation) against Main Lead with a synthetic guitar DI:
- `build_irs.py` picks a cab/mic per category and bakes a smooth EQ (max +-8 dB; clean/mod halfway)
  toward Main Lead's spectrum into `<config>/nam_irs/tone_pack/<category>.wav` (Main Lead is far darker above 6 kHz; the
  amp-only chains sounded thin and fizzy). Bass gets the bass cab/mic with the most low end.
- `body_levels.py` sets each preset's make-up gain so its 150 Hz-4 kHz level matches Main Lead at a
  -12 dBFS-peak DI (`levels.json`). Distortion / lead levels barely depend on input level; clean, bass
  and acoustic do (`input_levels.py`), so those may need a trim for a very hot or quiet input.
- Every preset uses Main Lead's noise gate (-60 dB) and Tone Polish.

Settings → Tone Pack: per-category volume trims (-12..+12 dB) and the preset to load after a song
(default Main Lead; "Keep the last tone" turns it off). The return happens on `song:ended` and
`song:stop` (leaving the player), not while the next song is starting.

## Your own presets per category (v1.3)

Per request: OD (crunch) and Dist play **Main Lead**, Solo (leads) plays **Metal Tone**, Clean keeps the
Auto Clean. Songs by artists on the metal list (Settings → Tone Pack; MCR, Metallica, Trivium, Linkin Park…)
play **Metal Tone** for every crunch / distortion tone (name- or gear-classified), via the same per-song
overrides. The target change is applied once (`tone-pack-targets-v`); later changes in the Audio settings
are kept.
