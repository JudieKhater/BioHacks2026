"""Tests for stutter cleanup, word prediction, lesson context, symptoms and guidance (offline)."""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import config  # noqa: E402
config.GEMINI_API_KEY = ""  # force offline paths in tests

import gemini  # noqa: E402
import predictor  # noqa: E402
import speech_symptoms as S  # noqa: E402

CONTEXT = Path(__file__).resolve().parent.parent / "context"


def lib():
    lb = predictor.ContextLibrary()
    assert lb.load_dir(CONTEXT) >= 1
    return lb


# ------------------------------------------------------------ disfluencies
def test_clean_removes_stutters_but_keeps_his_words():
    text, d = S.clean("So the oculo- the oculomotor n-nerve controls the")
    assert text == "So the oculomotor nerve controls the"
    assert d.part_words == 2 and d.repetitions == 1
    assert d.trailing


def test_clean_fillers_and_phrase_repeats():
    text, d = S.clean("um the pressure damages the the outer fibers.")
    assert text == "the pressure damages the outer fibers."
    assert d.fillers == 1 and d.repetitions == 1
    assert not d.trailing


def test_clean_keeps_punctuation():
    text, d = S.clean("Okay, um, today we're looking at the oculomotor nerve, cranial nerve three.")
    assert text == "Okay, today we're looking at the oculomotor nerve, cranial nerve three."
    assert d.fillers == 1
    text, _ = S.clean("caused by aneurysms, tumours or her-herniation.")
    assert text == "caused by aneurysms, tumours or herniation."


def test_clean_never_adds_words():
    raw = "Mitochondria make ATP for the cell."
    text, d = S.clean(raw)
    assert text == raw and d.events == 0


def test_is_incomplete():
    assert S.is_incomplete("Loss of blood flow typically results from")
    assert S.is_incomplete("and they have double vi-")
    assert not S.is_incomplete("The nerve controls eye movement.")


def test_monitor_rates():
    m = S.SymptomMonitor()
    for raw in ["the the nerve controls", "um so it is", "Plain sentence with eight words in it here."]:
        m.add(S.clean(raw)[1])
    s = m.summary()
    assert s["utterances"] == 3 and s["disfluencies_per_100_words"] > 0
    assert s["trailing_pct"] == 67


# ------------------------------------------------------------- prediction
def test_library_loads_terms_and_skips_bibliography():
    lb = lib()
    terms = [t.lower() for t in lb.all_terms()]
    assert "oculomotor nerve" in terms and "medial rectus muscle" in terms
    assert not any("alamy" in s.lower() for s in lb.sentences)


def test_offline_predicts_stalled_sentence_from_context():
    p = predictor.offline_predict("So the oculo- the oculomotor n-nerve controls the", lib(), stalled=True)
    assert p.clean == "So the oculomotor nerve controls the"
    assert p.predicted.startswith("medial rectus")
    assert p.source == "offline" and p.confidence >= config.PREDICT_MIN_CONFIDENCE


def test_offline_finishes_half_said_word():
    p = predictor.offline_predict("the nerve controls the medi", lib(), stalled=True)
    assert p.clean == "the nerve controls the"
    assert p.predicted.split()[0] == "medial"


def test_no_prediction_for_complete_sentence():
    p = predictor.offline_predict("Good morning everyone.", lib())
    assert p.predicted == "" and p.source == "none"


def test_prediction_is_separate_from_spoken_words():
    raw = "Loss of blood flow typically results from"
    p = predictor.offline_predict(raw, lib(), stalled=True)
    assert p.clean == raw            # his words are untouched
    assert p.predicted and p.predicted not in p.clean


def test_strip_overlap():
    assert predictor._strip_overlap("the nerve controls the", "controls the medial rectus") == "medial rectus"
    assert predictor._strip_overlap("the nerve controls the", "medial rectus") == "medial rectus"


def test_gemini_disabled_returns_none():
    assert not gemini.enabled()
    assert gemini.generate("hi") is None
    lb = lib()
    pred = predictor.offline_predict("controls the", lb, stalled=True)
    assert predictor.gemini_predict("controls the", pred, lb, []) is None


def test_context_hint_and_relevant_passages():
    lb = lib()
    hint = predictor.context_hint("Today we look at the oculomotor nerve.", lb, None, [])
    assert hint and hint["term"].lower().startswith("oculomotor nerve")
    assert predictor.context_hint("Today we look at the oculomotor nerve.", lb, None, [hint["term"]]) is None
    assert any("aneurysm" in p.lower() for p in lb.relevant("what pressure causes a blown pupil", 3))


