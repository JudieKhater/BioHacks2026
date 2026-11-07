"""
Recap backend (FastAPI).

Run from the repo root:
    uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
then open http://localhost:8000

Data flow
---------
  audio_client.py  --POST /api/features-->  fatigue engine --> DB (numbers only)
                   --POST /api/captions-->  stutter cleanup + word prediction
                                            --> /ws/captions (only while captions are on)
                                                -> captions.html and the desktop overlay
                                            --> transcript (memory) --> live notes /ws/notes
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
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))  # so `import config` works under uvicorn

import config  # noqa: E402
import consent  # noqa: E402
import gemini  # noqa: E402
import predictor  # noqa: E402
import recap_ai  # noqa: E402
import speech_symptoms as symptoms  # noqa: E402
from db import make_store  # noqa: E402
from fatigue import FatigueEngine, FeatureWindow  # noqa: E402

store = make_store(config.DATABASE_URL, config.SQLITE_PATH)
engine = FatigueEngine(on_threshold=config.ON_THRESHOLD, off_threshold=config.OFF_THRESHOLD,
                       min_on_seconds=config.MIN_ON_SECONDS)

# Subject vocabulary: boosts Scribe accuracy (sent by the audio client as keyterms)
# and helps the offline recap fallback pick content sentences. Lesson material adds more.
KEYTERMS = [t.strip() for t in os.getenv(
    "RECAP_KEYTERMS",
    "mitochondria,photosynthesis,chloroplast,enzyme,osmosis,diffusion,ATP,DNA,RNA,"
    "transcription,translation,meiosis,mitosis,homeostasis,neuron,membrane,protein,cellular respiration",
).split(",") if t.strip()]

library = predictor.ContextLibrary()
_loaded = library.load_dir(config.CONTEXT_DIR)
print(f"[context] {_loaded} document(s) from {config.CONTEXT_DIR}")

# In memory only, for today. Never written to disk.
# Each line: {id, ts, text (his words, stutters removed), raw, predicted, source}
transcript_today: list[dict] = []
transcript_day = datetime.now().strftime("%Y-%m-%d")
latest_effort: dict = {"value": None, "ts": 0.0}
latest_features: dict = {}
symptom_monitor = symptoms.SymptomMonitor()
live = {"predict": config.PREDICT_DEFAULT}
EMPTY_NOTES = {"topic": "", "summary": [], "key_terms": [], "related": [], "questions": [], "source": "empty"}
notes_state: dict = {**EMPTY_NOTES, "updated": 0.0}
notes_dirty = {"value": False, "force": False}
line = {"id": 1, "raw": "", "ts": 0.0}          # the caption line in progress
recent_hints: deque[str] = deque(maxlen=8)
_tasks: dict[str, Optional[asyncio.Task]] = {"stall": None}
_gemini_live = asyncio.Semaphore(1)
_live_calls = {"last": 0.0}


def all_keyterms() -> list[str]:
    return list(dict.fromkeys(library.keyterms() + KEYTERMS))


# ------------------------------------------------------------------- auth
def require_token(x_recap_token: Optional[str] = Header(default=None)):
    if config.RECAP_TOKEN and x_recap_token != config.RECAP_TOKEN:
        raise HTTPException(status_code=401, detail="missing or wrong X-Recap-Token")


# ------------------------------------------------------------- websockets
class Hub:
    def __init__(self):
        self.channels: dict[str, set[WebSocket]] = {"state": set(), "captions": set(), "trainer": set(),
                                                   "notes": set()}

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
    snap["predict"] = live["predict"]
    return snap


async def push_state():
    msg = state_msg()
    await hub.send("state", msg)
    await hub.send("captions", {"type": "visibility", "captions_on": msg["captions_on"], "predict": live["predict"]})


def symptoms_msg() -> dict:
    snap = engine.snapshot()
    disfl = symptom_monitor.summary()
    return {
        "type": "symptoms",
        "subsystems": symptoms.subsystems(snap, latest_features, disfl),
        "guidance": symptoms.guidance(snap, latest_features, disfl),
        "disfluency": disfl,
        "predictions_used": symptom_monitor.predictions_used,
        "latest": {k: latest_features.get(k) for k in ("wpm", "loudness_db", "breath_group_words", "pause_pct")},
        "battery": snap["battery"],
        "markers": {"speech_loss_wpm": symptoms.SPEECH_LOSS_WPM,
                    "intelligibility_loss_pct": symptoms.INTELLIGIBILITY_LOSS_PCT},
    }


def _clear_day_memory():
    transcript_today.clear()
    symptom_monitor.reset()
    notes_state.clear()
    notes_state.update({**EMPTY_NOTES, "updated": 0.0})
    recent_hints.clear()


def _roll_day():
    """Start a fresh day at midnight: clear transcript, notes, reset the daily score."""
    global transcript_day
    today = datetime.now().strftime("%Y-%m-%d")
    if today != transcript_day:
        _clear_day_memory()
        engine.reset_day()
        transcript_day = today


# --------------------------------------------------------- live AI loops
async def _notes_loop():
    """Rebuild the live notes every NOTES_EVERY_SECONDS while there's new speech."""
    while True:
        await asyncio.sleep(2)
        due = time.time() - notes_state.get("updated", 0) >= config.NOTES_EVERY_SECONDS
        if notes_dirty["value"] and (due or notes_dirty["force"]):
            notes_dirty.update(value=False, force=False)
            lines = [t["text"] for t in transcript_today]
            try:
                notes = await asyncio.to_thread(predictor.build_notes, lines, library, dict(notes_state))
            except Exception as e:  # never let the loop die
                print(f"[notes] {e}")
                continue
            notes_state.clear()
            notes_state.update({**notes, "updated": time.time()})
            await hub.send("notes", {"type": "notes", **notes_state})


