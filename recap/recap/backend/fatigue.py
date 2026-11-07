"""
Fatigue engine for Recap.

Pure Python, no web framework, so it can be unit-tested and reused by the
simulator. Every number in here is a starting point a clinician would tune.

How the score works
-------------------
1. A *baseline* is captured while Alex is fresh (e.g. first class of the day).
2. Each new feature window is compared to the baseline, giving four "deficits",
   each scaled to 0..1:
     - speech rate drop    (full scale = 30 % drop; Alex's own 165 -> 118 wpm is 28 %)
     - loudness drop        (full scale = 6 dB quieter)
     - breath-group drop    (full scale = 40 % fewer words per breath)
     - effort rise          (throat-sensor effort per unit of sound, full scale = +50 %)
3. The available deficits are averaged with weights, x100, then smoothed with
   an exponential moving average so one bad window doesn't trigger anything.
4. Captions use hysteresis: they turn ON at a high threshold and only turn OFF
   below a lower one, after a minimum on-time, so they never flicker mid-sentence.

Alex is always in control: mode "off" never shows captions, "on" always does,
and only "auto" lets the score decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# Thresholds a speech-language pathologist might use as a cue that someone
# needs communication support (speech rate under ~125 wpm, intelligibility
# under ~90 %). We surface them as flags for the clinic, not as diagnoses.
SUPPORT_WPM_THRESHOLD = 125.0

# Full-scale values: the deficit that maps to 1.0 (100 % of that component).
FULL_SCALE = {
    "wpm": 0.30,          # 30 % slower than baseline
    "loudness_db": 6.0,   # 6 dB quieter than baseline
    "breath_group": 0.40, # 40 % fewer words per breath
    "effort": 0.50,       # 50 % more effort per unit of output
}

WEIGHTS = {"wpm": 0.35, "loudness_db": 0.25, "breath_group": 0.20, "effort": 0.20}


@dataclass
class FeatureWindow:
    """One window of features (typically 10 s of audio, sent every 5 s)."""
    t: float                                  # unix time, seconds
    wpm: Optional[float] = None               # words per minute while speaking
    loudness_db: Optional[float] = None       # mean speech loudness, dBFS
    breath_group_words: Optional[float] = None  # mean words between breath pauses
    effort: Optional[float] = None            # throat sensor energy (EMG or accel), arbitrary units
    speaking_s: float = 0.0                   # seconds of speech in this window
    confidence: Optional[float] = None        # mean ASR word confidence 0..1, if known


@dataclass
class Baseline:
    wpm: Optional[float] = None
    loudness_db: Optional[float] = None
    breath_group_words: Optional[float] = None
    effort_per_output: Optional[float] = None
    n: int = 0


@dataclass
class EngineState:
    mode: str = "auto"              # "auto" | "on" | "off" -- Alex decides
    score: float = 0.0              # smoothed fatigue score 0..100
    raw_score: float = 0.0
    captions_on: bool = False
    captions_on_since: Optional[float] = None
    speaking_s_today: float = 0.0
    calibrating: bool = False
    components: dict = field(default_factory=dict)
    support_flag: bool = False      # current wpm under the support threshold


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def output_level(loudness_db: Optional[float]) -> Optional[float]:
    """Convert dBFS to a linear amplitude so effort can be divided by output."""
    if loudness_db is None:
        return None
    return 10 ** (loudness_db / 20.0)


class FatigueEngine:
    def __init__(
        self,
        on_threshold: float = 55.0,
        off_threshold: float = 40.0,
        min_on_seconds: float = 120.0,
        smoothing: float = 0.3,
        calibration_windows: int = 6,
    ):
        assert off_threshold < on_threshold, "hysteresis needs off < on"
        self.on_threshold = on_threshold
        self.off_threshold = off_threshold
        self.min_on_seconds = min_on_seconds
        self.alpha = smoothing
        self.calibration_windows = calibration_windows
        self.baseline = Baseline()
        self.state = EngineState()
        self._cal_buffer: list[FeatureWindow] = []

    # ---------------------------------------------------------------- baseline
    def start_calibration(self) -> None:
        """Begin capturing a fresh baseline from the next N speaking windows."""
        self._cal_buffer = []
        self.state.calibrating = True

    def set_baseline(self, **values) -> None:
        """Set a baseline directly (used by the simulator and for demos)."""
        for k, v in values.items():
            setattr(self.baseline, k, v)
        self.baseline.n = max(self.baseline.n, 1)
        self.state.calibrating = False

    def _finish_calibration(self) -> None:
        def mean(vals):
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None

        buf = self._cal_buffer
        b = Baseline(
            wpm=mean([w.wpm for w in buf]),
            loudness_db=mean([w.loudness_db for w in buf]),
            breath_group_words=mean([w.breath_group_words for w in buf]),
            n=len(buf),
        )
        ratios = []
        for w in buf:
            out = output_level(w.loudness_db)
            if w.effort is not None and out:
                ratios.append(w.effort / out)
        b.effort_per_output = mean(ratios)
        self.baseline = b
        self.state.calibrating = False

    # ------------------------------------------------------------ components
    def components(self, w: FeatureWindow) -> dict:
        """Return each available deficit scaled to 0..1."""
        b = self.baseline
        c: dict[str, float] = {}
        if w.wpm is not None and b.wpm:
            c["wpm"] = _clip01(((b.wpm - w.wpm) / b.wpm) / FULL_SCALE["wpm"])
        if w.loudness_db is not None and b.loudness_db is not None:
            c["loudness_db"] = _clip01((b.loudness_db - w.loudness_db) / FULL_SCALE["loudness_db"])
        if w.breath_group_words is not None and b.breath_group_words:
            drop = (b.breath_group_words - w.breath_group_words) / b.breath_group_words
            c["breath_group"] = _clip01(drop / FULL_SCALE["breath_group"])
        out = output_level(w.loudness_db)
        if w.effort is not None and out and b.effort_per_output:
            rise = (w.effort / out) / b.effort_per_output - 1.0
            c["effort"] = _clip01(rise / FULL_SCALE["effort"])
        return c

    @staticmethod
    def combine(components: dict) -> float:
        if not components:
            return 0.0
        total_w = sum(WEIGHTS[k] for k in components)
        return 100.0 * sum(WEIGHTS[k] * v for k, v in components.items()) / total_w

    # ---------------------------------------------------------------- update
    def update(self, w: FeatureWindow) -> EngineState:
        s = self.state
        s.speaking_s_today += w.speaking_s

        # Ignore near-silent windows: no speech means no new information.
        if w.speaking_s < 1.0:
            self._apply_captions_rule(w.t)
            return s

        if s.calibrating:
            self._cal_buffer.append(w)
            if len(self._cal_buffer) >= self.calibration_windows:
                self._finish_calibration()
            self._apply_captions_rule(w.t)
            return s

        comps = self.components(w)
        s.components = comps
        s.raw_score = self.combine(comps)
        s.score = self.alpha * s.raw_score + (1 - self.alpha) * s.score
        s.support_flag = w.wpm is not None and w.wpm < SUPPORT_WPM_THRESHOLD
        self._apply_captions_rule(w.t)
        return s

    def set_mode(self, mode: str, now: float) -> EngineState:
        if mode not in ("auto", "on", "off"):
            raise ValueError("mode must be auto, on or off")
        self.state.mode = mode
        self._apply_captions_rule(now)
        return self.state

    def _apply_captions_rule(self, now: float) -> None:
        s = self.state
        if s.mode == "on":
            self._set_captions(True, now)
            return
        if s.mode == "off":
            self._set_captions(False, now)
            return
        # auto: hysteresis + minimum on-time
        if not s.captions_on and s.score >= self.on_threshold:
            self._set_captions(True, now)
        elif s.captions_on and s.score <= self.off_threshold:
            on_for = now - (s.captions_on_since or now)
            if on_for >= self.min_on_seconds:
                self._set_captions(False, now)

    def _set_captions(self, on: bool, now: float) -> None:
        s = self.state
        if on and not s.captions_on:
            s.captions_on_since = now
        if not on:
            s.captions_on_since = None
        s.captions_on = on

    # ------------------------------------------------------------- helpers
    @property
    def battery(self) -> int:
        """Speech 'battery' shown to Alex: simply 100 minus the smoothed score."""
        return int(round(max(0.0, min(100.0, 100.0 - self.state.score))))

    def snapshot(self) -> dict:
        d = asdict(self.state)
        d["battery"] = self.battery
        d["baseline"] = asdict(self.baseline)
        d["thresholds"] = {"on": self.on_threshold, "off": self.off_threshold,
                           "support_wpm": SUPPORT_WPM_THRESHOLD}
        return d

    def reset_day(self) -> None:
        self.state.speaking_s_today = 0.0
        self.state.score = 0.0
        self.state.raw_score = 0.0
