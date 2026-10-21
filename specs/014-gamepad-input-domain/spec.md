# Spec 014 — Gamepad-Input Capability Domain

**Status:** active · **Plan:** plastic controllers, plan 2 (highway input), task 2 · **Base:** `feature/five-fret-charts`

## Summary

`gamepad-input` is a **core-owned provider-coordinator** capability domain that
lets a plugin claim one game controller and receive its raw button and axis
changes. It is how plastic guitars, plastic drum kits and Xbox-style pads are
played as instruments in FeedBack. Core stays instrument-agnostic: it reports
"button 4 went down", and the consuming plugin (first, the five-fret plugin)
names the inputs through its own controller profiles.

## Motivation

Core's menu gamepad support (`static/v3/gamepad.js`) turns standard pads into
keyboard events for navigation. A plugin that plays a controller as an
instrument needs the opposite: raw, timestamped input from one controller, and
the menus out of the way while it plays (otherwise A → Space → pause). Without a
shared domain each plugin would poll `navigator.getGamepads()` privately and
nothing could tell core's menus to stand down.

## Why not reuse `midi-input`

`midi-input` is a source/session plane over discrete MIDI messages with a
permission boundary (`requestMIDIAccess()`) and provider-registered sources. The
Gamepad API has no permission prompt, no providers and no messages: it is a
polled state snapshot per slot. The useful contract is different — exclusive
claim of a slot, change detection, reconnect handling — so it is a sibling
domain.

## Why core-owned (not plugin-owned)

The claim registry is shared state between every controller consumer and core's
own menu support (`claimCount()`). If a feature plugin owned it, core's menus
would depend on that plugin. The domain is `core.gamepad-input`, as
`midi-input` is `core.midi-input`.

## Contract

- **Owner:** `core.gamepad-input`, kind `provider-coordinator`, safety `sensitive`.
- **Public commands:** `inspect`, `list-sources` (read-only snapshots).
- **Provider operations:** `source.enumerate`, `source.describe`.
- **Events:** `change` (`{ type: 'connect'|'disconnect'|'claim'|'release', index }`).

### In-page API (`window.feedBack.gamepadInput`, `version: 1`)

- `list()` → `[{ index, id, mapping, vendor, product, connected, claimedBy }]`.
  `vendor`/`product` are parsed from `id` (`Vendor: 045e Product: 02ea`, bare or
  in parentheses), lower-case hex, else `null`.
- `claim(index, ownerId)` → handle, or `null` when `index` is not a
  non-negative integer, there is no connected controller at `index`, or another
  owner holds it. Claiming again with the same
  owner returns the same handle.
- handle: `{ index, id, controllerKey, addListener(fn), removeListener(fn), release() }`.
  `fn(ev)` gets `{ type: 'button'|'axis', index, value, pressed (buttons only),
  t }` with `t = performance.now() / 1000`, fired only on change. Axis changes
  (and analog button value changes) smaller than 0.02 against the last reported
  value are ignored.
- `isClaimed(index)`, `claimCount()`.
- `on('change', fn)` → unsubscribe function.

### Identity

`controllerKey` is the controller `id` plus `#n`, for the nth identical
(same-id) controller in slot order (`…#1`, `…#2`). It is fixed for the life of
the claim.

### Polling

Polling runs on `requestAnimationFrame` only while at least one claim exists;
releasing the last claim lets the loop lapse. The steady state allocates
nothing: values are compared in place, the reconcile pass runs only when a
claim is not connected in its own slot, and listener lists are copy-on-write so
dispatch never copies them. A claim's baseline is the
controller's state at claim time, so held inputs do not fire on claim.

### Disconnect and reconnect

When a claimed controller disappears the handle stays valid but silent and the
claim still counts. A claim keeps its slot while a controller with its `id` is
there; otherwise it rebinds to a free slot holding an identical controller
(preferring its own slot), and events resume on the same handle, whose `index`
follows the controller. Changes are diffed against the last reported state, so a
button held when the controller unplugged reports its release on reconnect.
A different controller arriving in the vacated slot is not routed to the claim
and is free to be claimed.

### Change events

`change` fires once per transition. When a poll tick sees an unplug or replug
before the browser's `gamepadconnected`/`gamepaddisconnected` event arrives, the
later browser event for the same slot and type is not announced again.

### Trust

Owner ids are not authenticated. Any in-page script that knows an owner id can
call `claim(index, ownerId)` and receive (or `release()`) that owner's handle.
This is the same in-page trust model as `midi-input`; the owner check only
keeps cooperating plugins from claiming the same controller.

### Redaction

Live input never leaves the in-page handle: no capability events or
diagnostics carry button or axis values. The command snapshots list controllers
(with their `id`) and claims (index, owner, connected).

### Without the Gamepad API

When `navigator.getGamepads` is missing, `list()` returns `[]`, `claim()`
returns `null` and nothing polls.

## Consumers

- The five-fret plugin (plastic guitar / gamepad play), via its own profiles.
- Core's menu gamepad support stands down while `claimCount() > 0`.

## Acceptance

- Owner registers; appears in the Capability Inspector with `inspect` and `list-sources`.
- A second owner cannot claim a held controller.
- Button and axis changes fire once per change, with the 0.02 axis threshold.
- No rAF is queued while there are no claims.
- Unplugging and replugging the same controller resumes events on the same handle.
- Safe without `navigator.getGamepads`.

Tests: `tests/js/gamepad_input_domain.test.js`.

## Out of scope

- Naming inputs (frets, pads, strum) — the consuming plugin's profiles.
- Menu stand-down in `static/v3/gamepad.js` (plan 2, task 3).
- Rumble/haptics and output.