async def _enrich_library():
    ok = await asyncio.to_thread(library.enrich_with_gemini)
    if ok:
        print(f"[context] Gemini glossary: {len(library.glossary)} terms")
        await hub.send("notes", {"type": "context", **library.summary()})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(_notes_loop()), asyncio.create_task(_enrich_library())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Recap", version="2.0", lifespan=lifespan)


async def _send_assist(line_id: int, pred: predictor.Prediction, final: bool):
    msg = {"type": "assist", "id": line_id, "final": final, **pred.as_dict()}
    if engine.state.captions_on:
        await hub.send("captions", msg)
    await hub.send("notes", msg)
    if final:
        for t in reversed(transcript_today):
            if t["id"] == line_id:
                t.update(predicted=pred.predicted, source=pred.source)
                break


async def _predict(line_id: int, raw: str, final: bool):
    """Instant offline guess, then a Gemini refinement that replaces it."""
    pred = predictor.offline_predict(raw, library, stalled=True)
    if not pred.clean:
        return
    shown = pred.predicted and pred.confidence >= config.PREDICT_MIN_CONFIDENCE
    if shown:
        await _send_assist(line_id, pred, final)

    # Throttle live calls: free-tier quotas are a few requests a minute.
    now = time.time()
    throttled = now - _live_calls["last"] < config.GEMINI_LIVE_MIN_INTERVAL
    can_call = gemini.available(config.GEMINI_LIVE_MODEL) or gemini.available(config.GEMINI_MODEL)
    if throttled or _gemini_live.locked() or not can_call:
        if final and shown:
            symptom_monitor.predictions_used += 1
        return
    _live_calls["last"] = now
    async with _gemini_live:
        recent = [t["text"] for t in transcript_today[-6:] if t["id"] != line_id]
        better = await asyncio.to_thread(predictor.gemini_predict, raw, pred, library, recent,
                                         notes_state.get("topic", ""))
    if better is None:
        if final and shown:
            symptom_monitor.predictions_used += 1
        return
    if not final and (line["id"] != line_id or line["raw"] != raw):
        return  # he kept talking; the guess is stale
    if better.confidence < config.PREDICT_MIN_CONFIDENCE:
        better.predicted = ""  # Gemini isn't sure: show nothing rather than a wrong guess
    if better.predicted or shown:
        await _send_assist(line_id, better, final)
    if final and better.predicted:
        symptom_monitor.predictions_used += 1


