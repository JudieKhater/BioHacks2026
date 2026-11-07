import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fatigue import FatigueEngine, FeatureWindow  # noqa: E402
import features  # noqa: E402


def fresh_engine():
    e = FatigueEngine(min_on_seconds=60)
    e.set_baseline(wpm=165, loudness_db=-20, breath_group_words=12, effort_per_output=10.0)
    return e


def win(t, wpm, db, bg, effort=None):
    return FeatureWindow(t=t, wpm=wpm, loudness_db=db, breath_group_words=bg,
                         effort=effort, speaking_s=8)


def test_fresh_speech_scores_low():
    e = fresh_engine()
    for i in range(10):
        e.update(win(i * 5, 165, -20, 12))
    assert e.state.score < 5
    assert not e.state.captions_on
    assert e.battery > 95


def test_alex_afternoon_turns_captions_on():
    # 118 wpm, 4 dB quieter, shorter breath groups: Alex at 4 pm
    e = fresh_engine()
    for i in range(15):
        e.update(win(i * 5, 118, -24, 8))
    assert e.state.score > 55
    assert e.state.captions_on
    assert e.state.support_flag  # under 125 wpm


def test_hysteresis_and_min_on_time():
    e = fresh_engine()
    e.min_on_seconds = 300
    t = 0
    for _ in range(15):
        e.update(win(t, 118, -24, 8)); t += 5
    assert e.state.captions_on
    # recovers immediately, but captions must stay on for min_on_seconds
    e.update(win(t, 165, -20, 12)); t += 5
    for _ in range(3):
        e.update(win(t, 165, -20, 12)); t += 5
    assert e.state.captions_on, "must not flicker off before min on-time"
    for _ in range(80):
        e.update(win(t, 165, -20, 12)); t += 5
    assert not e.state.captions_on


def test_mode_controls_override_score():
    e = fresh_engine()
    e.set_mode("on", 0)
    assert e.state.captions_on
    for i in range(15):
        e.update(win(i * 5, 118, -24, 8))
    e.set_mode("off", 100)
    assert not e.state.captions_on


def test_effort_rise_detected():
    e = fresh_engine()
    out = 10 ** (-20 / 20)
    for i in range(15):
        # same sound output, 60 % more effort
        e.update(win(i * 5, 165, -20, 12, effort=10.0 * out * 1.6))
    assert e.state.components["effort"] == 1.0


def test_calibration_from_windows():
    e = FatigueEngine(calibration_windows=3)
    e.start_calibration()
    for i in range(3):
        e.update(win(i, 160 + i, -21, 11))
    assert not e.state.calibrating
    assert abs(e.baseline.wpm - 161) < 1e-6


def test_silence_ignored():
    e = fresh_engine()
    e.update(FeatureWindow(t=0, speaking_s=0.2))
    assert e.state.score == 0


# ------------------------------------------------------------- features
def synth_speech(sr=16000, seconds=10, syll_rate=4.0, groups=4, level=0.1, seed=0):
    """Amplitude-modulated noise bursts: syllables inside breath groups."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * seconds)) / sr
    env = np.zeros_like(t)
    group_len = seconds / groups
    for g in range(groups):
        start = g * group_len
        speak_end = start + group_len * 0.75  # 25 % pause between groups
        mask = (t >= start) & (t < speak_end)
        env[mask] = 0.5 * (1 - np.cos(2 * np.pi * syll_rate * (t[mask] - start)))
    noise = rng.normal(0, 1, t.size)
    floor = rng.normal(0, 0.001, t.size)
    return (level * env * noise + floor).astype(np.float32)


def test_features_find_breath_groups_and_loudness():
    x = synth_speech()
    f = features.extract(x, 16000)
    assert f["breath_groups"] == 4
    assert 6 < f["speaking_s"] < 9
    assert f["loudness_db"] is not None
    assert f["wpm"] is not None and f["wpm_source"] == "estimate"


def test_quieter_speech_is_lower_db():
    loud = features.extract(synth_speech(level=0.1), 16000)
    quiet = features.extract(synth_speech(level=0.05), 16000)
    assert quiet["loudness_db"] < loud["loudness_db"] - 4


def test_asr_words_give_wpm_and_confidence():
    x = synth_speech()
    words = [{"text": "cell", "type": "word", "logprob": -0.1}] * 20 + [{"text": " ", "type": "spacing"}]
    f = features.extract(x, 16000, asr_words=words)
    assert f["wpm_source"] == "asr"
    assert 0.85 < f["confidence"] < 0.95
