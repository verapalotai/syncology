"""Food reconciliation — Yazio logged foods → canonical USDA foods.

Cross-language (Hungarian/German → English), so fuzzy string matching (the
biomarker winner) fails here. Two signals are combined instead:

- **name**, via multilingual embeddings (Ollama ``bge-m3``, local) for candidate
  retrieval — weak alone on bare food words, but enough to shortlist;
- **macro fingerprint**, the per-100g energy/protein/fat/carbs both datasets
  carry, which is language-agnostic and disambiguates the shortlist.

A fuzzy baseline (:class:`FuzzyReconciler`) is kept for the write-up comparison.
Embeddings for the ~13.7k USDA foods are cached to disk (gitignored) so only the
Yazio side is embedded per run.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import duckdb
import httpx
import numpy as np

from syncology import config

EMBED_MODEL = "bge-m3"
_EMBED_URL = config.OLLAMA_BASE_URL.rsplit("/v1", 1)[0].rstrip("/") + "/api/embed"
# Per-100g scale factors so energy and macro grams are comparable in the vector.
_MACRO_SCALE = np.array([900.0, 100.0, 100.0, 100.0])  # energy, protein, fat, carbs


def embed(texts: list[str], model: str = EMBED_MODEL, batch: int = 256) -> np.ndarray:
    """L2-normalized embeddings for ``texts`` via the local Ollama endpoint."""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        r = httpx.post(_EMBED_URL, json={"model": model, "input": chunk}, timeout=300)
        r.raise_for_status()
        out.extend(r.json()["embeddings"])
    arr = np.asarray(out, dtype=np.float32)
    return arr / np.clip(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9, None)


def _macro_vec(energy, protein, fat, carbs) -> np.ndarray:
    v = np.array([energy or 0.0, protein or 0.0, fat or 0.0, carbs or 0.0], dtype=np.float32)
    return v / _MACRO_SCALE


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", _strip_accents(s or "").lower())).strip()


@dataclass
class Match:
    product: str
    fdc_id: int | None
    description: str | None
    cosine: float
    macro_sim: float
    score: float
    method: str


class FuzzyReconciler:
    """Baseline: normalized fuzzy match of the raw name against USDA descriptions."""

    def __init__(self, con: duckdb.DuckDBPyConnection, cutoff: float = 0.6):
        rows = con.execute("SELECT fdc_id, description FROM foods").fetchall()
        self.by_norm = {_norm(desc): (fid, desc) for fid, desc in rows}
        self.norms = list(self.by_norm)
        self.cutoff = cutoff

    def resolve(self, product: str) -> Match:
        m = difflib.get_close_matches(_norm(product), self.norms, n=1, cutoff=self.cutoff)
        if not m:
            return Match(product, None, None, 0.0, 0.0, 0.0, "none")
        fid, desc = self.by_norm[m[0]]
        score = difflib.SequenceMatcher(None, _norm(product), m[0]).ratio()
        return Match(product, fid, desc, 0.0, 0.0, score, "fuzzy")


class FoodReconciler:
    """Embedding candidate retrieval + macro-distance rerank."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        cache_dir: str | Path = "data/clean",
        top_k: int = 25,
        model: str = EMBED_MODEL,
    ):
        self.top_k = top_k
        self.model = model
        rows = con.execute(
            "SELECT fdc_id, description, energy_kcal, protein_g, fat_g, carbs_g FROM foods"
        ).fetchall()
        self.fdc_ids = np.array([r[0] for r in rows])
        self.descs = [r[1] for r in rows]
        self.fdc_macros = np.stack([_macro_vec(*r[2:6]) for r in rows])
        self.fdc_emb = self._load_or_build_embeddings(Path(cache_dir))

    def _load_or_build_embeddings(self, cache_dir: Path) -> np.ndarray:
        cache = cache_dir / f"fdc_embed_{self.model.replace('/', '_')}.npz"
        if cache.exists():
            z = np.load(cache, allow_pickle=False)
            if len(z["ids"]) == len(self.fdc_ids):
                return z["emb"]
        emb = embed(self.descs, self.model)
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez(cache, ids=self.fdc_ids, emb=emb)
        return emb

    def resolve_all(
        self, products: list[tuple], query_names: list[str] | None = None
    ) -> list[Match]:
        """``products`` = list of (name, energy, protein, fat, carbs).

        ``query_names`` overrides the text embedded for each product (e.g. English
        translations) while macros still come from ``products`` — this is the
        cross-language unlock: embed an English name against the English USDA
        descriptions instead of the raw Hungarian.
        """
        names = query_names if query_names is not None else [p[0] for p in products]
        q_emb = embed(names, self.model)
        q_macros = np.stack([_macro_vec(*p[1:5]) for p in products])
        cos = q_emb @ self.fdc_emb.T  # (Q, F)
        out: list[Match] = []
        for i, prod in enumerate(products):
            cand = np.argpartition(-cos[i], self.top_k)[: self.top_k]
            # macro similarity within the shortlist, combined with cosine
            md = np.linalg.norm(self.fdc_macros[cand] - q_macros[i], axis=1)
            macro_sim = 1.0 / (1.0 + md)
            combined = 0.5 * cos[i][cand] + 0.5 * macro_sim
            best = cand[int(np.argmax(combined))]
            out.append(
                Match(
                    prod[0], int(self.fdc_ids[best]), self.descs[best],
                    float(cos[i][best]), float(1.0 / (1.0 + np.linalg.norm(
                        self.fdc_macros[best] - q_macros[i]))),
                    float(combined.max()), "embed+macro",
                )
            )
        return out