async def _stall_watch(line_id: int, raw: str):
    """If a partial caption stops changing, he may have stalled: offer a prediction."""
    await asyncio.sleep(config.PREDICT_STALL_SECONDS)
    if line["id"] == line_id and line["raw"] == raw and live["predict"]:
        await _predict(line_id, raw, final=False)


# ----------------------------------------------------------------- models
class FeaturesIn(BaseModel):
    ts: Optional[float] = None
    wpm: Optional[float] = None
    loudness_db: Optional[float] = None
    breath_group_words: Optional[float] = None
    pause_pct: Optional[float] = None
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


class PredictIn(BaseModel):
    on: bool


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


class ContextIn(BaseModel):
    title: str = "Pasted notes"
    text: str


# --------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    return {"ok": True, "store": store.kind, "gemini": gemini.enabled(), "solana": config.SOLANA_ANCHOR,
            "models": {"notes": config.GEMINI_MODEL, "live": config.GEMINI_LIVE_MODEL},
            "context_docs": len(library.docs)}


@app.get("/api/config")
def client_config():
    """What the audio client needs (keyterms for Scribe, including lesson vocabulary)."""
    return {"keyterms": all_keyterms()}


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
        latest_features.update(ts=ts, wpm=f.wpm, loudness_db=f.loudness_db,
                               breath_group_words=f.breath_group_words, pause_pct=f.pause_pct)
        await asyncio.to_thread(store.insert_features, ts, row)
    await push_state()
    await hub.send("notes", symptoms_msg())
    return state_msg()


@app.post("/api/sensor", dependencies=[Depends(require_token)])
def post_sensor(s: SensorIn):
    latest_effort.update(value=s.effort, ts=time.time())
    return {"ok": True}


@app.post("/api/captions", dependencies=[Depends(require_token)])
async def post_caption(c: CaptionIn):
    _roll_day()
    raw = c.text.strip()
    if not raw:
        return {"ok": True}
    line_id = line["id"]
    clean_text, disfl = symptoms.clean(raw)
    line.update(raw=raw, ts=time.time())
    if _tasks["stall"] and not _tasks["stall"].done():
        _tasks["stall"].cancel()

    msg = {"type": "caption", "id": line_id, "text": clean_text or raw, "raw": raw, "final": c.final}
    # Captions only reach the room while they're on: Alex's call (or auto).
    if engine.state.captions_on:
        await hub.send("captions", msg)

    if c.final:
        line.update(id=line_id + 1, raw="")
        entry = {"id": line_id, "ts": time.time(), "text": clean_text or raw, "raw": raw,
                 "predicted": "", "source": "none"}
        transcript_today.append(entry)
        symptom_monitor.add(disfl)
        notes_dirty["value"] = True
        await hub.send("notes", {"type": "line", **entry, "disfluency": disfl.as_dict()})
        if not disfl.trailing:
            library.learn(clean_text)  # his own phrasing helps future predictions
        if live["predict"] and (disfl.trailing or symptoms.is_incomplete(clean_text)):
            asyncio.create_task(_predict(line_id, raw, final=True))
        hint = predictor.context_hint(clean_text, library, notes_state, list(recent_hints))
        if hint:
            recent_hints.append(hint["term"])
            hint_msg = {"type": "hint", **hint}
            if engine.state.captions_on:
                await hub.send("captions", hint_msg)
            await hub.send("notes", hint_msg)
    elif live["predict"]:
        _tasks["stall"] = asyncio.create_task(_stall_watch(line_id, raw))
    return {"ok": True, "shown": engine.state.captions_on, "id": line_id}


