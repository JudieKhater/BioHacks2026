"""
One small Gemini REST client shared by the recap, notes and live prediction.

- The key goes in the `x-goog-api-key` header, never in the URL (URLs end up in logs).
- `thinking="minimal"` keeps latency low; if a model rejects that setting we
  retry once without it.
- If a model has been retired (404) we switch to GEMINI_FALLBACK_MODEL and
  remember the swap.
- If we're rate-limited (429) that model is paused for the retry delay Google
  gives (default 60 s) instead of hammering the quota.
- Any failure returns None so every caller can fall back to its offline path.
"""
from __future__ import annotations

import base64
import io
import json
import re
import threading
import wave
import time
import urllib.error
import urllib.request
from typing import Optional

import config

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FALLBACK_MODEL = "gemini-flash-latest"

_lock = threading.Lock()
_cooldown_until: dict[str, float] = {}   # model -> time it may be called again
_replaced: dict[str, str] = {}           # retired model -> replacement


def enabled() -> bool:
    return bool(config.GEMINI_API_KEY)


def available(model: Optional[str] = None) -> bool:
    """Key set and the model isn't paused after a rate limit."""
    model = _replaced.get(model or config.GEMINI_MODEL, model or config.GEMINI_MODEL)
    return enabled() and time.time() >= _cooldown_until.get(model, 0.0)


def _retry_delay(detail: str) -> float:
    m = re.search(r'retry(?:Delay| in)"?:?\s*"?(\d+(?:\.\d+)?)s', detail, flags=re.I)
    return min(300.0, float(m.group(1))) if m else 60.0


def _post(model: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        URL.format(model=model), data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate(prompt: str, *, model: Optional[str] = None, json_mode: bool = False,
             temperature: float = 0.2, thinking: Optional[str] = "minimal",
             timeout: float = 20.0, fallback_model: Optional[str] = None,
             audio: Optional[tuple[bytes, str]] = None,
             fallback_thinking: Optional[str] = "minimal") -> Optional[str]:
    """
    Return the model's text, or None if there's no key or the call failed.
    `fallback_model` is tried once if `model` is overloaded (503) or rate-limited.
    `audio` is (bytes, mime type), sent before the prompt (speech-to-text).
    """
    if not enabled():
        return None
    requested = model or config.GEMINI_MODEL
    model = _replaced.get(requested, requested)

    def try_fallback():
        if fallback_model and fallback_model != requested:
            return generate(prompt, model=fallback_model, json_mode=json_mode, temperature=temperature,
                            thinking=fallback_thinking, timeout=timeout, audio=audio)
        return None

    if not available(model):
        return try_fallback()  # this model is cooling down after a rate limit
    gen: dict = {"temperature": temperature}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    if thinking:
        gen["thinkingConfig"] = {"thinkingLevel": thinking}
    parts_in: list[dict] = []
    if audio:
        parts_in.append({"inline_data": {"mime_type": audio[1], "data": base64.b64encode(audio[0]).decode()}})
    parts_in.append({"text": prompt})
    body = {"contents": [{"role": "user", "parts": parts_in}], "generationConfig": gen}

    for _attempt in range(3):
        try:
            data = _post(model, body, timeout)
            parts = (data["candidates"][0].get("content") or {}).get("parts") or []
            # transcription models answer in an `audioTranscription` part instead of `text`
            return "".join(p.get("text") or (p.get("audioTranscription") or {}).get("text", "")
                           for p in parts if not p.get("thought"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:1500]
            except Exception:
                pass
            if e.code == 400 and "hinking" in detail and "thinkingConfig" in gen:
                gen.pop("thinkingConfig", None)  # model doesn't support that thinking level
                continue
            if e.code == 404 and model != FALLBACK_MODEL:
                print(f"[gemini] {model} unavailable; switching to {FALLBACK_MODEL}")
                with _lock:
                    _replaced[requested] = FALLBACK_MODEL
                model = FALLBACK_MODEL
                continue
            if e.code in (429, 503):
                # 429: our quota; 503: Google's model is overloaded. Pause this model either way.
                wait = _retry_delay(detail) if e.code == 429 else 30.0
                with _lock:
                    _cooldown_until[model] = time.time() + wait
                print(f"[gemini {model}] HTTP {e.code}; pausing it for {wait:.0f} s")
                return try_fallback()
            print(f"[gemini {model}] HTTP {e.code}: {detail[:300]}")
            return None
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError, OSError) as e:
            print(f"[gemini {model}] falling back: {e}")
            return None
    return None


def wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


_NO_SPEECH = re.compile(r"^\W*(\[[^\]]*\]|\([^)]*\)|no speech|silence|inaudible)?\W*$", flags=re.I)


def transcribe(pcm16: bytes, sample_rate: int = 16000, keyterms: Optional[list[str]] = None,
               timeout: float = 15.0) -> Optional[str]:
    """
    Speech to text for one phrase of 16-bit mono PCM. Verbatim: repeats, fillers and
    cut-off words are kept, because the speech check counts them.
    Uses GEMINI_STT_MODEL, falling back to GEMINI_MODEL if it's busy or rate-limited.
    Returns "" for no speech, None on failure.
    """
    terms = ", ".join((keyterms or [])[:40])
    prompt = ("Transcribe this classroom speech verbatim, in English. Keep repeated words, fillers (um, uh) "
              "and false starts; write a word that was cut off with a trailing hyphen (e.g. 'oculo-'). "
              "Do not correct or complete anything. Return only the transcript, or nothing if there is no speech."
              + (f" Subject terms that may come up: {terms}." if terms else ""))
    text = generate(prompt, model=config.GEMINI_STT_MODEL, fallback_model=config.GEMINI_MODEL, thinking=None,
                    fallback_thinking="minimal", temperature=0.0, timeout=timeout,
                    audio=(wav_bytes(pcm16, sample_rate), "audio/wav"))
    if text is None:
        return None
    text = " ".join(text.split())
    return "" if _NO_SPEECH.match(text) else text


def generate_json(prompt: str, **kw) -> Optional[dict]:
    """Like generate(), parsed as a JSON object (None if not valid JSON)."""
    raw = generate(prompt, json_mode=True, **kw)
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[gemini] not JSON: {raw[:120]!r}")
        return None
    return out if isinstance(out, dict) else None
