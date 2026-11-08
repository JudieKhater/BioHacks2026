"""
Teaching sessions: what happened between "Start session" and "End session".

A SessionRecorder collects the numbers from one session (feature windows,
stutter counts, predictions used). At the end it builds a report:

  - measures at the start vs the end of the session (fatigue shows as change),
  - comparison with his previous sessions (the trend over time),
  - guidance for right now, and a breathing plan (breathing.py),
  - flags worth raising with his clinic.

Only numbers and generated guidance are stored. No audio, no transcript.
Pure Python, so it is easy to test and the simulator can backfill history.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import breathing
import speech_symptoms as symptoms

MEASURES = ("wpm", "loudness_db", "breath_group_words", "pause_pct")
# For each measure: is a higher number better? (pause_pct: fewer pauses is better)
HIGHER_IS_BETTER = {"wpm": True, "loudness_db": True, "breath_group_words": True, "pause_pct": False,
                    "stumbles_per_100_words": False, "battery_end": True}
LABELS = {"wpm": "Speaking rate (wpm)", "loudness_db": "Loudness (dBFS)",
          "breath_group_words": "Words per breath", "pause_pct": "Pauses (% of speaking time)",
          "stumbles_per_100_words": "Stumbles per 100 words", "battery_end": "Speech battery at the end (%)"}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


@dataclass
class SessionRecorder:
    id: str
    started: float
    battery_start: int = 100
    windows: list[dict] = field(default_factory=list)
    monitor: symptoms.SymptomMonitor = field(default_factory=lambda: symptoms.SymptomMonitor(window_s=10 ** 9))
    predictions: int = 0

    @classmethod
    def start(cls, now: Optional[float] = None, battery: int = 100) -> "SessionRecorder":
        now = now or time.time()
        return cls(id=f"s{int(now * 1000)}", started=now, battery_start=battery)

    def add_window(self, ts: float, f: dict, score: float, battery: int) -> None:
        """One 5-second feature window (only windows with speech count)."""
        if (f.get("speaking_s") or 0) < 1.0:
            return
        self.windows.append({"ts": ts, **{k: f.get(k) for k in MEASURES}, "effort": f.get("effort"),
                             "speaking_s": f.get("speaking_s") or 0.0, "score": score, "battery": battery})

    def add_utterance(self, d: symptoms.Disfluency, ts: Optional[float] = None) -> None:
        self.monitor.add(d, ts or time.time())

    # ------------------------------------------------------------- report
    def _thirds(self, key: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
        vals = [w[key] for w in self.windows if w.get(key) is not None]
        if not vals:
            return None, None, None
        n = max(1, len(vals) // 3)
        return _mean(vals), _mean(vals[:n]), _mean(vals[-n:])

    def measures(self) -> dict:
        out = {}
        for k in MEASURES:
            mean, first, last = self._thirds(k)
            out[k] = {"mean": mean, "start": first, "end": last,
                      "change": None if first is None or last is None else round(last - first, 2)}
        return out

    def report(self, ended: float, snapshot: dict, previous: list[dict],
               lesson_sentences: Optional[list[str]] = None) -> dict:
        """Build the end-of-session report. `previous`: earlier reports, newest first."""
        m = self.measures()
        disfl = self.monitor.summary(now=ended)
        wpms = [w["wpm"] for w in self.windows if w.get("wpm") is not None]
        under_120 = round(100 * sum(1 for v in wpms if v < symptoms.SPEECH_LOSS_WPM) / len(wpms)) if wpms else None
        batteries = [w["battery"] for w in self.windows]
        battery_end = snapshot.get("battery", batteries[-1] if batteries else self.battery_start)

        # "end of session" view for the subsystem levels and the right-now guidance
        latest = {k: m[k]["end"] for k in MEASURES}
        subsystems = symptoms.subsystems(snapshot, latest, disfl)
        guidance = symptoms.guidance(snapshot, latest, disfl)

        r = {
            "id": self.id, "started": self.started, "ended": ended,
            "duration_min": round((ended - self.started) / 60, 1),
            "speaking_min": round(sum(w["speaking_s"] for w in self.windows) / 60, 1),
            "windows": len(self.windows),
            "empty": not self.windows and not disfl["utterances"],
            "measures": m,
            "stumbles_per_100_words": disfl.get("disfluencies_per_100_words"),
            "disfluency": disfl,
            "predictions_used": self.predictions,
            "battery_start": self.battery_start, "battery_end": battery_end,
            "battery_min": min(batteries) if batteries else battery_end,
            "score_max": round(max((w["score"] for w in self.windows), default=0.0), 1),
            "pct_time_under_120_wpm": under_120,
            "subsystems": subsystems,
            "guidance": guidance,
        }
        r["comparison"] = compare(r, previous)
        r["breathing"] = breathing.plan(r, previous, lesson_sentences or [])
        r["flags"] = breathing.clinician_flags(r, previous)
        return r


def headline(r: dict) -> dict:
    """The few numbers that are trended across sessions."""
    m = r.get("measures") or {}
    return {
        "wpm": (m.get("wpm") or {}).get("mean"),
        "loudness_db": (m.get("loudness_db") or {}).get("mean"),
        "breath_group_words": (m.get("breath_group_words") or {}).get("mean"),
        "pause_pct": (m.get("pause_pct") or {}).get("mean"),
        "stumbles_per_100_words": r.get("stumbles_per_100_words"),
        "battery_end": r.get("battery_end"),
    }


def compare(r: dict, previous: list[dict], n: int = 5) -> list[dict]:
    """This session vs the average of up to `n` earlier non-empty sessions."""
    prev = [headline(p) for p in previous if not p.get("empty")][:n]
    if not prev:
        return []
    now = headline(r)
    out = []
    for k, v in now.items():
        vals = [p[k] for p in prev if p.get(k) is not None]
        if v is None or not vals:
            continue
        avg = sum(vals) / len(vals)
        delta = v - avg
        # "meaningful" change: 5 % of the average (1 point for percentages and small counts)
        thresh = max(abs(avg) * 0.05, 1.0 if k in ("pause_pct", "stumbles_per_100_words", "battery_end") else 0.3)
        if abs(delta) < thresh:
            verdict = "same"
        else:
            better = (delta > 0) == HIGHER_IS_BETTER[k]
            verdict = "better" if better else "worse"
        out.append({"key": k, "label": LABELS[k], "now": round(v, 1), "usual": round(avg, 1),
                    "delta": round(delta, 1), "verdict": verdict, "sessions": len(vals)})
    return out


def summary_row(r: dict) -> dict:
    """Compact row for the sessions list and trend charts."""
    return {"id": r["id"], "started": r["started"], "ended": r["ended"], "duration_min": r.get("duration_min"),
            "speaking_min": r.get("speaking_min"), "empty": r.get("empty", False),
            "readiness": (r.get("breathing") or {}).get("readiness"), **headline(r)}


def offline_summary(r: dict) -> str:
    if r.get("empty"):
        return "No speech was picked up during this session, so there's nothing to report."
    m = r["measures"]
    parts = [f"You spoke for about {r['speaking_min']} minutes over {r['duration_min']} minutes."]
    w = m["wpm"]
    if w["start"] is not None and w["end"] is not None:
        parts.append(f"Your speaking rate went from about {w['start']:.0f} to {w['end']:.0f} words per minute.")
    b = m["breath_group_words"]
    if b["start"] is not None and b["end"] is not None:
        parts.append(f"Words per breath went from {b['start']:.1f} to {b['end']:.1f}.")
    parts.append(f"Your speech battery finished at {r['battery_end']}%.")
    worse = [c["label"].split(" (")[0].lower() for c in r.get("comparison", []) if c["verdict"] == "worse"]
    better = [c["label"].split(" (")[0].lower() for c in r.get("comparison", []) if c["verdict"] == "better"]

    def listing(items):
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    if worse:
        parts.append(f"Compared with your recent sessions, your {listing(worse)} "
                     f"{'was' if len(worse) == 1 else 'were'} weaker today.")
    if better:
        parts.append(f"Your {listing(better)} {'was' if len(better) == 1 else 'were'} better than usual.")
    return " ".join(parts)
