"""lib/fivefret_bench.py: metrics and the pass line."""

from __future__ import annotations

import fivefret_bench as fb
from fivefretchart import FretNote


def N(t, frets=1, **kw):
    return FretNote(time=t, frets=frets, **kw)


def seq(frets, t0=0.0, dt=0.5):
    return [N(t0 + i * dt, f) for i, f in enumerate(frets)]


def test_match_one_to_one_within_tol():
    real = [N(1.0), N(1.05)]
    auto = [N(1.04), N(2.0)]
    pairs = fb.match_notes(real, auto)
    assert len(pairs) == 1
    assert pairs[0][0].time == 1.05 and pairs[0][1].time == 1.04
    assert fb.match_notes([N(1.0)], [N(1.2)]) == []
    assert fb.onset_f1(real, auto) == 0.5


def test_direction_perfect_and_inverted():
    real = seq([1, 2, 4, 8, 16])
    pairs = fb.match_notes(real, real)
    assert fb.direction_agreement(pairs) == 1.0
    mirror = seq([16, 8, 4, 2, 1])
    assert fb.direction_agreement(fb.match_notes(real, mirror)) == 0.0
    stay = seq([1, 1, 1, 1, 1])
    assert fb.direction_agreement(fb.match_notes(stay, stay)) == 1.0
    assert fb.direction_agreement(fb.match_notes(real[:1], real[:1])) is None


def test_lane_closeness_shift_invariant():
    auto = seq([1, 2, 4, 2, 1])
    real = seq([4, 8, 16, 8, 4])  # auto shifted up by 2 lanes
    assert fb.lane_closeness(fb.match_notes(real, auto)) == 0.0
    assert fb.lane_closeness([]) is None


def test_chord_size_and_open():
    real = [N(0.0, 0b11), N(1.0, 0), N(2.0, 0b111)]
    auto = [N(0.0, 0b110), N(1.0, 0b1), N(2.0, 0b11)]
    assert fb.lane_center(0) == -1.0 and fb.lane_center(0b110) == 1.5
    assert fb.chord_size_agreement(fb.match_notes(real, auto)) == 2 / 3
    hp = fb.hopo_agreement(fb.match_notes([N(0, hopo=True), N(1)], [N(0, tap=True), N(1, hopo=True)]))
    assert hp == 0.5


def test_density_ratio_overlap_only():
    real = [N(t) for t in (0.0, 1.0, 2.0, 3.0)]
    auto = [N(t / 2 + 1.0) for t in range(0, 20)]  # 1.0..10.5, overlap 1.0..3.0
    # real in window: 1,2,3 -> 3; auto in window: 1.0..3.0 step .5 -> 5
    assert fb.density_ratio(real, auto) == 5 / 3
    assert fb.density_ratio(real, [N(10.0), N(11.0)]) is None
    assert fb.density_ratio([], auto) is None


def test_fret_limit():
    assert fb.fret_limit_ok([N(0, 0), N(0.5, 0b100)], "easy")
    assert not fb.fret_limit_ok([N(0, 0b1000)], "easy")
    assert fb.fret_limit_ok([N(0, 0b1000)], "medium")
    assert not fb.fret_limit_ok([N(0, 0b10000)], "medium")
    assert fb.fret_limit_ok([N(0, 0b11111)], "hard")
    assert fb.fret_limit_ok([N(0, 0b11111)], "expert")


def test_split_deterministic():
    names = [f"pack{i}.sloppak" for i in range(200)]
    assert [fb.split(n) for n in names] == [fb.split(n) for n in names]
    got = {fb.split(n) for n in names}
    assert got == {"tune", "holdout"}


def test_score_levels_only_real_levels():
    real = {"expert": seq([1, 2, 4] * 10), "hard": seq([1, 2] * 5)}
    auto = {"expert": seq([1, 2, 4] * 10), "hard": seq([1, 2] * 5),
            "medium": seq([1, 8]), "easy": seq([1, 2])}
    s = fb.score_levels(real, auto)
    assert s["expert"]["matched"] == 30 and s["expert"]["direction"] == 1.0
    assert s["hard"]["has_real"] and s["hard"]["density_ratio"] == 1.0
    assert not s["medium"]["has_real"] and s["medium"]["density_ratio"] is None
    assert s["medium"]["fret_limit_ok"] is True
    assert s["easy"]["fret_limit_ok"] is True
    bad = fb.score_levels({}, {"easy": [N(0, 0b1000)]})
    assert bad["easy"]["fret_limit_ok"] is False


