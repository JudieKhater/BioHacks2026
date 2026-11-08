"""
Breathing plan after a teaching session.

Built from that session's numbers and grounded in the two papers in the repo
root. Neither paper tests an exercise programme; they tell us *what to measure*
and *when to worry*, and the plan is built around exactly those things:

  Green et al. 2013 (Amyotroph Lateral Scler Frontotemporal Degener 14:494)
    - ALS weakens the respiratory muscles; vital capacity falls on average
      1.8 %/month (slow progression) to 3.5 %/month (rapid).
    - Weaker breathing shows in speech as more frequent breaths and longer,
      more frequent pauses. The respiratory subsystem is measured as
      *percent pause* while reading a passage aloud.
    - The phonatory subsystem is checked with a sustained "ah".
  Eshghi et al. 2022 (Sci Rep 12:15713)
    - Speaking rate < 120 wpm and intelligibility < 85 % mark functional
      speech loss; speech measures change faster than self-report, so they
      are the right signal for timing interventions (e.g. communication aids).

So the plan:
  1. Recovery breaths: rest first when the session tired him (energy
     conservation, a general ALS speech-care principle, not from the papers).
  2. Phrase-breathing practice: read lesson text aloud with a breath mark
     every N words, N taken from his own words-per-breath at the end of the
     session, so breaths land at phrase ends instead of running out mid-phrase.
     This is the same passage-reading task Green et al. use for percent pause,
     so the next session report shows whether it's helping.
  3. An easy sustained "ah" (Green et al.'s phonatory task), comfortable, not
     maximal, timed and compared week to week.
  4. Pacing at the start of phrases when his rate fell (Eshghi et al.).
  5. EMST only if his therapist prescribed it (not from these papers).

Every dose shrinks when the session tired him: no training on a tired system.
Never tongue, lip or jaw strengthening.
"""
from __future__ import annotations

import re
from typing import Optional

import speech_symptoms as symptoms

GREEN = "Green et al. 2013, Amyotroph Lateral Scler Frontotemporal Degener 14:494"
ESHGHI = "Eshghi et al. 2022, Sci Rep 12:15713"

FALLBACK_PASSAGE = [
    "Plants take in light, water and carbon dioxide.",
    "Inside each leaf, the chloroplasts turn that light into stored energy.",
    "The sugar they make travels through the plant to wherever it is needed.",
    "Oxygen, the by-product, leaves through tiny openings under the leaf.",
]

SAFETY = ("Stop if you feel dizzy, short of breath or tired. These are practice suggestions built on your "
          "measurements, not a prescription: check new exercises with your SLP or respiratory therapist.")


def _v(r: dict, key: str, part: str = "mean") -> Optional[float]:
    return ((r.get("measures") or {}).get(key) or {}).get(part)


def readiness(r: dict) -> str:
    """recover = rest only today; light = small doses; standard = normal practice."""
    resp = next((s["level"] for s in r.get("subsystems", []) if s["key"] == "respiratory"), "unknown")
    battery = r.get("battery_end", 100)
    if battery < 35 or r.get("score_max", 0) >= 70 or resp == "alert":
        return "recover"
    if battery < 60 or resp == "watch" or (r.get("pct_time_under_120_wpm") or 0) >= 25:
        return "light"
    return "standard"


def phrase_length(r: dict) -> int:
    """Words per breath mark: what he managed late in the session (when tired), 3-10."""
    bg = _v(r, "breath_group_words", "end") or _v(r, "breath_group_words")
    if bg is None:
        return 6
    return max(3, min(10, int(round(bg))))


def practice_passage(sentences: list[str], n_words: int, max_words: int = 60) -> list[str]:
    """Pick a few lesson sentences and split them into phrases of about n_words, at punctuation if possible."""
    picked, total = [], 0
    for s in sentences:
        k = len(s.split())
        if 7 <= k <= 28 and not re.search(r"\d{4}|http|et al", s):
            picked.append(s)
            total += k
        if total >= max_words or len(picked) >= 4:
            break
    if total < 20:
        picked = FALLBACK_PASSAGE
    phrases, cur = [], []
    for word in " ".join(picked).split():
        cur.append(word)
        at_punct = word[-1] in ",.;:!?"
        if len(cur) >= n_words or (at_punct and len(cur) >= max(2, n_words - 2)):
            phrases.append(" ".join(cur))
            cur = []
    if cur:
        if phrases and len(cur) <= 2:
            phrases[-1] += " " + " ".join(cur)
        else:
            phrases.append(" ".join(cur))
    return phrases


