"""Unit tests for lib/fivefret.py — five-fret validation and wire helpers."""

from __future__ import annotations

import copy

import fivefret


def _minimal() -> dict:
    return {"version": 1, "name": "Bass", "instrument": "bass",
            "source": {"kind": "chart"}, "notes": [{"t": 0.5, "f": 1}]}


def test_constants():
    assert fivefret.SCHEMA_VERSION == 1
    assert fivefret.PARTS == ("guitar", "rhythm", "bass")
    assert fivefret.LEVELS == ("easy", "medium", "hard")


def test_valid_minimal():
    assert fivefret.validate_five_fret(_minimal()) == (True, "")


def test_bad_mask():
    d = _minimal()
    d["notes"] = [{"t": 0.5, "f": 32}]
    ok, why = fivefret.validate_five_fret(d)
    assert not ok and "f" in why


def test_bad_instrument():
    d = _minimal()
    d["instrument"] = "keys"
    assert fivefret.validate_five_fret(d)[0] is False


def test_open_note_ok():
    d = _minimal()
    d["notes"] = [{"t": 0, "f": 0}]
    assert fivefret.validate_five_fret(d) == (True, "")


def test_full_payload_ok():
    d = _minimal()
    d["notes"] = [{"t": 1.0, "f": 31, "l": 0.5, "h": True, "tap": False, "x": 1}]
    d["source"] = {"kind": "auto", "origin": "rb", "generator": "g"}
    d["levels"] = {"easy": {"notes": [{"t": 1, "f": 1}], "generated": True},
                   "extra": "ignored"}
    d["star_power"] = [{"start_time": 1, "end_time": 2}]
    d["solos"] = []
    assert fivefret.validate_five_fret(d) == (True, "")


def test_optional_name_and_instrument():
    d = _minimal()
    del d["name"], d["instrument"]
    assert fivefret.validate_five_fret(d) == (True, "")


def test_rejections():
    def bad(mut):
        d = copy.deepcopy(_minimal())
        mut(d)
        ok, why = fivefret.validate_five_fret(d)
        assert ok is False and why
    bad(lambda d: d.pop("version"))
    bad(lambda d: d.update(version=2))
    bad(lambda d: d.update(version=True))
    bad(lambda d: d.pop("source"))
    bad(lambda d: d.update(source={}))
    bad(lambda d: d.update(source={"kind": "x"}))
    bad(lambda d: d.update(source={"kind": "chart", "origin": "web"}))
    bad(lambda d: d.update(source={"kind": "chart", "md5": 5}))
    bad(lambda d: d.pop("notes"))
    bad(lambda d: d.update(notes={}))
    bad(lambda d: d.update(name=3))
    bad(lambda d: d.update(notes=[{"t": -1, "f": 1}]))
    bad(lambda d: d.update(notes=[{"t": "x", "f": 1}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": 1.5}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": True}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": -1}]))
    bad(lambda d: d.update(notes=[{"t": 0}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": 1, "l": 0}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": 1, "h": 1}]))
    bad(lambda d: d.update(notes=[{"t": 0, "f": 1, "tap": "y"}]))
    bad(lambda d: d.update(notes=["n"]))
    bad(lambda d: d.update(levels=[]))
    bad(lambda d: d.update(levels={"easy": {}}))
    bad(lambda d: d.update(levels={"easy": {"notes": [{"t": 0, "f": 99}]}}))
    bad(lambda d: d.update(levels={"hard": {"notes": [], "generated": 1}}))
    bad(lambda d: d.update(star_power=[{"start_time": 1}]))
    bad(lambda d: d.update(solos={}))
    assert fivefret.validate_five_fret([])[0] is False


def test_levels_to_wire():
    lv = {"hard": {"notes": [{"t": 0, "f": 1}, {"t": 1, "f": 2}], "generated": True}}
    assert fivefret.levels_to_wire(lv) == {"hard": {"total": 2, "generated": True}}


def test_levels_to_wire_edge_cases():
    assert fivefret.levels_to_wire(None) == {}
    lv = {"easy": {"notes": [{"t": "x"}]}, "weird": {"notes": [{"t": 0, "f": 1}]},
          "medium": {"notes": [{"t": 0, "f": 1}]}}
    assert fivefret.levels_to_wire(lv) == {"medium": {"total": 1, "generated": False}}


def test_notes_to_wire_sorts_and_drops():
    notes = [{"t": 2, "f": 4}, {"t": "x"}, {"t": 1, "f": 3, "l": 0.5, "h": True},
             {"t": 1, "f": 1}, {"t": 3, "f": 40}]
    assert fivefret.notes_to_wire(notes) == [
        {"t": 1.0, "f": 1}, {"t": 1.0, "f": 3, "l": 0.5, "h": True}, {"t": 2.0, "f": 4}]
    assert fivefret.notes_to_wire(None) == []


def test_ranges_reused_from_drums():
    import drums
    assert fivefret.ranges_to_wire is drums.ranges_to_wire


_HUGE = 10 ** 400                               # json parses a 400-digit literal as this int


def test_non_finite_and_huge_numbers_rejected():
    """NaN / inf / a too-big int in t, f or l is (False, reason), never an exception."""
    for key in ("t", "f", "l"):
        for v in (float("nan"), float("inf"), float("-inf"), _HUGE, -_HUGE):
            d = _minimal()
            d["notes"] = [{"t": 0.5, "f": 1, key: v}]
            ok, why = fivefret.validate_five_fret(d)
            assert ok is False and why, (key, v)
            assert fivefret.notes_to_wire(d["notes"]) == []


def test_huge_range_bound_rejected():
    d = _minimal()
    d["star_power"] = [{"start_time": 0, "end_time": _HUGE}]
    ok, why = fivefret.validate_five_fret(d)
    assert ok is False and "star_power" in why