def translate_food_names(
    names: list[str], model: str = config.BULK_MODEL, batch: int = 40
) -> list[str]:
    """Translate food names to concise English (the food itself, no brand)."""
    import instructor
    from anthropic import Anthropic
    from pydantic import BaseModel

    class Translations(BaseModel):
        english: list[str]

    client = instructor.from_anthropic(Anthropic(api_key=config.anthropic_api_key()))
    out: list[str] = []
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        numbered = "\n".join(f"{j + 1}. {n}" for j, n in enumerate(chunk))
        res = client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0,
            max_retries=2,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Translate each food/product name (Hungarian or German) to a "
                        "concise generic ENGLISH food name — the food itself, dropping "
                        "brands, quantities and packaging. Keep it short (e.g. "
                        "'carrot, raw', 'orange juice'). Return exactly one English "
                        f"string per input, in order.\n\n{numbered}"
                    ),
                }
            ],
            response_model=Translations,
        )
        eng = res.english[: len(chunk)] + [""] * max(0, len(chunk) - len(res.english))
        out.extend(eng)
    return out


def generate_hypothetical_docs(
    names: list[str], model: str = config.BULK_MODEL, batch: int = 30
) -> list[str]:
    """HyDE (Gao et al. 2022): a hypothetical corpus-style entry per food name.

    Instead of embedding the query name, embed an LLM-generated *hypothetical
    document* that lives in the same space as the USDA descriptions — a generic
    English food entry with a one-line description. Input names may be Hungarian /
    German (HyDE-from-raw, translation folded in) or already English.
    """
    import instructor
    from anthropic import Anthropic
    from pydantic import BaseModel

    class Docs(BaseModel):
        docs: list[str]

    client = instructor.from_anthropic(Anthropic(api_key=config.anthropic_api_key()))
    out: list[str] = []
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        numbered = "\n".join(f"{j + 1}. {n}" for j, n in enumerate(chunk))
        res = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=0,
            max_retries=2,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "For each food/product name (Hungarian, German or English), write "
                        "a concise ENGLISH food-database entry as it might appear in a "
                        "nutrition reference: the generic food name, then a short clause on "
                        "its form and main ingredients. Drop brands, quantities and "
                        "packaging. One entry per input, in order, e.g. "
                        "'Cucumber, raw — a crisp green vegetable eaten fresh'.\n\n"
                        f"{numbered}"
                    ),
                }
            ],
            response_model=Docs,
        )
        d = res.docs[: len(chunk)] + [""] * max(0, len(chunk) - len(res.docs))
        out.extend(d)
    return out


def build_food_map(
    con: duckdb.DuckDBPyConnection,
    cache_dir: str | Path = "data/clean",
    translate: bool = True,
) -> int:
    """Reconcile every Yazio food to a USDA food; materialize ``food_map``.

    With ``translate`` (default), names are first translated to English via the
    API, then matched — the cross-language accuracy unlock. Set ``False`` for the
    raw-embedding baseline used in the write-up comparison.
    """
    products = con.execute(
        "SELECT product, energy_kcal, protein_g, fat_g, carbs_g FROM yazio_foods"
    ).fetchall()
    query_names = None
    en_by_product: dict[str, str] = {}
    if translate:
        english = translate_food_names([p[0] for p in products])
        query_names = english
        en_by_product = {p[0]: en for p, en in zip(products, english)}

    matches = FoodReconciler(con, cache_dir).resolve_all(products, query_names=query_names)
    con.execute("DROP TABLE IF EXISTS food_map")
    con.execute(
        """
        CREATE TABLE food_map (
            product     VARCHAR PRIMARY KEY,
            en_name     VARCHAR,
            fdc_id      BIGINT,
            description VARCHAR,
            cosine      DOUBLE,
            macro_sim   DOUBLE,
            score       DOUBLE,
            method      VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO food_map VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (m.product, en_by_product.get(m.product), m.fdc_id, m.description,
             m.cosine, m.macro_sim, m.score, "translate+embed+macro" if translate else m.method)
            for m in matches
        ],
    )
    return len(matches)
