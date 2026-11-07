"""
Speech-symptom monitoring and guidance.

Pure Python (no web framework) so it is easy to test. Grounded in two papers
in the repo root:

  Green et al. 2013, "Bulbar and speech motor assessment in ALS: challenges and
  future directions" (Amyotroph Lateral Scler Frontotemporal Degener 14:494).
    - Speech has four subsystems: respiratory, phonatory, articulatory, resonatory.
    - Respiratory weakness -> longer, more frequent pauses (percent pause).
    - Phonatory change (loudness, voice quality) can be the first bulbar sign.
    - Speaking rate drops before intelligibility; below ~100-120 wpm
      intelligibility tends to fall quickly.
    - Resonatory (nasalance) needs a nasal/oral two-mic setup we don't have.

  Eshghi et al. 2022, "Rate of speech decline in individuals with ALS"
  (Sci Rep 12:15713).
    - Functional speech-loss markers: speaking rate < 120 wpm, intelligibility < 85 %.
    - Mean decline: 1.74 wpm/month (spinal onset), 4.16 wpm/month (bulbar onset);
      intelligibility 0.28 vs 1.61 %/month.
    - Median time to < 120 wpm in bulbar onset: 23 months from symptom onset.

Nothing here diagnoses anything. Outputs are flags and plain-language tips for
Alex and his clinicians. Guidance follows ALS speech advice: conserve energy,
pace, use supports. No tongue/lip/jaw strengthening.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Optional

# ---------------------------------------------------------------- constants
SPEECH_LOSS_WPM = 120.0          # Eshghi 2022: functional speech-loss marker
INTELLIGIBILITY_LOSS_PCT = 85.0  # Eshghi 2022
SUPPORT_WPM = 125.0              # early cue, a little above the 120 marker
DECLINE_WPM_PER_MONTH = {"spinal": 1.74, "bulbar": 4.16}
DECLINE_INTELL_PER_MONTH = {"spinal": 0.28, "bulbar": 1.61}

FILLERS = {"um", "uh", "er", "erm", "ah", "eh", "hmm", "mm", "uhm", "umm"}
# Words that leave a sentence hanging if it stops on them.
DANGLING = {
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "by", "from", "into", "onto",
    "and", "or", "but", "so", "because", "that", "which", "who", "is", "are", "was", "were",
    "be", "been", "can", "will", "would", "should", "could", "has", "have", "had", "do", "does",
    "this", "these", "those", "its", "their", "his", "her", "our", "your", "my", "as", "than",
    "called", "controls", "control", "causes", "cause", "makes", "make", "like", "between",
    "through", "via", "about", "very", "more", "most", "some", "both", "each", "every", "if",
    "when", "where", "while", "then", "also", "not", "it's", "there's", "we", "you", "they",
}

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*-?|…")


# ------------------------------------------------------------ disfluencies
@dataclass
class Disfluency:
    words: int = 0
    repetitions: int = 0     # "the the", "controls the controls the"
    part_words: int = 0      # "oculo- oculomotor", "n-nerve"
    fillers: int = 0         # um, uh
    trailing: bool = False   # sentence stops on a dangling word or a fragment

    @property
    def events(self) -> int:
        return self.repetitions + self.part_words + self.fillers

    def as_dict(self) -> dict:
        return {"words": self.words, "repetitions": self.repetitions, "part_words": self.part_words,
                "fillers": self.fillers, "trailing": self.trailing, "events": self.events}


def tokens(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _norm(tok: str) -> str:
    return tok.lower().strip("-'…")


def _is_fragment_of(frag: str, nxt: str) -> bool:
    f, n = _norm(frag), _norm(nxt)
    return bool(f) and len(f) < len(n) and n.startswith(f)


_SPLIT_PUNCT = re.compile(r"^([\"'(\[]*)(.*?)([.,;:!?\"')\]…]*)$", flags=re.S)


def clean(text: str) -> tuple[str, Disfluency]:
    """
    Remove stutters while keeping his words and punctuation: fillers, part-word
    starts and immediate repetitions are dropped. Returns (clean text, what was found).
    """
    # each item: [leading punct, word, trailing punct]
    items = [list(_SPLIT_PUNCT.match(t).groups()) for t in (text or "").split()]
    d = Disfluency()
    out: list[list[str]] = []

    def drop(item):
        # keep sentence punctuation that was hanging on a dropped word ("um," -> nothing, "uh." -> ".")
        if item[2] and out and not out[-1][2]:
            out[-1][2] = item[2]

    i = 0
    while i < len(items):
        lead, word, trail = items[i]
        low = _norm(word)
        nxt = [it[1] for it in items[i + 1:i + 4]]
        if not low:
            drop(items[i])
        elif low in FILLERS:
            d.fillers += 1
            drop(items[i])
        else:
            # "n-nerve" / "her-herniation": short stem + hyphen + word starting with that stem
            m = re.fullmatch(r"([A-Za-z]{1,4})-([A-Za-z][A-Za-z'\-]*)", word)
            if m and m.group(2).lower().startswith(m.group(1).lower()):
                d.part_words += 1
                word = m.group(2)
                low = _norm(word)
            cut = word.endswith("-")
            if nxt and (cut or len(low) <= 6) and _is_fragment_of(word, nxt[0]) and (cut or low not in DANGLING):
                d.part_words += 1          # "oculo- oculomotor"
                drop(items[i])
            elif cut and any(_is_fragment_of(word, w) for w in nxt[1:3]):
                d.part_words += 1          # "the oculo- the oculomotor": cut off, then restarted
                drop(items[i])
            else:
                out.append([lead, word, trail])  # includes an uncompleted "vi-": it's what he said
        i += 1

    # collapse immediate repetitions of 1-3 word phrases: "controls the controls the"
    changed = True
    while changed:
        changed = False
        for n in (3, 2, 1):
            j = 0
            while j + 2 * n <= len(out):
                a = [_norm(x[1]) for x in out[j:j + n]]
                b = [_norm(x[1]) for x in out[j + n:j + 2 * n]]
                if a == b and all(a):
                    # keep the second attempt's punctuation: "the, the inferior" -> "the inferior"
                    out[j + n - 1][2] = out[j + 2 * n - 1][2]
                    del out[j + n:j + 2 * n]
                    d.repetitions += 1
                    changed = True
                else:
                    j += 1
    text_out = " ".join("".join(x) for x in out)
    d.words = len(out)
    d.trailing = is_incomplete(text_out)
    return text_out, d


def is_incomplete(text: str) -> bool:
    """Heuristic: does this utterance stop mid-thought?"""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("…", "...", "-", ",")):
        return True
    toks = tokens(t)
    if not toks:
        return False
    last = _norm(toks[-1])
    return last in DANGLING or toks[-1].endswith("-")


# ------------------------------------------------------- rolling monitor
class SymptomMonitor:
    """Keeps the last few minutes of transcript-derived symptoms in memory."""

    def __init__(self, window_s: float = 300.0):
        self.window_s = window_s
        self.items: deque[tuple[float, Disfluency]] = deque()
        self.predictions_used = 0

    def add(self, d: Disfluency, ts: Optional[float] = None) -> None:
        ts = ts or time.time()
        self.items.append((ts, d))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        while self.items and now - self.items[0][0] > self.window_s:
            self.items.popleft()

    def summary(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        self._trim(now)
        words = sum(d.words for _, d in self.items)
        events = sum(d.events for _, d in self.items)
        n = len(self.items)
        trailing = sum(1 for _, d in self.items if d.trailing)
        return {
            "utterances": n,
            "words": words,
            "disfluencies_per_100_words": round(100.0 * events / words, 1) if words else None,
            "repetitions": sum(d.repetitions for _, d in self.items),
            "part_words": sum(d.part_words for _, d in self.items),
            "fillers": sum(d.fillers for _, d in self.items),
            "trailing_pct": round(100.0 * trailing / n) if n else None,
            "window_min": round(self.window_s / 60),
        }

    def reset(self) -> None:
        self.items.clear()
        self.predictions_used = 0


# ----------------------------------------------------- subsystem status
def _level(value: Optional[float], warn: float, alert: float, higher_is_worse: bool = True) -> str:
    if value is None:
        return "unknown"
    v = value if higher_is_worse else -value
    w, a = (warn, alert) if higher_is_worse else (-warn, -alert)
    if v >= a:
        return "alert"
    if v >= w:
        return "watch"
    return "ok"


def subsystems(snapshot: dict, latest: dict, disfl: dict) -> list[dict]:
    """
    Map live measures onto Green et al.'s four speech subsystems.
    `snapshot`: FatigueEngine.snapshot(); `latest`: last feature window; `disfl`: SymptomMonitor.summary().
    """
    comps = snapshot.get("components") or {}
    base = snapshot.get("baseline") or {}
    wpm = latest.get("wpm")
    pause = latest.get("pause_pct")
    bg = latest.get("breath_group_words")
    db = latest.get("loudness_db")
    rate_100 = disfl.get("disfluencies_per_100_words")

    resp_detail = []
    if bg is not None:
        resp_detail.append(f"{bg:.1f} words per breath" + (f" (fresh: {base['breath_group_words']:.0f})"
                                                           if base.get("breath_group_words") else ""))
    if pause is not None:
        resp_detail.append(f"{pause:.0f}% of speaking time is pauses")
    resp_val = max(comps.get("breath_group", 0.0), _clip((pause or 0) - 20, 0, 20) / 20 if pause is not None else 0.0) \
        if (bg is not None or pause is not None) else None

    phon_detail = []
    if db is not None:
        phon_detail.append(f"loudness {db:.1f} dBFS" + (f" (fresh: {base['loudness_db']:.1f})"
                                                        if base.get("loudness_db") is not None else ""))
    if comps.get("effort") is not None:
        phon_detail.append(f"effort for the same sound up {comps['effort'] * 50:.0f}%")
    phon_vals = [v for k, v in comps.items() if k in ("loudness_db", "effort")]
    phon_val = max(phon_vals) if phon_vals else None

    art_detail = []
    art_level = "unknown"
    if wpm is not None:
        art_detail.append(f"{wpm:.0f} words per minute" + (f" (fresh: {base['wpm']:.0f})" if base.get("wpm") else ""))
        art_level = "alert" if wpm < SPEECH_LOSS_WPM else ("watch" if wpm < SUPPORT_WPM or comps.get("wpm", 0) > 0.5 else "ok")
    if rate_100 is not None:
        art_detail.append(f"{rate_100:.1f} stumbles per 100 words")
        if rate_100 >= 8:
            art_level = "alert"
        elif rate_100 >= 4 and art_level in ("ok", "unknown"):
            art_level = "watch"

    return [
        {"key": "respiratory", "name": "Breathing for speech", "level": _level(resp_val, 0.4, 0.75),
         "detail": "; ".join(resp_detail) or "No speech yet",
         "why": "Weaker breathing muscles mean more, longer pauses and fewer words per breath."},
        {"key": "phonatory", "name": "Voice", "level": _level(phon_val, 0.4, 0.75),
         "detail": "; ".join(phon_detail) or "No speech yet",
         "why": "Loudness and voice-quality change can be the first bulbar sign."},
        {"key": "articulatory", "name": "Rate and clarity", "level": art_level,
         "detail": "; ".join(art_detail) or "No speech yet",
         "why": "Speaking rate falls before intelligibility; under 120 wpm clarity tends to drop quickly."},
        {"key": "resonatory", "name": "Nasality", "level": "unknown",
         "detail": "Not measured: needs a nasal/oral two-mic headset (nasalance).",
         "why": "Hypernasal speech is common in bulbar ALS; flag it to the SLP if you notice it."},
    ]


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------- guidance
def guidance(snapshot: dict, latest: dict, disfl: dict) -> list[dict]:
    """
    Plain-language, energy-conserving tips for right now. Ordered by priority.
    Never suggests strengthening exercises for tongue, lips or jaw.
    """
    tips: list[dict] = []
    battery = snapshot.get("battery", 100)
    comps = snapshot.get("components") or {}
    wpm = latest.get("wpm")
    rate_100 = disfl.get("disfluencies_per_100_words") or 0
    trailing = disfl.get("trailing_pct") or 0
    pause = latest.get("pause_pct")

    if battery < 30:
        tips.append({"level": "alert", "title": "Voice rest",
                     "text": "Your voice is running low. Switch to a written or slide-based activity for 10 minutes "
                             "and let captions and predicted words carry the explanation."})
    if wpm is not None and wpm < SPEECH_LOSS_WPM:
        tips.append({"level": "alert", "title": "Keep captions on",
                     "text": f"Speaking rate is {wpm:.0f} wpm, under the 120 wpm point where clarity usually starts "
                             "to drop. Captions on; short sentences; key words on the board."})
    if comps.get("breath_group", 0) > 0.5 or (pause is not None and pause > 35):
        tips.append({"level": "watch", "title": "Shorter phrases",
                     "text": "Fewer words per breath now. Say 4-6 words, pause, breathe, continue. "
                             "Pausing at phrase ends sounds natural and saves air."})
    if comps.get("loudness_db", 0) > 0.5:
        tips.append({"level": "watch", "title": "Let the mic do the work",
                     "text": "Your voice is quieter than this morning. Don't push for volume: "
                             "use the voice amplifier or face the class so they can read your lips."})
    if rate_100 >= 4:
        tips.append({"level": "watch", "title": "Slow the start",
                     "text": "More stumbles than usual. Take a breath before each sentence and start "
                             "slower; it costs less effort than restarting."})
    if trailing >= 30:
        tips.append({"level": "watch", "title": "Finish with a gesture",
                     "text": "Several sentences are trailing off. Point to the slide or let the predicted "
                             "words finish them; students can follow the captions."})
    if comps.get("effort", 0) > 0.5:
        tips.append({"level": "watch", "title": "Relax the throat",
                     "text": "More throat effort for the same sound. Drop your shoulders, sip water, "
                             "and lower volume rather than straining."})
    if not tips:
        tips.append({"level": "ok", "title": "Pacing is good",
                     "text": "Voice is holding up. Plan the most talk-heavy part of the lesson while you're fresh."})
    return tips


WEEKLY_CHECK = [
    # Green et al. 2013, Table II: the tasks behind each subsystem measure.
    {"subsystem": "respiratory", "task": "Read the Bamboo passage aloud at a comfortable pace",
     "measure": "percent pause"},
    {"subsystem": "phonatory", "task": "Sustain \"ah\" for as long as is comfortable, once",
     "measure": "voice noise (noise-to-harmonics ratio), steadiness"},
    {"subsystem": "resonatory", "task": "Say \"Buy Bobby a puppy\" three times",
     "measure": "nasalance (clinic equipment)"},
    {"subsystem": "articulatory", "task": "Say \"I owe you a yoyo today\" three times",
     "measure": "speaking rate, clarity"},
]


# ---------------------------------------------------- longitudinal model
def project_decline(trends: list[dict], onset: str = "bulbar") -> dict:
    """
    Fit a straight line to daily morning speaking rate and compare with
    Eshghi et al. 2022. Returns slope (wpm/month), months until the 120 wpm
    marker at that slope, and the published reference slopes.
    """
    pts = []
    for row in trends:
        y = row.get("morning_wpm")
        if y is None:
            continue
        try:
            day = date.fromisoformat(row["day"])
        except (KeyError, ValueError):
            continue
        pts.append((day.toordinal(), float(y)))
    ref = {"spinal_wpm_per_month": DECLINE_WPM_PER_MONTH["spinal"],
           "bulbar_wpm_per_month": DECLINE_WPM_PER_MONTH["bulbar"],
           "marker_wpm": SPEECH_LOSS_WPM, "source": "Eshghi et al. 2022, Sci Rep 12:15713"}
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < 7:
        return {"ok": False, "reason": "Needs at least 5 days spread over a week or more.", "reference": ref}

    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    slope_day = sxy / sxx if sxx else 0.0
    slope_month = slope_day * 30.44
    current = my + slope_day * (pts[-1][0] - mx)

    months_to_marker = None
    if current <= SPEECH_LOSS_WPM:
        months_to_marker = 0.0
    elif slope_month < -0.05:
        months_to_marker = round((current - SPEECH_LOSS_WPM) / -slope_month, 1)

    pub = DECLINE_WPM_PER_MONTH.get(onset, DECLINE_WPM_PER_MONTH["bulbar"])
    decline = -slope_month
    if decline <= 0.5:
        compare = "stable"
    elif decline < DECLINE_WPM_PER_MONTH["spinal"] * 1.25:
        compare = "similar to the published spinal-onset average"
    elif decline < DECLINE_WPM_PER_MONTH["bulbar"] * 1.25:
        compare = "similar to the published bulbar-onset average"
    else:
        compare = "faster than the published bulbar-onset average"

    return {
        "ok": True, "days": n, "current_morning_wpm": round(current, 1),
        "slope_wpm_per_month": round(slope_month, 2), "comparison": compare,
        "months_to_120": months_to_marker, "published_for_onset": pub, "reference": ref,
        "note": "Short-term daily data are noisy and include fatigue and practice effects; "
                "for clinician review, not a prognosis.",
    }