@app.post("/api/mode", dependencies=[Depends(require_token)])
async def set_mode(m: ModeIn):
    engine.set_mode(m.mode, time.time())
    await push_state()
    return state_msg()


@app.post("/api/predict", dependencies=[Depends(require_token)])
async def set_predict(p: PredictIn):
    """Alex turns predicted words on or off."""
    live["predict"] = p.on
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
    _clear_day_memory()
    latest_features.clear()
    await push_state()
    await hub.send("notes", {"type": "reset"})
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


@app.get("/api/clinic/projection")
async def clinic_projection(days: int = 90, onset: str = "bulbar"):
    if not await asyncio.to_thread(_clinic_sharing_on):
        return {"ok": False, "paused": True}
    t = await asyncio.to_thread(store.trends, days)
    return symptoms.project_decline(t, onset=onset)


# ------------------------------------------------- notes, symptoms, context
@app.get("/api/notes")
def get_notes():
    return {"notes": notes_state, "transcript": transcript_today[-200:], "predict": live["predict"]}


@app.post("/api/notes/refresh", dependencies=[Depends(require_token)])
def refresh_notes():
    notes_dirty.update(value=True, force=True)
    return {"ok": True}


@app.get("/api/symptoms")
def get_symptoms():
    return {**symptoms_msg(), "weekly_check": symptoms.WEEKLY_CHECK}


@app.get("/api/context")
def get_context():
    return library.summary()


async def _context_changed():
    asyncio.create_task(_enrich_library())
    await hub.send("notes", {"type": "context", **library.summary()})


@app.post("/api/context", dependencies=[Depends(require_token)])
async def add_context(c: ContextIn):
    if not c.text.strip():
        raise HTTPException(400, "empty text")
    library.add(c.title, c.text)
    await _context_changed()
    return library.summary()


@app.post("/api/context/upload", dependencies=[Depends(require_token)])
async def upload_context(request: Request, name: str = "upload.pdf"):
    """Raw file body (PDF, .txt or .md). No multipart, so no extra dependency."""
    data = await request.body()
    if len(data) > 20_000_000:
        raise HTTPException(413, "file too large (20 MB max)")
    if name.lower().endswith(".pdf"):
        text = await asyncio.to_thread(predictor.pdf_text, data)
    else:
        text = data.decode("utf-8", errors="replace")
    if not text or not text.strip():
        raise HTTPException(400, "no readable text (scanned PDFs need OCR first)")
    library.add(os.path.splitext(os.path.basename(name))[0].replace("_", " "), text, source=name)
    await _context_changed()
    return library.summary()


@app.delete("/api/context", dependencies=[Depends(require_token)])
async def clear_context():
    library.clear()
    await _context_changed()
    return library.summary()


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
    # His words only (stutters removed). Predicted words never enter the recap,
    # and sentences he didn't finish aren't offered as recap material.
    lines = [t["text"] for t in transcript_today if not symptoms.is_incomplete(t["text"])]
    return await asyncio.to_thread(recap_ai.draft_recap, lines, all_keyterms())


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
async def _ws_loop(ch: str, ws: WebSocket, hello: Optional[list[dict]] = None):
    await hub.join(ch, ws)
    try:
        for msg in hello or []:
            await ws.send_text(json.dumps(msg))
        while True:
            await ws.receive_text()  # keepalive pings from the page
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(ch, ws)


@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await _ws_loop("state", ws, [state_msg()])


@app.websocket("/ws/captions")
async def ws_captions(ws: WebSocket):
    await _ws_loop("captions", ws, [{"type": "visibility", "captions_on": engine.state.captions_on,
                                     "predict": live["predict"]}])


@app.websocket("/ws/notes")
async def ws_notes(ws: WebSocket):
    await _ws_loop("notes", ws, [{"type": "notes", **notes_state}, symptoms_msg(),
                                 {"type": "context", **library.summary()}])


@app.websocket("/ws/trainer")
async def ws_trainer(ws: WebSocket):
    await _ws_loop("trainer", ws)


# Static pages last, so /api and /ws routes win.
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
