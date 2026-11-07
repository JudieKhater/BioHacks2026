"""
Recap backend (FastAPI).

Run from the repo root:
    uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
then open http://localhost:8000

Data flow
---------
  audio_client.py  --POST /api/features-->  fatigue engine --> DB (numbers only)
                   --POST /api/captions-->  /ws/captions (only while captions are on)
  serial_bridge.py --POST /api/sensor---->  latest throat effort (merged into features)
                   --POST /api/emst/sample-> /ws/trainer (live breathing pressure)
  pages            <--/ws/state---------    live score, battery, captions on/off
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))  # so `import config` works under uvicorn

import config  # noqa: E402
import consent  # noqa: E402
import recap_ai  # noqa: E402
from db import make_store  # noqa: E402
from fatigue import FatigueEngine, FeatureWindow  # noqa: E402

app = FastAPI(title="Recap", version="1.0")
store = make_store(config.DATABASE_URL, config.SQLITE_PATH)
engine = FatigueEngine(on_threshold=config.ON_THRESHOLD, off_threshold=config.OFF_THRESHOLD,
                       min_on_seconds=config.MIN_ON_SECONDS)

# Biology vocabulary: boosts Scribe accuracy (sent by the audio client as keyterms)
# and helps the offline recap fallback pick content sentences.
KEYTERMS = [t.strip() for t in os.getenv(
    "RECAP_KEYTERMS",
    "mitochondria,photosynthesis,chloroplast,enzyme,osmosis,diffusion,ATP,DNA,RNA,"
    "transcription,translation,meiosis,mitosis,homeostasis,neuron,membrane,protein,cellular respiration",
).split(",") if t.strip()]

# In memory only, for today's recap. Never written to disk.
transcript_today: list[tuple[float, str]] = []
transcript_day = datetime.now().strftime("%Y-%m-%d")
latest_effort: dict = {"value": None, "ts": 0.0}


# ------------------------------------------------------------------- auth
def require_token(x_recap_token: Optional[str] = Header(default=None)):
    if config.RECAP_TOKEN and x_recap_token != config.RECAP_TOKEN:
        raise HTTPException(status_code=401, detail="missing or wrong X-Recap-Token")


# ------------------------------------------------------------- websockets
class Hub:
    def __init__(self):
        self.channels: dict[str, set[WebSocket]] = {"state": set(), "captions": set(), "trainer": set()}

    async def join(self, ch: str, ws: WebSocket):
        await ws.accept()
        self.channels[ch].add(ws)

    def leave(self, ch: str, ws: WebSocket):
        self.channels[ch].discard(ws)

    async def send(self, ch: str, msg: dict):
        dead = []
        for ws in list(self.channels[ch]):
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.leave(ch, ws)


hub = Hub()


def state_msg() -> dict:
    snap = engine.snapshot()
    snap["type"] = "state"
    snap["ts"] = time.time()
    snap["store"] = store.kind
    return snap


async def push_state():
    msg = state_msg()
    await hub.send("state", msg)
    await hub.send("captions", {"type": "visibility", "captions_on": msg["captions_on"]})


def _roll_day():
    """Start a fresh day at midnight: clear transcript, reset the daily score."""
    global transcript_day
    today = datetime.now().strftime("%Y-%m-%d")
    if today != transcript_day:
        transcript_today.clear()
        engine.reset_day()
        transcript_day = today


# ----------------------------------------------------------------- models
class FeaturesIn(BaseModel):
    ts: Optional[float] = None
    wpm: Optional[float] = None
    loudness_db: Optional[float] = None
    breath_group_words: Optional[float] = None
    effort: Optional[float] = None
    speaking_s: float = 0.0
    confidence: Optional[float] = None


class CaptionIn(BaseModel):
    text: str
    final: bool = False


class SensorIn(BaseModel):
    effort: float


class ModeIn(BaseModel):
    mode: str = Field(pattern="^(auto|on|off)$")


class BaselineIn(BaseModel):
    wpm: Optional[float] = None
    loudness_db: Optional[float] = None
    breath_group_words: Optional[float] = None
    effort_per_output: Optional[float] = None


class EmstSampleIn(BaseModel):
    pressure_cmh2o: float


class EmstSessionIn(BaseModel):
    reps: int
    peak_cmh2o: float
    target_cmh2o: Optional[float] = None
    notes: Optional[str] = None


class RecapApproveIn(BaseModel):
    sentences: list[str]


class ConsentIn(BaseModel):
    party: str = "ALS clinic"
    granted: bool


# --------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    return {"ok": True, "store": store.kind, "gemini": bool(config.GEMINI_API_KEY),
            "solana": config.SOLANA_ANCHOR}


@app.get("/api/config")
def client_config():
    """What the audio client needs (keyterms for Scribe)."""
    return {"keyterms": KEYTERMS}


@app.get("/api/state")
def get_state():
    return state_msg()


@app.post("/api/features", dependencies=[Depends(require_token)])
async def post_features(f: FeaturesIn):
    _roll_day()
    ts = f.ts or time.time()
    effort = f.effort
    if effort is None and latest_effort["value"] is not None and ts - latest_effort["ts"] < 10:
        effort = latest_effort["value"]
    w = FeatureWindow(t=ts, wpm=f.wpm, loudness_db=f.loudness_db,
                      breath_group_words=f.breath_group_words, effort=effort,
                      speaking_s=f.speaking_s, confidence=f.confidence)
    s = engine.update(w)
    row = {"wpm": f.wpm, "loudness_db": f.loudness_db, "breath_group_words": f.breath_group_words,
           "effort": effort, "speaking_s": f.speaking_s, "confidence": f.confidence,
           "score": round(s.score, 2), "captions_on": int(s.captions_on)}
    if f.speaking_s >= 1.0:
        await asyncio.to_thread(store.insert_features, ts, row)
    await push_state()
    return state_msg()


@app.post("/api/sensor", dependencies=[Depends(require_token)])
def post_sensor(s: SensorIn):
    latest_effort.update(value=s.effort, ts=time.time())
    return {"ok": True}


@app.post("/api/captions", dependencies=[Depends(require_token)])
async def post_caption(c: CaptionIn):
    _roll_day()
    text = c.text.strip()
    if not text:
        return {"ok": True}
    if c.final:
        transcript_today.append((time.time(), text))
    # Captions only reach the room while they're on: Alex's call (or auto).
    if engine.state.captions_on:
        await hub.send("captions", {"type": "caption", "text": text, "final": c.final})
    return {"ok": True, "shown": engine.state.captions_on}


@app.post("/api/mode", dependencies=[Depends(require_token)])
async def set_mode(m: ModeIn):
    engine.set_mode(m.mode, time.time())
    await push_state()
    return state_msg()


@app.post("/api/calibrate", dependencies=[Depends(require_token)])
async def calibrate():
    engine.start_calibration()
    await push_state()
    return {"ok": True, "message": f"Speak normally; baseline set after {engine.calibration_windows} windows."}


@app.post("/api/baseline", dependencies=[Depends(require_token)])
async def set_baseline(b: BaselineIn):
    engine.set_baseline(**{k: v for k, v in b.model_dump().items() if v is not None})
    await push_state()
    return state_msg()


@app.post("/api/reset-day", dependencies=[Depends(require_token)])
async def reset_day():
    engine.reset_day()
    transcript_today.clear()
    await push_state()
    return {"ok": True}


@app.get("/api/today")
async def today():
    return {"curve": await asyncio.to_thread(store.today_curve)}


def _clinic_sharing_on() -> bool:
    """Alex's latest decision wins. Sharing is on until he first pauses it."""
    events = store.consent_log(limit=1)
    return True if not events else bool(events[0]["granted"])


