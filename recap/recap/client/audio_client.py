"""
Recap audio client: runs on the laptop the lapel mic is plugged into.

  mic --> every 5 s: features over the last 10 s --> POST /api/features
      --> speech to text --> POST /api/captions
            ElevenLabs Scribe v2 Realtime (word by word, word timestamps), or
            Gemini (one phrase at a time, cut at his pauses; ~1-2 s per phrase)

Usage (from the repo root):
    python client/audio_client.py                  # captions + features (best available speech-to-text)
    python client/audio_client.py --captions on    # also switch captions on now (else Auto decides)
    python client/audio_client.py --stt gemini     # force Gemini speech-to-text
    python client/audio_client.py --no-asr         # features only, estimated wpm
    python client/audio_client.py --list-devices   # find your lapel mic
    python client/audio_client.py --device 2

Needs: ELEVENLABS_API_KEY or GEMINI_API_KEY in .env (unless --no-asr), the backend running.
Audio never leaves this laptop except to the speech-to-text service for live
captions; nothing is recorded to disk.
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
import gemini  # noqa: E402  (also loads .env via config)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

SR = 16000
BLOCK_S = 0.1          # 100 ms audio chunks (Scribe likes small, steady chunks)
WINDOW_S = 10.0        # features computed over the last 10 s
HOP_S = 5.0            # ...every 5 s

SERVER = os.getenv("RECAP_SERVER", "http://127.0.0.1:8000")
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


class PhraseCutter:
    """
    Splits the mic stream into phrases at his pauses, for phrase-at-a-time
    speech-to-text. Energy-based: a 100 ms block is speech if it is 10 dB above
    the recent noise floor. A phrase ends after `pause_s` of quiet or `max_s`.
    """

    def __init__(self, pause_s: float = 0.9, max_s: float = 12.0, min_speech_s: float = 0.4):
        self.pause_blocks = max(1, int(round(pause_s / BLOCK_S)))
        self.max_blocks = int(max_s / BLOCK_S)
        self.min_speech_blocks = int(min_speech_s / BLOCK_S)
        self.levels: deque[float] = deque(maxlen=int(6 / BLOCK_S))   # last 6 s of block loudness
        self.preroll: deque[np.ndarray] = deque(maxlen=3)            # keep 300 ms before speech starts
        self.blocks: list[np.ndarray] = []
        self.speech_blocks = 0
        self.quiet_run = 0
        self.t_start = 0.0
        self.level_db = -90.0

    def add(self, block: np.ndarray):
        """Feed one int16 block. Returns (pcm bytes, t_start, t_end, speech seconds) when a phrase ends."""
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) / 32768.0
        db = 20 * np.log10(rms + 1e-9)
        self.level_db = db
        self.levels.append(db)
        floor = float(np.percentile(self.levels, 10)) if len(self.levels) >= 10 else -60.0
        speech = db > max(floor + 10.0, -55.0)

        if not self.blocks:
            if speech:
                self.blocks = list(self.preroll) + [block]
                self.t_start = time.time() - BLOCK_S * len(self.blocks)
                self.speech_blocks, self.quiet_run = 1, 0
            else:
                self.preroll.append(block)
            return None

        self.blocks.append(block)
        if speech:
            self.speech_blocks += 1
            self.quiet_run = 0
        else:
            self.quiet_run += 1
        if self.quiet_run >= self.pause_blocks or len(self.blocks) >= self.max_blocks:
            blocks, n_speech = self.blocks, self.speech_blocks
            self.blocks, self.speech_blocks, self.quiet_run = [], 0, 0
            self.preroll.clear()
            if n_speech < self.min_speech_blocks:
                return None  # a cough or a click
            pcm = np.concatenate(blocks).astype(np.int16).tobytes()
            return pcm, self.t_start, time.time(), n_speech * BLOCK_S
        return None


async def gemini_stt_loop(rec: Recorder, phrase_q: asyncio.Queue, keyterms: list[str]):
    """Transcribe each phrase with Gemini and post it as a finished caption line."""
    while True:
        pcm, t0, t1, speech_s = await phrase_q.get()
        if phrase_q.qsize() > 3:
            print("[gemini stt] falling behind; skipping a phrase")
            continue
        text = await asyncio.to_thread(gemini.transcribe, pcm, SR, keyterms)
        if text is None:
            print("[gemini stt] no transcript (rate-limited or offline); phrase skipped")
            continue
        if not text:
            continue
        # spread the words evenly over the phrase so the feature loop gets a speech rate
        words = text.split()
        for i, w in enumerate(words):
            rec.words.append({"text": w, "type": "word", "abs_start": t0 + (t1 - t0) * (i + 0.5) / len(words)})
        print(f"[{time.strftime('%H:%M:%S')}] \"{text}\"")
        await asyncio.to_thread(post, "/api/captions", {"text": text, "final": True})


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="input device index (see --list-devices)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--no-asr", action="store_true", help="no speech-to-text; features only")
    ap.add_argument("--stt", choices=["auto", "elevenlabs", "gemini"], default="auto",
                    help="speech-to-text: auto = ElevenLabs if its key is set, else Gemini")
    ap.add_argument("--captions", choices=["on", "auto", "off"], default=None,
                    help="set the caption mode when starting (default: leave Alex's setting)")
    ap.add_argument("--pause", type=float, default=0.9,
                    help="Gemini mode: seconds of quiet that end a phrase (default 0.9)")
    args = ap.parse_args()

    import sounddevice as sd  # pip install sounddevice
    if args.list_devices:
        print(sd.query_devices())
        return

    el_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    stt = None
    if not args.no_asr:
        if args.stt in ("auto", "elevenlabs") and el_key:
            stt = "elevenlabs"
        elif args.stt in ("auto", "gemini") and gemini.enabled():
            stt = "gemini"
        else:
            print("No ELEVENLABS_API_KEY or GEMINI_API_KEY for the chosen --stt: "
                  "running features only (estimated wpm, no captions).")
    use_asr = stt is not None

    if get("/api/health") is None:
        print(f"Can't reach the Recap server at {SERVER}. Start it first:\n"
              f"  .venv\\Scripts\\python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8000")
        return
    if args.captions:
        post("/api/mode", {"mode": args.captions})

    loop = asyncio.get_running_loop()
    rec = Recorder()
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    phrase_q: asyncio.Queue = asyncio.Queue()
    cutter = PhraseCutter(pause_s=args.pause)

    def on_audio(indata, frames, t, status):
        if status:
            print(f"[mic] {status}")
        block = indata[:, 0].copy()
        rec.add_audio(block)
        if stt == "elevenlabs":
            def put():
                if not audio_q.full():
                    audio_q.put_nowait(block.tobytes())
            loop.call_soon_threadsafe(put)
        elif stt == "gemini":
            phrase = cutter.add(block)
            if phrase:
                loop.call_soon_threadsafe(phrase_q.put_nowait, phrase)

    cfg = get("/api/config") or {}
    keyterms = cfg.get("keyterms", [])

    with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=int(SR * BLOCK_S),
                        device=args.device, callback=on_audio):
        dev = sd.query_devices(args.device if args.device is not None else sd.default.device[0])
        print(f"Listening on '{dev['name']}', speech-to-text: {stt or 'off'}, sending to {SERVER}. Ctrl+C to stop.")
        if stt == "gemini":
            print("Captions appear after each pause (about 1-2 s).")
        tasks = [feature_loop(rec, use_asr)]
        if stt == "elevenlabs":
            tasks.append(scribe_loop(rec, audio_q, el_key, keyterms))
        elif stt == "gemini":
            tasks.append(gemini_stt_loop(rec, phrase_q, keyterms))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
