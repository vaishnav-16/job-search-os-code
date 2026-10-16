"""Tests for fire_decay.py, the W4 in-fire decay instrument.

Run: python -m pytest pipeline/test_fire_decay.py -q   (tmp trees; no DB, no network)

What has to hold for the numbers to mean anything: a resume lands in the fire
that started it (and nowhere when it is too far from any cron time), position
is the order inside that fire with a stable tie-break, the floor is the lower
of the two reviewers, local tailors never count, and a sidecar from the older
shape (no `scores`) neither crashes the run nor drags a mean.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fire_decay as fd  # noqa: E402

CRON = (30, [8, 14, 21])


class Tree:
    def __init__(self, root: Path):
        self.root = root
        for d in ("tailored", "scores", "queue", "applications"):
            (root / d).mkdir()

    def job(self, jid, at, rec=None, hm=None, score=None, lane="auto-tailor",
            title="Product Manager", who="routine", review_line=None, note="test"):
        folder = f"applications/{jid}-folder"
        (self.root / folder).mkdir()
        side = {"id": jid, "folder": folder, "tailored_at": at, "tailorer": who, "rounds": 2}
        if rec is not None:
            side["scores"] = {"ats": 90, "recruiter": rec, "hm": hm}
        (self.root / "tailored" / f"{jid}.json").write_text(json.dumps(side), encoding="utf-8")
        if review_line:
            (self.root / folder / "review.md").write_text(f"# review\n\nbody\n\n{review_line}\n", encoding="utf-8")
        if score is not None:
            (self.root / "scores" / f"{jid}.json").write_text(
                json.dumps({"id": jid, "score": score, "note": f"{lane}; {note}"}), encoding="utf-8")
        (self.root / "queue" / f"{jid}.json").write_text(json.dumps({"id": jid, "title": title}), encoding="utf-8")

    def rows(self):
        return {r["id"]: r for r in fd.collect(self.root, *CRON, db_path=None)}


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_fire_grouping_and_unattributed(tmp_path):
    t = Tree(tmp_path)
    t.job("a", "2026-10-10T08:55:00Z", 8, 8, 9)
    t.job("b", "2026-10-10T09:40:00Z", 7, 8, 9)       # still the 08:30 fire
    t.job("c", "2026-10-10T15:01:00Z", 7, 7, 9)       # 14:30 fire
    t.job("d", "2026-10-10T22:10:00Z", 7, 7, 9)       # 21:30 fire
    t.job("e", "2026-10-11T03:45:00Z", 7, 7, 9)       # 6h15m after 21:30: no fire
    r = t.rows()
    assert r["a"]["fire"] == r["b"]["fire"] == utc("2026-10-10T08:30:00")
    assert r["c"]["fire"] == utc("2026-10-10T14:30:00")
    assert r["d"]["fire"] == utc("2026-10-10T21:30:00")
    assert r["e"]["fire"] is None and r["e"]["position"] is None
    s = fd.summarize(list(r.values()), date(2026, 10, 10), date(2026, 10, 11))
    assert s["fires"] == 3 and s["resumes"] == 4 and s["unattributed"] == 1


def test_position_order_and_tie_break(tmp_path):
    t = Tree(tmp_path)
    # written out of order; z and y share a timestamp, so id breaks the tie
    t.job("z", "2026-10-10T15:22:20Z", 8, 8, 8)
    t.job("m", "2026-10-10T15:01:00Z", 8, 8, 8)
    t.job("y", "2026-10-10T15:22:20Z", 8, 8, 8)
    t.job("k", "2026-10-10T15:40:00Z", 8, 8, 8)
    r = t.rows()
    assert [r[i]["position"] for i in ("m", "y", "z", "k")] == [1, 2, 3, 4]


def test_floor_and_bucket_math(tmp_path):
    t = Tree(tmp_path)
    # one fire of six scored-8 jobs: positions 1-4 early, 5-6 late
    floors = [(8, 7), (7, 7), (8, 8), (6, 7), (7, 6), (6, 6)]
    for i, (rec, hm) in enumerate(floors):
        t.job(f"j{i}", f"2026-10-10T09:0{i}:00Z", rec, hm, 8)
    t.job("s", "2026-10-10T09:30:00Z", 8, 9, 6, lane="digest-stretch", title="Forward Deployed Engineer")
    rows = list(t.rows().values())
    assert {r["id"]: r["floor"] for r in rows}["j0"] == 7
    s = fd.summarize(rows, date(2026, 10, 10), date(2026, 10, 10))
    eight = s["by_score"]["8"]
    assert eight["early"]["n"] == 4 and eight["early"]["floor"] == 7.0   # 7, 7, 8, 6
    assert eight["late"]["n"] == 2 and eight["late"]["floor"] == 6.0     # 6, 6
    assert s["by_score"]["stretch"]["late"] == {"n": 1, "floor": 8.0, "both8": 1.0}
    assert "6" not in s["by_score"]                                       # stretch is its own row
    assert list(s["by_score"]) == ["8", "stretch", "all"]
    assert s["by_family"]["PM"]["both8"] == round(1 / 6, 3)               # only (8, 8)
    assert s["by_family"]["FDE"]["n"] == 1
    assert s["by_family"]["all"]["n"] == 7


def test_promoted_is_its_own_bucket(tmp_path):
    # A dashboard promotion is usually a 5-year stretch Vaish chose by hand. Counted
    # at its scorer score it posed as in-fire decay on 10-13 (promoted 9s at floor 4).
    t = Tree(tmp_path)
    t.job("p", "2026-10-10T09:00:00Z", 4, 5, 9, note="promoted via dashboard 2026-10-10; Senior PM, 5+ years")
    t.job("q", "2026-10-10T09:05:00Z", 8, 8, 9)
    t.job("s", "2026-10-10T09:10:00Z", 6, 6, 8, lane="digest-stretch")
    rows = t.rows()
    assert rows["p"]["bucket"] == "promoted" and rows["q"]["bucket"] == 9
    s = fd.summarize(list(rows.values()), date(2026, 10, 10), date(2026, 10, 10))
    assert list(s["by_score"]) == ["9", "stretch", "promoted", "all"]
    assert s["by_score"]["9"]["early"]["floor"] == 8.0


def test_local_tailors_excluded(tmp_path):
    t = Tree(tmp_path)
    t.job("loc", "2026-10-10T08:40:00Z", 5, 5, 8, who="local")
    t.job("rt1", "2026-10-10T08:50:00Z", 8, 8, 8)
    r = t.rows()
    assert "loc" not in r
    assert r["rt1"]["position"] == 1


def test_missing_scores_tolerated(tmp_path):
    t = Tree(tmp_path)
    t.job("old", "2026-10-10T08:40:00Z")                               # old shape, no review.md, no score
    t.job("rv", "2026-10-10T08:45:00Z", score=10,
          review_line="SCORES: ats 88 / recruiter 7 / hm 8 / screener 7")  # review.md fallback
    t.job("ok", "2026-10-10T08:50:00Z", 8, 8, 10)
    (tmp_path / "tailored" / "broken.json").write_text("{not json", encoding="utf-8")
    r = t.rows()
    assert r["old"]["floor"] is None and r["old"]["bucket"] == "unscored"
    assert (r["rv"]["recruiter"], r["rv"]["hm"], r["rv"]["floor"]) == (7, 8, 7)
    assert [r[i]["position"] for i in ("old", "rv", "ok")] == [1, 2, 3]  # old still holds a slot
    s = fd.summarize(list(r.values()), date(2026, 10, 10), date(2026, 10, 10))
    assert s["without_review_scores"] == 1 and s["unscored"] == 1
    assert s["by_score"]["10"]["early"] == {"n": 2, "floor": 7.5, "both8": 0.5}
    assert "unscored" not in s["by_score"]
    assert fd.render(s)                                                 # renders with gaps


def test_family_classifier():
    assert fd.family("Forward Deployed Product Manager") == "FDE"
    assert fd.family("Forward-Deployed Engineer, AI") == "FDE"
    assert fd.family("Technical Product Owner") == "PO"
    assert fd.family("Senior Product Manager, Payments") == "PM"
    assert fd.family("Product Analyst") == "other"
    assert fd.family("") == "other"


def test_cron_reused_from_usage_deltas():
    assert fd.cron_hours() == CRON