def plan(r: dict, previous: list[dict], lesson_sentences: list[str]) -> dict:
    if r.get("empty"):
        return {"readiness": "standard", "phrase_words": None, "exercises": [], "safety": SAFETY,
                "note": "No speech in this session, so no plan. Start a session and teach as usual."}
    level = readiness(r)
    n = phrase_length(r)
    bg_s, bg_e = _v(r, "breath_group_words", "start"), _v(r, "breath_group_words", "end")
    p_s, p_e = _v(r, "pause_pct", "start"), _v(r, "pause_pct", "end")
    w_s, w_e = _v(r, "wpm", "start"), _v(r, "wpm", "end")

    def change(a, b, fmt="{:.0f}"):
        return f"{fmt.format(a)} → {fmt.format(b)}" if a is not None and b is not None else "not measured"

    ex: list[dict] = []

    # 1. Recovery breaths
    ex.append({
        "key": "recovery", "name": "Recovery breaths",
        "when": "Now, sitting down", "dose": "5-6 slow breaths, once" if level != "recover" else "5-6 slow breaths, twice, a few minutes apart",
        "steps": ["Sit upright with your back supported and shoulders relaxed.",
                  "One hand on your stomach. Breathe in gently through your nose for about 3 seconds; let the stomach rise.",
                  "Breathe out slowly through slightly pursed lips for about 5 seconds. Don't force it out.",
                  "Rest your voice for 10 minutes afterwards if you can."],
        "why": f"Your words per breath went {change(bg_s, bg_e, '{:.1f}')} and pauses {change(p_s, p_e)}% during the session. "
               "Weaker breathing shows up in speech exactly this way: more breaths, longer pauses. After a heavy talking "
               "session the aim is to recover, not to train.",
        "source": f"Measures: {GREEN}. Rest-first: general ALS energy-conservation advice (not tested in these studies).",
    })

    # 2. Phrase-breathing practice with lesson text
    phrases = practice_passage(lesson_sentences, n)
    when = {"recover": "Tomorrow morning, when you're fresh", "light": "Later today after resting, or tomorrow morning",
            "standard": "Later today or tomorrow morning"}[level]
    dose = {"recover": "Read it once", "light": "Read it once", "standard": "Read it twice, with a minute's rest between"}[level]
    ex.append({
        "key": "phrasing", "name": f"Phrase breathing: a breath every {n} words",
        "when": when, "dose": dose,
        "steps": [f"Read the passage below aloud at a comfortable pace. Each / is a breath mark (about every {n} words).",
                  "Take a small, easy breath at every mark, before you feel short of air, not after.",
                  "Keep your volume comfortable; let the microphone carry it.",
                  "If you read it during a Recap session, your next report shows the pauses and words per breath."],
        "passage": phrases,
        "why": f"{n} words is roughly what you managed per breath late in this session. Planning breaths at phrase ends "
               "means pauses fall where listeners expect them, instead of running out of air mid-phrase and trailing off. "
               "Reading a passage aloud is the same task researchers use to measure the breathing part of speech "
               "(percent pause), so it doubles as a check.",
        "source": f"Task and measure: {GREEN} (percent pause while reading a passage). Phrase length: your session data.",
    })

    # 3. Easy sustained "ah"
    if level != "recover":
        ex.append({
            "key": "ah", "name": "Easy sustained \"ah\"",
            "when": "Once a day, same time of day each time", "dose": "1-2 tries, a minute apart",
            "steps": ["After a normal breath in, say \"ah\" at a comfortable, steady loudness.",
                      "Stop when it starts to feel like effort. This is not a maximum test.",
                      "Note how many seconds it lasted and compare week to week, not day to day."],
            "why": "A steady, comfortable \"ah\" shows how well breath and voice work together. A drop over weeks is "
                   "worth mentioning to your clinic.",
            "source": f"Task: {GREEN} (sustained \"ah\" for the voice subsystem). Kept easy for energy conservation.",
        })

    # 4. Pacing, if the rate fell or went under the support line
    rate_drop = (w_s - w_e) / w_s if w_s and w_e else 0
    if (w_e is not None and w_e < symptoms.SUPPORT_WPM) or rate_drop >= 0.15:
        ex.append({
            "key": "pacing", "name": "Slow starts",
            "when": "Next session, first few minutes", "dose": "Just the first sentence after each pause",
            "steps": ["Take your breath first, then start each phrase slowly and clearly.",
                      "Don't try to speed back up when you're tired; let captions and predicted words carry the pace."],
            "why": f"Your speaking rate went {change(w_s, w_e)} wpm. Speaking rate drops before clarity does, and under "
                   "about 120 wpm clarity tends to fall quickly, so clear beats fast.",
            "source": f"{ESHGHI} (120 wpm marker); {GREEN} (rate falls before intelligibility).",
        })

    # 5. EMST, only if prescribed
    ex.append({
        "key": "emst", "name": "Breathing trainer (EMST), only if prescribed",
        "when": "Mornings", "dose": "Your therapist's prescription only" if level == "standard"
        else "Skip extra reps today; do tomorrow morning's prescribed set when fresh",
        "steps": ["Use the Breathing page with the target your therapist set.", "Stop if dizzy."],
        "why": "Strength-type breathing work belongs at the start of the day, not after a tiring session.",
        "source": "Your therapist's prescription. Not part of the referenced studies.",
        "link": "/trainer.html",
    })

    return {"readiness": level, "phrase_words": n, "exercises": ex, "safety": SAFETY,
            "readiness_text": {"recover": "This session tired your breathing and voice. Rest first; practise tomorrow.",
                               "light": "Some tiredness showed. Keep practice short today.",
                               "standard": "You held up well. Normal practice is fine."}[level]}


