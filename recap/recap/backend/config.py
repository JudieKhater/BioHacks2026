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

# Gemini, for the end-of-day recap and the clinic summary. Empty = offline fallback.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Optional shared secret. If set, every write request needs header X-Recap-Token.
RECAP_TOKEN = os.getenv("RECAP_TOKEN", "").strip()

# Optional: anchor consent receipts on Solana devnet via the solana CLI.
SOLANA_ANCHOR = os.getenv("SOLANA_ANCHOR", "0") == "1"

# Fatigue thresholds (a clinician would tune these).
ON_THRESHOLD = float(os.getenv("ON_THRESHOLD", "55"))
OFF_THRESHOLD = float(os.getenv("OFF_THRESHOLD", "40"))
MIN_ON_SECONDS = float(os.getenv("MIN_ON_SECONDS", "120"))

FRONTEND_DIR = ROOT / "frontend"