def _lv(matched=30, direction=0.8, lane=0.5, chord=0.7, dens=1.0, ok=True, has_real=True):
    def low(d=dens, h=has_real):
        return {"has_real": h, "density_ratio": d if h else None, "fret_limit_ok": ok}
    return {"expert": {"has_real": True, "matched": matched, "direction": direction,
                       "lane_closeness": lane, "chord_size": chord, "density_ratio": 1.0,
                       "fret_limit_ok": True},
            "hard": low(), "medium": low(), "easy": low()}


def _rows(n=20, **kw):
    return [{"pack": f"p{i}", "part": "guitar", "split": "holdout", "paired": True,
             "levels": _lv(**kw)} for i in range(n)]


def _failed(v):
    return sorted((c["name"], c["level"]) for c in v["checks"] if not c["ok"])


def test_verdict_pass_and_each_failure():
    v = fb.verdict(_rows())
    assert v["pass"] and v["n"] == 20 and _failed(v) == []
    assert _failed(fb.verdict(_rows(direction=0.6))) == [("direction", "expert")]
    assert _failed(fb.verdict(_rows(lane=1.5))) == [("lane_closeness", "expert")]
    assert _failed(fb.verdict(_rows(chord=0.5))) == [("chord_size", "expert")]
    assert set(_failed(fb.verdict(_rows(dens=1.3)))) == {
        ("density_ratio", "hard"), ("density_ratio", "medium"), ("density_ratio", "easy")}
    assert _failed(fb.verdict(_rows(dens=0.7)))[0][0] == "density_ratio"
    rows = _rows()
    rows[3]["levels"]["easy"]["fret_limit_ok"] = False
    v = fb.verdict(rows)
    assert not v["pass"] and _failed(v) == [("fret_limit", "easy")]
    rows = _rows()
    rows[0]["levels"]["medium"]["fret_limit_ok"] = False
    assert _failed(fb.verdict(rows)) == [("fret_limit", "medium")]


def test_verdict_expert_medians_need_min_matched():
    rows = _rows()
    for r in rows[:10]:  # too few matched notes: ignored, so the bad values don't count
        r["levels"]["expert"].update(matched=5, direction=0.0)
    assert fb.verdict(rows)["pass"]
    for r in rows:
        r["levels"]["expert"]["matched"] = 5
    assert not fb.verdict(rows)["pass"]


def test_verdict_excludes_levels_without_real():
    rows = _rows()
    for r in rows[:5]:
        r["levels"]["easy"] = {"has_real": False, "density_ratio": None, "fret_limit_ok": True}
    v = fb.verdict(rows)
    easy = [c for c in v["checks"] if c["name"] == "density_ratio" and c["level"] == "easy"][0]
    assert v["pass"] and easy["excluded"] == 5


def test_verdict_needs_twenty():
    v = fb.verdict(_rows(19))
    assert not v["pass"] and v["n"] == 19
    assert [c for c in v["checks"] if c["name"] == "enough_songs"][0]["ok"] is False


def test_unpaired_rows_ignored():
    rows = _rows(20)
    rows += [dict(r, paired=False, levels=_lv(direction=0.0)) for r in _rows(10)]
    rows += [dict(r, split="tune", levels=_lv(direction=0.0)) for r in _rows(10)]
    v = fb.verdict(rows)
    assert v["pass"] and v["n"] == 20


def test_fret_limit_counts_every_row():
    rows = _rows(20)
    rows.append({"pack": "t", "part": "guitar", "split": "tune", "paired": False,
                 "levels": _lv(ok=False)})
    v = fb.verdict(rows)
    assert v["n"] == 20
    assert _failed(v) == [("fret_limit", "easy"), ("fret_limit", "medium")]
    fl = [c for c in v["checks"] if c["name"] == "fret_limit"]
    assert [c["value"] for c in fl] == [1, 1] and fl[0]["rows"] == 21


def test_score_levels_sorts_by_time():
    real = seq([1, 2, 4, 8, 16] * 6)
    auto = list(reversed(real))
    s = fb.score_levels({"expert": list(reversed(real))}, {"expert": auto})
    assert s["expert"]["matched"] == 30 and s["expert"]["direction"] == 1.0
