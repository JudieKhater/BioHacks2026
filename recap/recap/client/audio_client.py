"""
Recap audio client: runs on the laptop the lapel mic is plugged into.

  mic --> every 5 s: features over the last 10 s --> POST /api/features
      --> ElevenLabs Scribe v2 Realtime (WebSocket) --> POST /api/captions
                                       word timestamps --> real speech rate

Usage (from the repo root):
    python client/audio_client.py                  # captions + features
    python client/audio_client.py --no-asr         # features only, estimated wpm
    python client/audio_client.py --list-devices   # find your lapel mic
    python client/audio_client.py --device 2

Needs: ELEVENLABS_API_KEY in .env (unless --no-asr), the backend running.
Audio never leaves this laptop except to Scribe for live captions; nothing is
recorded to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
import features  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SR = 16000
BLOCK_S = 0.1          # 100 ms audio chunks (Scribe likes small, steady chunks)
WINDOW_S = 10.0        # features computed over the last 10 s
HOP_S = 5.0            # ...every 5 s

SERVER = os.getenv("RECAP_SERVER", "http://localhost:8000")
TOKEN = os.getenv("RECAP_TOKEN", "")
SCRIBE_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


def post(path: str, body: dict) -> dict | None:
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Recap-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:  # keep going: the demo must survive a hiccup
        print(f"[post {path}] {e}")
        return None


def get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(SERVER + path, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[get {path}] {e}")
        return None


class Recorder:
    """Holds the last WINDOW_S of audio plus Scribe words with wall-clock times."""

    def __init__(self):
        self.audio = deque(maxlen=int(WINDOW_S / BLOCK_S))
        self.words: deque[dict] = deque(maxlen=2000)

    def add_audio(self, block: np.ndarray):
        self.audio.append(block)

    def add_words(self, words: list[dict], audio_t0: float):
        for w in words or []:
            if w.get("type", "word") != "word" or "start" not in w:
                continue
            self.words.append({**w, "abs_start": audio_t0 + float(w["start"])})

    def window(self):
        if not self.audio:
            return None, []
        x = np.concatenate(list(self.audio)).astype(np.float32) / 32768.0
        t_end = time.time()
        t_start = t_end - len(x) / SR
        ws = [w for w in self.words if t_start <= w["abs_start"] <= t_end]
        return x, ws


async def feature_loop(rec: Recorder, use_asr: bool):
    while True:
        await asyncio.sleep(HOP_S)
        x, words = rec.window()
        if x is None:
            continue
        f = features.extract(x, SR, asr_words=words if use_asr else None)
        f["ts"] = time.time()
        state = await asyncio.to_thread(post, "/api/features", f)
        if state:
            print(f"[{time.strftime('%H:%M:%S')}] wpm={f['wpm']} ({f['wpm_source']}) "
                  f"db={f['loudness_db']} breath_words={f['breath_group_words']} "
                  f"score={state['score']:.0f} battery={state['battery']} captions={'ON' if state['captions_on'] else 'off'}")


async def scribe_loop(rec: Recorder, audio_q: asyncio.Queue, api_key: str, keyterms: list[str]):
    import websockets  # pip install websockets

    params = [("model_id", "scribe_v2_realtime"), ("audio_format", "pcm_16000"),
              ("commit_strategy", "vad"), ("include_timestamps", "true"), ("language_code", "en")]
    params += [("keyterms", k) for k in keyterms[:50] if len(k) <= 20]
    url = SCRIBE_URL + "?" + urllib.parse.urlencode(params)
    headers = {"xi-api-key": api_key}

    while True:  # reconnect forever
        try:
            try:
                conn = websockets.connect(url, additional_headers=headers, max_size=None)
            except TypeError:  # websockets < 14
                conn = websockets.connect(url, extra_headers=headers, max_size=None)
            async with conn as ws:
                print("[scribe] connected")
                audio_t0 = None

                async def sender():
                    nonlocal audio_t0
                    while True:
                        chunk: bytes = await audio_q.get()
                        if audio_t0 is None:
                            audio_t0 = time.time() - BLOCK_S
                        await ws.send(json.dumps({
                            "message_type": "input_audio_chunk",
                            "audio_base_64": base64.b64encode(chunk).decode(),
                            "commit": False,
                            "sample_rate": SR,
                        }))

                async def receiver():
                    async for raw in ws:
                        m = json.loads(raw)
                        kind = m.get("message_type")
                        if kind == "partial_transcript" and m.get("text"):
                            await asyncio.to_thread(post, "/api/captions", {"text": m["text"], "final": False})
                        elif kind == "committed_transcript_with_timestamps":
                            rec.add_words(m.get("words") or [], audio_t0 or time.time())
                            if m.get("text"):
                                await asyncio.to_thread(post, "/api/captions", {"text": m["text"], "final": True})
                        elif kind in ("session_started",):
                            print("[scribe] session", m.get("session_id"))
                        elif "error" in m:
                            print(f"[scribe] {kind}: {m.get('error')}")

                await asyncio.gather(sender(), receiver())
        except Exception as e:
            print(f"[scribe] disconnected ({e}); retrying in 2 s")
            # drop stale audio so captions resume in real time
            while not audio_q.empty():
                audio_q.get_nowait()
            await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="input device index (see --list-devices)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--no-asr", action="store_true", help="skip Scribe; features only")
    args = ap.parse_args()

    import sounddevice as sd  # pip install sounddevice
    if args.list_devices:
        print(sd.query_devices())
        return

    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    use_asr = not args.no_asr and bool(api_key)
    if not args.no_asr and not api_key:
        print("No ELEVENLABS_API_KEY set: running features only (estimated wpm, no captions).")

    loop = asyncio.get_running_loop()
    rec = Recorder()
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=200)

    def on_audio(indata, frames, t, status):
        if status:
            print(f"[mic] {status}")
        block = indata[:, 0].copy()
        rec.add_audio(block)
        if use_asr:
            def put():
                if not audio_q.full():
                    audio_q.put_nowait(block.tobytes())
            loop.call_soon_threadsafe(put)

    cfg = get("/api/config") or {}
    keyterms = cfg.get("keyterms", [])

    with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=int(SR * BLOCK_S),
                        device=args.device, callback=on_audio):
        print(f"Listening… sending to {SERVER}. Ctrl+C to stop.")
        tasks = [feature_loop(rec, use_asr)]
        if use_asr:
            tasks.append(scribe_loop(rec, audio_q, api_key, keyterms))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
