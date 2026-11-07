"""
Demo simulator: plays Alex's day through the real backend, no hardware needed.

Uses his clinic numbers: 165 wpm at 9 am falling to 118 wpm by 4 pm, getting
quieter, fewer words per breath, more effort per sound.

    python client/simulator.py --day            # compress a school day into ~2 minutes
    python client/simulator.py --history 21     # backfill 3 weeks for the clinic charts
    python client/simulator.py --breath         # fake breathing-trainer reps
    python client/simulator.py --captions       # also stream sample lesson captions
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
import urllib.request
from datetime import datetime, timedelta

SERVER = os.getenv("RECAP_SERVER", "http://localhost:8000")
TOKEN = os.getenv("RECAP_TOKEN", "")

LESSON = [
    "Good morning everyone, today we are finishing cellular respiration.",
    "Mitochondria take glucose and oxygen and turn them into ATP the cell can use.",
    "Remember that glycolysis happens in the cytoplasm, not in the mitochondria.",
    "The Krebs cycle releases carbon dioxide, which is what you breathe out.",
    "Most of the ATP comes from the electron transport chain on the inner membrane.",
    "If there is no oxygen, cells switch to fermentation and make far less ATP.",
    "Okay, put your phones away and let's look at the diagram on page two hundred.",
    "For the test, know the inputs and outputs of each stage, not every enzyme name.",
]


def post(path, body):
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Recap-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def alex_at(frac: float, day_decline: float = 0.0, noise: float = 1.0) -> dict:
    """frac 0 = 9 am, 1 = 4 pm. day_decline 0..1 slowly worsens mornings over weeks."""
    j = lambda s: random.gauss(0, s * noise)
    fatigue = frac ** 1.4  # holds up in the morning, drops after lunch
    wpm = 165 - 47 * fatigue - 12 * day_decline + j(3)
    db = -20 - 4.5 * fatigue - 1.0 * day_decline + j(0.4)
    bg = 12 - 4.5 * fatigue - 1.5 * day_decline + j(0.4)
    out = 10 ** (db / 20)
    effort = 10.0 * out * (1 + 0.6 * fatigue + 0.1 * day_decline) * (1 + j(0.03))
    return {"wpm": round(wpm, 1), "loudness_db": round(db, 2), "breath_group_words": round(bg, 2),
            "effort": round(effort, 4), "speaking_s": 8.0, "confidence": round(0.97 - 0.07 * fatigue, 3)}


def run_day(minutes: float, captions: bool):
    post("/api/reset-day", {})
    post("/api/mode", {"mode": "auto"})
    post("/api/baseline", {"wpm": 165, "loudness_db": -20, "breath_group_words": 12, "effort_per_output": 10.0})
    steps = int(minutes * 60 / 1.0)
    li = 0
    for i in range(steps + 1):
        frac = i / steps
        f = alex_at(frac)
        s = post("/api/features", f)
        clock = (datetime(2026, 11, 9, 9) + timedelta(hours=7 * frac)).strftime("%I:%M %p")
        print(f"{clock}  wpm={f['wpm']:5.1f}  score={s['score']:5.1f}  battery={s['battery']:3d}%  "
              f"captions={'ON ' if s['captions_on'] else 'off'}")
        if captions and i % 4 == 0:
            post("/api/captions", {"text": LESSON[li % len(LESSON)], "final": True}); li += 1
        time.sleep(1.0)


def backfill(days: int):
    """Write past days straight into the store, with realistic timestamps."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
    import config
    from db import make_store
    store = make_store(config.DATABASE_URL, config.SQLITE_PATH)
    now = datetime.now()
    for d in range(days, 0, -1):
        day = (now - timedelta(days=d)).replace(hour=9, minute=0, second=0, microsecond=0)
        if day.weekday() >= 5:
            continue  # weekends off
        decline = (days - d) / max(days, 1) * 0.4
        for k in range(0, 7 * 12):  # every 5 minutes, 9 am to 4 pm
            ts = day + timedelta(minutes=5 * k)
            f = alex_at(k / 84, decline, noise=0.8)
            score = max(0, min(100, 100 * ((165 - f["wpm"]) / 165 / 0.3 * 0.6 + (-20 - f["loudness_db"]) / 6 * 0.4)))
            store.insert_features(ts.timestamp(), {**f, "score": round(score, 1), "captions_on": int(score > 55)})
        store.insert_emst((day + timedelta(minutes=-30)).timestamp(), random.choice([20, 25, 25, 25]),
                          45 + random.gauss(0, 3) + 6 * (days - d) / days, 40)
        print("backfilled", day.date())


def breath(reps: int = 10, target: float = 40):
    for r in range(reps):
        for k in range(30):  # one 1.5 s blow
            p = (target + 8) * math.sin(math.pi * k / 29) + random.gauss(0, 1)
            post("/api/emst/sample", {"pressure_cmh2o": max(0, p)})
            time.sleep(0.05)
        for _ in range(10):
            post("/api/emst/sample", {"pressure_cmh2o": 0})
            time.sleep(0.1)
        print("rep", r + 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", action="store_true")
    ap.add_argument("--minutes", type=float, default=2.0)
    ap.add_argument("--captions", action="store_true")
    ap.add_argument("--history", type=int, default=0)
    ap.add_argument("--breath", action="store_true")
    a = ap.parse_args()
    if a.history:
        backfill(a.history)
    if a.breath:
        breath()
    if a.day:
        run_day(a.minutes, a.captions)
    if not (a.history or a.breath or a.day):
        ap.print_help()
