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
import re
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


_PLURAL = (("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("es", ""), ("s", ""))


def _stem(word: str) -> str:
    """Crude singularizer so 'cherries'/'cherry' and 'potatoes'/'potato' match."""
    for suf, rep in _PLURAL:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)] + rep
    return word


def _stems(text: str) -> set[str]:
    return {_stem(t) for t in re.findall(r"[a-z]+", text.lower())}


# Difficulty strata for the benchmark (assigned from the food name).
_REGIONAL = ("lecsó", "lecso", "ajvar", "körözött", "korozott", "túró rudi", "rakott",
             "pörkölt", "gulyás", "bundás", "meggyleves", "kovász")
_BRAND = ("dmbio", "oatly", "alnatura", "the bridge", "barista", "jersey miracle",
          "alpro", "danone", "milbona", "müller", "yfood", "bio ")
_PREP = ("boiled", "cooked", "roasted", "fried", "steamed", "grilled", "baked",
         "smoked", "dried", "ground", "pickled", "fermented", "jam", "juice",
         "paste", "sauce", "spread", "puree", "syrup", "brew")
_MODIFIER = ("red", "green", "yellow", "white", "purple", "baby", "wild", "cherry",
             "sour", "sweet", "hot", "black", "dark", "spicy", "fresh", "raw")


def classify(product: str, en_name: str | None = None) -> str:
    """Difficulty stratum: regional | branded | prepared | compound | simple."""
    p = (product or "").lower()
    en = (en_name or "").lower()
    if any(r in p or r in en for r in _REGIONAL):
        return "regional"
    if any(b in p for b in _BRAND):
        return "branded"
    words = en.split()
    if any(w in _PREP for w in words):
        return "prepared"
    if len(words) >= 2 and words[0] in _MODIFIER:
        return "compound"
    return "simple"


def is_correct(description: str | None, keywords: tuple[str, ...]) -> bool:
    """True if any keyword's word-stems are all present in the description.

    Token-set, stem-aware, order-insensitive — so "goat cheese" matches
    "Cheese, goat" and "cherry" matches "Cherries, raw", while still rejecting the
    real errors (green apple → "Green peas", americano → "Cheese, American").
    """
    if not description:
        return False
    dstems = _stems(description)
    for kw in keywords:
        kstems = _stems(kw)
        if kstems and kstems <= dstems:
            return True
    return False
