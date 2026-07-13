"""Gold-label loader for the food-reconciliation benchmark.

The labels themselves — the highest-frequency *logged* foods — are a fingerprint
of the owner's diet, so they live in gitignored personal data
(``data/raw/personal/nutrition/food_gold_labels.json``), not in the repo. This
module ships only the loader and the scoring rule, so the harness is public and
reproducible-in-method while the personal food list stays private.

Labels are concept-level: a retrieved USDA food counts as correct if its
description contains any accept keyword (case-insensitive) — robust to USDA's many
near-duplicate rows and to preparation variants, while still failing the real
error cases (carrot→papaya, cucumber→borage).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_GOLD_PATH = Path(
    os.environ.get(
        "SYNCOLOGY_FOOD_GOLD",
        os.path.join(
            os.environ.get("SYNCOLOGY_DATA_DIR", "data"),
            "raw/personal/nutrition/food_gold_labels.json",
        ),
    )
)


def load_gold() -> dict[str, tuple[str, ...]]:
    """Return {raw Yazio product -> accept keywords}. Raises if the file is absent."""
    if not _GOLD_PATH.exists():
        raise FileNotFoundError(
            f"food gold labels not found at {_GOLD_PATH} (gitignored personal data); "
            "set SYNCOLOGY_FOOD_GOLD or place the file to run the benchmark."
        )
    raw = json.loads(_GOLD_PATH.read_text())
    return {product: tuple(kws) for product, kws in raw.items()}


def is_correct(description: str | None, keywords: tuple[str, ...]) -> bool:
    if not description:
        return False
    d = description.lower()
    return any(k in d for k in keywords)
