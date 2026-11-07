"""
Audio feature extraction for Recap. Needs only numpy.

Everything works on mono float audio in the range -1..1.

What each feature means
-----------------------
- loudness_db:        mean RMS level of the *speech* frames, in dBFS (0 = max).
                      Falling loudness through the day = vocal fatigue.
- speaking_s:         seconds of speech in the window (adds up to "vocal dose").
- breath groups:      runs of speech separated by pauses >= 250 ms. People with
                      diaphragm weakness say fewer words per breath, so this is
                      an indirect, daily breathing measure.
- wpm:                words per minute while speaking. Best from ASR word
                      timestamps (ElevenLabs Scribe); otherwise estimated from
                      syllable peaks in the loudness envelope (labelled estimate).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

FRAME_MS = 30
BREATH_PAUSE_MS = 250       # pause long enough to count as a breath / phrase break
SYLLABLES_PER_WORD = 1.5    # English conversational average, rough


@dataclass
class Segment:
    start: float  # seconds
    end: float

    @property
    def dur(self) -> float:
        return self.end - self.start


def frame_rms_db(x: np.ndarray, sr: int, frame_ms: int = FRAME_MS) -> np.ndarray:
    """RMS level per frame in dBFS."""
    n = max(1, int(sr * frame_ms / 1000))
    usable = len(x) - (len(x) % n)
    if usable <= 0:
        return np.array([])
    frames = x[:usable].reshape(-1, n)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-10))


def energy_vad(db: np.ndarray, margin_db: float = 12.0, floor_db: float = -55.0) -> np.ndarray:
    """
    Simple adaptive voice activity detection: a frame is speech if it is
    `margin_db` above the noise floor (10th percentile of the window).
    Good enough for a close-talk lapel mic; swap for Silero VAD if needed.
    """
    if db.size == 0:
        return np.zeros(0, dtype=bool)
    noise = np.percentile(db, 10)
    thresh = max(noise + margin_db, floor_db)
    speech = db > thresh
    # fill tiny gaps (< 90 ms) so a single quiet frame doesn't split a word
    speech = _close_gaps(speech, max_gap=3)
    return speech


def _close_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    m = mask.copy()
    i, n = 0, len(m)
    while i < n:
        if not m[i]:
            j = i
            while j < n and not m[j]:
                j += 1
            if 0 < i and j < n and (j - i) <= max_gap:
                m[i:j] = True
            i = j
        else:
            i += 1
    return m


def speech_segments(mask: np.ndarray, frame_ms: int = FRAME_MS,
                    pause_ms: int = BREATH_PAUSE_MS, min_seg_ms: int = 150) -> list[Segment]:
    """Group speech frames into breath groups split by pauses >= pause_ms."""
    segs: list[Segment] = []
    f = frame_ms / 1000.0
    pause_frames = max(1, pause_ms // frame_ms)
    start = None
    silent_run = 0
    for i, s in enumerate(mask):
        if s:
            if start is None:
                start = i
            silent_run = 0
        elif start is not None:
            silent_run += 1
            if silent_run >= pause_frames:
                end = i - silent_run + 1
                segs.append(Segment(start * f, end * f))
                start, silent_run = None, 0
    if start is not None:
        end = len(mask) - silent_run
        segs.append(Segment(start * f, end * f))
    return [s for s in segs if s.dur * 1000 >= min_seg_ms]


def estimate_syllables(db: np.ndarray, mask: np.ndarray) -> int:
    """Count loudness peaks inside speech: a rough syllable-nucleus count."""
    if db.size < 3:
        return 0
    env = np.convolve(db, np.ones(3) / 3, mode="same")
    count, last = 0, -10
    min_dist = 4  # frames (120 ms) between syllables
    for i in range(1, len(env) - 1):
        if mask[i] and env[i] > env[i - 1] and env[i] >= env[i + 1] and i - last >= min_dist:
            # require a dip of 2 dB since the last peak so a flat vowel isn't counted twice
            if last < 0 or env[last:i].min() < env[i] - 2.0:
                count += 1
                last = i
    return count


def words_from_timestamps(words: Sequence[dict]) -> list[dict]:
    """Keep only real words from a Scribe `words` list (drop spacing tokens)."""
    return [w for w in (words or []) if w.get("type", "word") == "word" and w.get("text", "").strip()]


def wpm_from_words(words: Sequence[dict], speaking_s: float) -> Optional[float]:
    if speaking_s < 1.0 or not words:
        return None
    return 60.0 * len(words) / speaking_s


def confidence_from_words(words: Sequence[dict]) -> Optional[float]:
    """Mean word probability from Scribe log-probabilities (logprob -> prob)."""
    lps = [w["logprob"] for w in words if isinstance(w.get("logprob"), (int, float))]
    if not lps:
        return None
    return float(np.mean([math.exp(lp) for lp in lps]))


def extract(x: np.ndarray, sr: int, asr_words: Optional[Sequence[dict]] = None) -> dict:
    """
    Compute all audio features for one window.
    `asr_words`: Scribe word objects whose times fall in this window (optional).
    """
    db = frame_rms_db(x, sr)
    mask = energy_vad(db)
    segs = speech_segments(mask)
    speaking_s = float(sum(s.dur for s in segs))
    loudness = float(db[mask].mean()) if mask.any() else None

    words = words_from_timestamps(asr_words) if asr_words else []
    if words:
        wpm = wpm_from_words(words, speaking_s)
        wpm_source = "asr"
    else:
        syl = estimate_syllables(db, mask)
        wpm = (60.0 * syl / SYLLABLES_PER_WORD / speaking_s) if speaking_s >= 1.0 else None
        wpm_source = "estimate"

    bg_words = None
    if segs and wpm:
        # words per breath group = rate x mean speaking time per group
        mean_seg = speaking_s / len(segs)
        bg_words = wpm / 60.0 * mean_seg

    return {
        "speaking_s": round(speaking_s, 2),
        "loudness_db": None if loudness is None else round(loudness, 2),
        "wpm": None if wpm is None else round(wpm, 1),
        "wpm_source": wpm_source,
        "breath_groups": len(segs),
        "breath_group_words": None if bg_words is None else round(bg_words, 2),
        "confidence": confidence_from_words(words) if words else None,
    }
