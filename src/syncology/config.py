"""Runtime configuration.

Loads ``.env`` (gitignored; only ``.env.example`` is tracked) into the process
environment and exposes settings. Secrets are always read from the environment —
never hard-coded, logged, or printed.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env if present; no-op if absent

# Model choices. Bulk structured extraction (e.g. lab PDFs) uses a cheap, fast
# model; note the cost in write-ups. Kept here so callers don't hard-code ids.
BULK_MODEL = "claude-haiku-4-5-20251001"


@lru_cache
def anthropic_api_key() -> str:
    """Return the Anthropic API key from the environment, or raise if unset."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (gitignored) — see .env.example."
        )
    return key


def has_anthropic_key() -> bool:
    """True if an Anthropic key is configured (for optional/guarded code paths)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