def test_offline_notes():
    n = predictor.build_notes(["The oculomotor nerve controls the medial rectus muscle.",
                               "Pressure from an aneurysm can cause a blown pupil."], lib())
    assert n["source"] == "offline" and n["topic"]
    assert any("medial rectus" in k["term"].lower() for k in n["key_terms"])


def test_learn_from_his_own_sentences():
    lb = predictor.ContextLibrary()
    lb.learn("Photosynthesis happens in the chloroplast.")
    p = predictor.offline_predict("photosynthesis happens in the", lb, stalled=True)
    assert p.predicted == "chloroplast"


# ------------------------------------------------- mic phrase cutting
def test_phrase_cutter_splits_at_pauses():
    import numpy as np
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
    import audio_client as A

    rng = np.random.default_rng(1)
    quiet = lambda s: rng.normal(0, 30, int(16000 * s)).astype(np.int16)           # noqa: E731
    t = np.arange(int(16000 * 1.5)) / 16000
    talk = (3000 * np.sin(2 * np.pi * 180 * t) * (1 + np.sin(2 * np.pi * 4 * t)) / 2).astype(np.int16)
    click = (np.ones(1600) * 3000).astype(np.int16)                                   # 0.1 s: too short
    sig = np.concatenate([quiet(2), talk, quiet(1.5), click, quiet(1.5), talk, quiet(1.5)])
    cutter = A.PhraseCutter(pause_s=0.9)
    phrases = [p for i in range(0, len(sig) - 1599, 1600) if (p := cutter.add(sig[i:i + 1600]))]
    assert len(phrases) == 2                      # two phrases, the click ignored
    assert all(1.2 <= p[3] <= 1.8 for p in phrases)


def test_transcribe_without_key_is_none():
    assert gemini.transcribe(bytes(3200)) is None


# -------------------------------------------------- subsystems and guidance
SNAP = {"battery": 25, "components": {"wpm": 0.9, "loudness_db": 0.8, "breath_group": 0.7},
        "baseline": {"wpm": 165, "loudness_db": -20, "breath_group_words": 12}}


def test_subsystems_flag_slow_quiet_speech():
    subs = {s["key"]: s for s in S.subsystems(SNAP, {"wpm": 115, "loudness_db": -25, "breath_group_words": 7,
                                                     "pause_pct": 38}, {"disfluencies_per_100_words": 2})}
    assert subs["articulatory"]["level"] == "alert"     # under 120 wpm
    assert subs["phonatory"]["level"] == "alert"
    assert subs["respiratory"]["level"] in ("watch", "alert")
    assert subs["resonatory"]["level"] == "unknown"     # needs nasalance hardware


def test_guidance_is_energy_conserving():
    tips = S.guidance(SNAP, {"wpm": 115, "pause_pct": 40}, {"disfluencies_per_100_words": 6, "trailing_pct": 40})
    titles = [t["title"] for t in tips]
    assert titles[0] == "Voice rest" and "Keep captions on" in titles
    text = " ".join(t["text"].lower() for t in tips)
    assert "tongue" not in text and "strengthen" not in text


def test_guidance_when_fresh():
    tips = S.guidance({"battery": 95, "components": {}}, {"wpm": 160}, {})
    assert tips[0]["level"] == "ok"


# ---------------------------------------------------------- decline model
def _trend(days, start_wpm, per_month):
    d0 = date(2026, 1, 1)
    return [{"day": (d0 + timedelta(days=i)).isoformat(), "morning_wpm": start_wpm + per_month * i / 30.44}
            for i in range(days)]


def test_projection_matches_bulbar_rate():
    p = S.project_decline(_trend(60, 150, -4.16))
    assert p["ok"] and abs(p["slope_wpm_per_month"] + 4.16) < 0.05
    assert "bulbar" in p["comparison"]
    assert 5 < p["months_to_120"] < 7.5      # ~142 wpm now, 22 wpm above 120 at 4.16/month


def test_projection_stable_and_too_little_data():
    assert S.project_decline(_trend(30, 160, 0))["comparison"] == "stable"
    assert S.project_decline(_trend(30, 160, 0))["months_to_120"] is None
    assert not S.project_decline(_trend(3, 160, -2))["ok"]