def _trend(previous: list[dict], r: dict, key: str) -> Optional[tuple[float, float, int]]:
    """Mean of the older half vs the newer half of up to 8 sessions (oldest first)."""
    vals = [_v(p, key) for p in reversed([p for p in previous if not p.get("empty")][:7])] + [_v(r, key)]
    vals = [v for v in vals if v is not None]
    if len(vals) < 4:
        return None
    h = len(vals) // 2
    return sum(vals[:h]) / h, sum(vals[h:]) / (len(vals) - h), len(vals)


def clinician_flags(r: dict, previous: list[dict]) -> list[dict]:
    """Things worth raising with the SLP / respiratory therapist. Flags, not diagnoses."""
    if r.get("empty"):
        return []
    flags = []
    under = r.get("pct_time_under_120_wpm") or 0
    if under >= 25:
        flags.append({"level": "alert", "text":
                      f"Speaking rate was under 120 wpm for {under}% of this session. In ALS research, under 120 wpm "
                      "marks the point where speech clarity tends to drop quickly; a good time to talk with your SLP "
                      "about communication supports.", "source": ESHGHI})
    bg = _trend(previous, r, "breath_group_words")
    if bg and bg[1] < bg[0] * 0.9:
        flags.append({"level": "watch", "text":
                      f"Words per breath has fallen over your last {bg[2]} sessions ({bg[0]:.1f} → {bg[1]:.1f}). "
                      "Ask your respiratory therapist whether a breathing test (vital capacity) is due.", "source": GREEN})
    pp = _trend(previous, r, "pause_pct")
    if pp and pp[1] > pp[0] + 5:
        flags.append({"level": "watch", "text":
                      f"Pauses have increased over your last {pp[2]} sessions ({pp[0]:.0f}% → {pp[1]:.0f}%). "
                      "More, longer pauses are how weaker breathing shows in speech.", "source": GREEN})
    wpm = _trend(previous, r, "wpm")
    if wpm and wpm[1] < wpm[0] * 0.92:
        flags.append({"level": "watch", "text":
                      f"Average speaking rate has fallen over your last {wpm[2]} sessions ({wpm[0]:.0f} → {wpm[1]:.0f} wpm).",
                      "source": ESHGHI})
    return flags
