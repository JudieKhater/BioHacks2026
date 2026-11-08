"""Session reports, breathing plans and session trends (offline, no server)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import config  # noqa: E402
config.GEMINI_API_KEY = ""

import breathing  # noqa: E402
import recap_ai  # noqa: E402
import sessions  # noqa: E402
import speech_symptoms as S  # noqa: E402
from db import SQLiteStore  # noqa: E402

LESSON = ["The oculomotor nerve is responsible for eye movement, eyelid elevation, pupil constriction, and focusing.",
          "Increased pressure is typically caused by aneurysms, tumours, or herniation and damages the outer nerve fibers.",
          "Loss of blood flow typically results from conditions such as diabetes and high blood pressure."]


def run_session(tired: bool, t0: float = 1_800_000_000.0, n: int = 12, wpm0=165.0, bg0=12.0):
    rec = sessions.SessionRecorder.start(now=t0, battery=90)
    for i in range(n):
        f = i / (n - 1)
        drop = f if tired else 0.05 * f
        rec.add_window(t0 + 5 * i, {"wpm": wpm0 - 50 * drop, "loudness_db": -20 - 5 * drop,
                                    "breath_group_words": bg0 - 6 * drop, "pause_pct": 18 + 18 * drop,
                                    "speaking_s": 8}, score=80 * drop, battery=int(90 - 70 * drop))
        rec.add_utterance(S.clean("the the nerve controls the" if tired and i % 2 else "The nerve controls eye movement.")[1])
    battery = 20 if tired else 88
    snap = {"battery": battery, "components": {"breath_group": 0.9 if tired else 0.05},
            "baseline": {"wpm": 165, "breath_group_words": 12}}
    return rec.report(t0 + 5 * n, snap, [], LESSON)


def test_report_compares_start_and_end():
    r = run_session(tired=True)
    m = r["measures"]
    assert m["wpm"]["start"] > m["wpm"]["end"]
    assert m["breath_group_words"]["change"] < 0
    assert r["pct_time_under_120_wpm"] > 0
    assert r["stumbles_per_100_words"] > 0
    assert not r["empty"] and r["guidance"]


def test_tired_session_means_rest_first():
    r = run_session(tired=True)
    p = r["breathing"]
    assert p["readiness"] == "recover"
    keys = [e["key"] for e in p["exercises"]]
    assert keys[0] == "recovery" and "ah" not in keys          # no extra voice work when tired
    phr = next(e for e in p["exercises"] if e["key"] == "phrasing")
    assert "tomorrow" in phr["when"].lower()
    emst = next(e for e in p["exercises"] if e["key"] == "emst")
    assert "skip" in emst["dose"].lower()


def test_fresh_session_means_normal_practice():
    r = run_session(tired=False)
    p = r["breathing"]
    assert p["readiness"] == "standard"
    assert "ah" in [e["key"] for e in p["exercises"]]


def test_phrase_length_follows_his_breath_groups():
    r = run_session(tired=True)                     # 12 -> ~7 words per breath over the session
    end = r["measures"]["breath_group_words"]["end"]  # average of the last third of the session
    assert r["breathing"]["phrase_words"] == round(end) == 7
    phrases = next(e for e in r["breathing"]["exercises"] if e["key"] == "phrasing")["passage"]
    assert phrases and all(len(p.split()) <= 7 + 2 for p in phrases)
    assert "oculomotor" in " ".join(phrases).lower()  # practice uses the lesson material


def test_passage_fallback_without_lesson():
    phrases = breathing.practice_passage([], 5)
    assert "chloroplasts" in " ".join(phrases)


def test_every_exercise_cites_a_source_and_avoids_strengthening():
    for tired in (True, False):
        p = run_session(tired=tired)["breathing"]
        for e in p["exercises"]:
            assert e["source"] and e["why"]
            text = (e["why"] + " ".join(e["steps"])).lower()
            assert "tongue" not in text and "lip strength" not in text
        assert "stop if" in p["safety"].lower()


def test_comparison_and_trend_flags():
    old = [run_session(tired=False, t0=1_800_000_000.0 + 86400 * d, wpm0=165, bg0=12) for d in range(4)]
    previous = list(reversed(old))                  # newest first
    rec = sessions.SessionRecorder.start(now=1_800_900_000.0, battery=90)
    for i in range(12):
        rec.add_window(1_800_900_000.0 + 5 * i, {"wpm": 112, "loudness_db": -24, "breath_group_words": 7,
                                                 "pause_pct": 35, "speaking_s": 8}, score=60, battery=40)
    r = rec.report(1_800_900_100.0, {"battery": 40, "components": {}}, previous, LESSON)
    verdicts = {c["key"]: c["verdict"] for c in r["comparison"]}
    assert verdicts["wpm"] == "worse" and verdicts["pause_pct"] == "worse"
    texts = " ".join(f["text"] for f in r["flags"])
    assert "under 120 wpm" in texts and "Words per breath has fallen" in texts


def test_empty_session():
    rec = sessions.SessionRecorder.start(now=1_800_000_000.0)
    r = rec.report(1_800_000_060.0, {"battery": 100, "components": {}}, [], LESSON)
    assert r["empty"] and r["breathing"]["exercises"] == [] and r["flags"] == []
    assert recap_ai.session_summary(r)["source"] == "offline"


def test_offline_summary_mentions_change():
    text = recap_ai.session_summary(run_session(tired=True))["text"]
    assert "words per minute" in text and "battery" in text


def test_store_roundtrip_and_rows(tmp_path):
    st = SQLiteStore(str(tmp_path / "s.db"))
    a = run_session(tired=False, t0=1_800_000_000.0)
    b = run_session(tired=True, t0=1_800_086_400.0)
    st.insert_session(a)
    st.insert_session(b)
    got = st.list_sessions(10)
    assert [g["id"] for g in got] == [b["id"], a["id"]]       # newest first
    assert st.get_session(a["id"])["breathing"]["readiness"] == "standard"
    row = sessions.summary_row(got[0])
    assert row["readiness"] == "recover" and row["wpm"] is not None
