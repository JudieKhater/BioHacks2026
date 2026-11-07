"""
Consent receipts: Alex decides who sees his data, and can change his mind.

Each grant/revoke produces a SHA-256 receipt of (time, party, decision).
No health data is ever in the receipt.

OPTIONAL (Solana prize, experimental): if SOLANA_ANCHOR=1 and the `solana`
CLI is installed and configured for devnet with a funded keypair, the receipt
hash is written to devnet as a memo on a zero-SOL self-transfer. Verify the
command on your machine first: `solana transfer --help` should list --with-memo.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from typing import Optional

import config


def receipt_hash(ts: float, party: str, granted: bool) -> str:
    payload = json.dumps({"ts": round(ts, 3), "party": party, "granted": granted}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def anchor_on_solana(h: str) -> Optional[str]:
    if not config.SOLANA_ANCHOR or not shutil.which("solana"):
        return None
    try:
        addr = subprocess.run(["solana", "address"], capture_output=True, text=True, timeout=10).stdout.strip()
        out = subprocess.run(
            ["solana", "transfer", addr, "0", "--with-memo", f"recap-consent:{h}",
             "--allow-unfunded-recipient", "--url", "devnet", "--output", "json"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            print(f"[solana] {out.stderr.strip()}")
            return None
        data = json.loads(out.stdout)
        return data.get("signature")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        print(f"[solana] skipped: {e}")
        return None
