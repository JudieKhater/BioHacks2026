"""Settings, read from environment variables (or a .env file in the repo root)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load .env if python-dotenv is installed; it's optional.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# Tiger Data / TimescaleDB connection string. Empty = local SQLite file.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("SQLITE_PATH", str(ROOT / "recap.db"))

# Gemini. Empty key = offline fallbacks everywhere, so the demo never breaks.
#   GEMINI_MODEL       notes, recap, clinic summary (a few seconds is fine)
#   GEMINI_LIVE_MODEL  live word prediction under captions (needs to answer in ~1 s)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-lite").strip()
#   GEMINI_STT_MODEL   mic speech-to-text in client/audio_client.py when there's no ElevenLabs key
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-3.5-transcribe").strip()

# Lesson / reference material (PDF, .txt, .md) loaded at startup. Captions,
# word prediction and notes are grounded in it.
CONTEXT_DIR = Path(os.getenv("RECAP_CONTEXT_DIR", "").strip() or str(ROOT / "context"))

# Predictive captions: guess the rest of a sentence when Alex stalls or tires.
# Predictions are always shown visually distinct and never enter the recap.
PREDICT_DEFAULT = os.getenv("PREDICT_DEFAULT", "1") == "1"
PREDICT_MIN_CONFIDENCE = float(os.getenv("PREDICT_MIN_CONFIDENCE", "0.45"))
PREDICT_STALL_SECONDS = float(os.getenv("PREDICT_STALL_SECONDS", "1.2"))
# Minimum gap between live Gemini prediction calls (the offline guess fills in between).
GEMINI_LIVE_MIN_INTERVAL = float(os.getenv("GEMINI_LIVE_MIN_INTERVAL", "4"))
# How often the live notes are rebuilt while he talks.
NOTES_EVERY_SECONDS = float(os.getenv("NOTES_EVERY_SECONDS", "20"))

# Optional shared secret. If set, every write request needs header X-Recap-Token.
RECAP_TOKEN = os.getenv("RECAP_TOKEN", "").strip()

# Optional: anchor consent receipts on Solana devnet via the solana CLI.
SOLANA_ANCHOR = os.getenv("SOLANA_ANCHOR", "0") == "1"

# Fatigue thresholds (a clinician would tune these).
ON_THRESHOLD = float(os.getenv("ON_THRESHOLD", "55"))
OFF_THRESHOLD = float(os.getenv("OFF_THRESHOLD", "40"))
MIN_ON_SECONDS = float(os.getenv("MIN_ON_SECONDS", "120"))

FRONTEND_DIR = ROOT / "frontend"