@app.get("/api/trends")
async def trends(days: int = 14):
    if not await asyncio.to_thread(_clinic_sharing_on):
        return {"days": [], "paused": True}
    return {"days": await asyncio.to_thread(store.trends, days), "paused": False}


@app.get("/api/clinic/summary")
async def clinic_summary(days: int = 14):
    if not await asyncio.to_thread(_clinic_sharing_on):
        return {"text": "Alex has paused sharing with the clinic.", "source": "paused"}
    t = await asyncio.to_thread(store.trends, days)
    return await asyncio.to_thread(recap_ai.clinic_summary, t)


# ---------------------------------------------------------- breathing trainer
@app.post("/api/emst/sample", dependencies=[Depends(require_token)])
async def emst_sample(s: EmstSampleIn):
    await hub.send("trainer", {"type": "pressure", "cmh2o": s.pressure_cmh2o, "ts": time.time()})
    return {"ok": True}


@app.post("/api/emst/session", dependencies=[Depends(require_token)])
async def emst_session(s: EmstSessionIn):
    await asyncio.to_thread(store.insert_emst, time.time(), s.reps, s.peak_cmh2o, s.target_cmh2o, s.notes)
    return {"ok": True}


# -------------------------------------------------------------------- recap
@app.get("/api/recap/draft")
async def recap_draft():
    lines = [t for _, t in transcript_today]
    return await asyncio.to_thread(recap_ai.draft_recap, lines, KEYTERMS)


@app.post("/api/recap/approve", dependencies=[Depends(require_token)])
async def recap_approve(r: RecapApproveIn):
    day = datetime.now().strftime("%Y-%m-%d")
    await asyncio.to_thread(store.save_recap, time.time(), day, r.sentences)
    return {"ok": True, "day": day, "count": len(r.sentences)}


@app.get("/api/recap/latest")
async def recap_latest():
    return await asyncio.to_thread(store.latest_recap) or {"sentences": []}


# ------------------------------------------------------------------ consent
@app.post("/api/consent", dependencies=[Depends(require_token)])
async def set_consent(c: ConsentIn):
    ts = time.time()
    h = consent.receipt_hash(ts, c.party, c.granted)
    tx = await asyncio.to_thread(consent.anchor_on_solana, h)
    await asyncio.to_thread(store.insert_consent, ts, c.party, c.granted, h, tx)
    return {"ok": True, "receipt_hash": h, "solana_tx": tx}


@app.get("/api/consent")
async def get_consent():
    return {"events": await asyncio.to_thread(store.consent_log)}


# --------------------------------------------------------------- websockets
async def _ws_loop(ch: str, ws: WebSocket, hello: Optional[dict] = None):
    await hub.join(ch, ws)
    try:
        if hello:
            await ws.send_text(json.dumps(hello))
        while True:
            await ws.receive_text()  # keepalive pings from the page
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(ch, ws)


@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await _ws_loop("state", ws, state_msg())


@app.websocket("/ws/captions")
async def ws_captions(ws: WebSocket):
    await _ws_loop("captions", ws, {"type": "visibility", "captions_on": engine.state.captions_on})


@app.websocket("/ws/trainer")
async def ws_trainer(ws: WebSocket):
    await _ws_loop("trainer", ws)


# Static pages last, so /api and /ws routes win.
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
