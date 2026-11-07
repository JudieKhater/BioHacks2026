"""Tests for the parts of the backend that don't need FastAPI installed."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import config  # noqa: E402
config.GEMINI_API_KEY = ""  # force offline fallbacks in tests

import consent  # noqa: E402
import recap_ai  # noqa: E402
from db import SQLiteStore  # noqa: E402


def test_sqlite_store_roundtrip(tmp_path):
    s = SQLiteStore(str(tmp_path / "t.db"))
    now = time.time()
    for i in range(5):
        s.insert_features(now - i * 5, {"wpm": 150 - i, "loudness_db": -20, "breath_group_words": 10,
                                        "effort": None, "speaking_s": 8, "confidence": 0.9,
                                        "score": 10 * i, "captions_on": 0})
    s.insert_emst(now, 25, 48.0, 40.0)
    t = s.trends(3)
    assert len(t) == 1
    assert t[0]["emst_reps"] == 25
    assert t[0]["speaking_min"] > 0
    assert len(s.today_curve()) == 5
    s.save_recap(now, "2026-11-07", ["Mitochondria make ATP for the cell."])
    assert s.latest_recap()["sentences"][0].startswith("Mitochondria")


def test_recap_is_verbatim_offline():
    lines = ["Today we looked at how mitochondria make ATP through cellular respiration.",
             "Okay settle down please.",
             "Remember that osmosis moves water across a membrane toward higher solute concentration."]
    out = recap_ai.draft_recap(lines, ["mitochondria", "ATP", "osmosis", "membrane"], k=2)
    assert out["source"] == "offline"
    all_sentences = set(out["all"])
    assert all(s in all_sentences for s in out["sentences"]), "recap must be his exact words"
    assert "Okay settle down please." not in out["sentences"]


def test_clinic_summary_offline():
    out = recap_ai.clinic_summary([{"day": "2026-11-06", "morning_wpm": 160, "afternoon_wpm": 120,
                                    "support_pct": 40, "emst_reps": 25}])
    assert out["source"] == "offline" and "wpm" in out["text"]


def test_consent_hash_has_no_health_data():
    h = consent.receipt_hash(1.0, "ALS clinic", True)
    assert len(h) == 64
    assert consent.anchor_on_solana(h) is None  # disabled by default
