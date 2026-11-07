"""
Gemini helpers: the end-of-day recap and the weekly clinic summary.

Design rule: Gemini never writes in Alex's voice. For the recap it only
returns the *numbers* of sentences to keep; the server then copies those
sentences verbatim from his own transcript. So the recap is always his words.

If no GEMINI_API_KEY is set, or the call fails, a simple offline fallback
runs instead so the demo never breaks.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Optional

import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


def _gemini(prompt: str, json_mode: bool = False, timeout: float = 20.0) -> Optional[str]:
    if not config.GEMINI_API_KEY:
        return None
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    req = urllib.request.Request(
        GEMINI_URL.format(model=config.GEMINI_MODEL, key=config.GEMINI_API_KEY),
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as e:
        print(f"[gemini] falling back: {e}")
        return None


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if len(p.split()) >= 4]


def _fallback_pick(sentences: list[str], keyterms: list[str], k: int) -> list[int]:
    """Offline: prefer sentences that use subject vocabulary, then longer ones."""
    terms = [t.lower() for t in keyterms]
    def score(i):
        s = sentences[i].lower()
        return (sum(t in s for t in terms), min(len(s.split()), 25))
    ranked = sorted(range(len(sentences)), key=score, reverse=True)[:k]
    return sorted(ranked)


def draft_recap(transcript_lines: list[str], keyterms: list[str], k: int = 5) -> dict:
    sentences = []
    for line in transcript_lines:
        sentences.extend(split_sentences(line))
    if not sentences:
        return {"sentences": [], "source": "empty"}

    numbered = "\n".join(f"{i}: {s}" for i, s in enumerate(sentences))
    prompt = (
        "You help a high-school biology teacher build an end-of-day recap from his own lesson.\n"
        f"Below are numbered sentences he said today. Choose up to {k} that best capture the "
        "key ideas students should remember. Do NOT rewrite anything.\n"
        'Return ONLY JSON like {"keep": [3, 7, 12]} using the sentence numbers.\n\n'
        f"{numbered}"
    )
    raw = _gemini(prompt, json_mode=True)
    idx, source = None, "gemini"
    if raw:
        try:
            idx = [int(i) for i in json.loads(raw).get("keep", [])]
            idx = sorted({i for i in idx if 0 <= i < len(sentences)})[:k]
        except (ValueError, AttributeError, json.JSONDecodeError):
            idx = None
    if not idx:
        idx, source = _fallback_pick(sentences, keyterms, k), "offline"
    # verbatim copy: the model only chose numbers
    return {"sentences": [sentences[i] for i in idx], "all": sentences, "source": source}


def clinic_summary(trends: list[dict]) -> dict:
    if not trends:
        return {"text": "No data yet.", "source": "empty"}
    prompt = (
        "You are writing a short, factual note for an ALS clinic team (speech-language "
        "pathologist, respiratory therapist). Summarize these daily trends from a wearable "
        "speech monitor in 4-6 plain sentences: morning vs afternoon speech rate, loudness, "
        "words per breath, fatigue score, breathing-trainer adherence. Note days under 125 wpm. "
        "Do not diagnose or recommend treatment; flag patterns for the clinicians to review.\n\n"
        f"{json.dumps(trends[-14:], indent=1)}"
    )
    text = _gemini(prompt)
    if text:
        return {"text": text.strip(), "source": "gemini"}
    # offline fallback: a simple template
    last = trends[-1]
    first = trends[0]
    def fmt(v, unit=""):
        return "n/a" if v is None else f"{v}{unit}"
    text = (
        f"Over {len(trends)} day(s), morning speech rate went from {fmt(first.get('morning_wpm'))} to "
        f"{fmt(last.get('morning_wpm'))} wpm; afternoon from {fmt(first.get('afternoon_wpm'))} to "
        f"{fmt(last.get('afternoon_wpm'))} wpm. Most recent day: {fmt(last.get('support_pct'), '%')} of windows "
        f"under 125 wpm, {fmt(last.get('emst_reps'))} breathing-trainer reps. For clinician review."
    )
    return {"text": text, "source": "offline"}
