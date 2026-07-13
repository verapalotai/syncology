# Food Entity Resolution — Cross-Lingual Embedding Benchmark

Resolving **875 Hungarian/German logged foods** (Yazio) to a canonical **13,692
USDA FoodData Central** food vocabulary, so nutrition can be trended against a
reference and A5's voice `log_meal` can turn free text into nutrients. Methods
and aggregate metrics are public here; no personal measurements appear.

## Why this is a different problem from biomarker resolution

Biomarker resolution (write-up #1) was won by a **rule-based** resolver: a small
closed vocabulary (~84 canonical analytes), where normalization + a curated alias
dictionary hit 100% and the LLM added nothing. Food resolution is the opposite
regime:

- **Open, large vocabulary** — thousands of foods, no clean alias set.
- **Cross-language** — Hungarian/German queries against English descriptions;
  string similarity is near-useless (`Uborka`↔`Cucumber` share no characters).
- **Many near-duplicates** — dozens of USDA rows per food ("apple, raw",
  "apples, raw, with skin", …), so "correct" is a *concept*, not one row.

This is textbook **cross-lingual entity linking**, so the method is grounded in
that literature rather than in hand-written rules.

## Grounding

- **Retrieve-then-rerank** — a bi-encoder shortlists candidates, a cross-encoder
  reranks. The standard entity-linking pipeline (BLINK, Wu et al. 2020, *Zero-shot
  Entity Linking with Dense Entity Retrieval*).
- **Attribute-aware matching** — both datasets carry per-100g macros; serializing
  structured attributes into the record is the principled way to use them (Ditto,
  Li et al. 2020, *Deep Entity Matching with Pre-trained LMs*). We test it as a
  rerank signal.
- **Query transformation** — translation is one transform; instruction-formatted
  queries are another (Qwen3-Embedding, 2025), and HyDE (Gao et al. 2022) a third.
- **Embedders** — modern multilingual bi-encoders: `bge-m3` (2024), the
  `Qwen3-Embedding` family (2025), `harrier-oss-v1-0.6b`.

## Benchmark setup

- **Corpus**: 13,692 USDA foods (SR-Legacy + Foundation + FNDDS survey),
  embedded once per model (cached).
- **Gold**: 63 hand-labeled foods drawn from the highest-frequency logged items,
  labeled at the **concept** level — a retrieval is correct if the matched USDA
  description contains an accepted keyword (e.g. `carrot`), robust to USDA's many
  near-duplicates while still failing the real errors (carrot→papaya,
  cucumber→borage, orange-juice→nance).
- **Axes**: embedder × query transform (raw name vs English translation) × rerank
  (none / macro fingerprint / cross-encoder `bge-reranker-v2-m3`).
- **Metrics**: recall@{1,5,10}, MRR@10. Instruction-tuned models get their task
  instruction on the query side only; the corpus is embedded plain (asymmetric
  bi-encoder retrieval). All on Apple MPS.

## Results

| embedder | size | query | R@1 | R@5 | R@10 | MRR | embed (13.7k) |
|---|---|---|---|---|---|---|---|
| bge-m3 | 0.6B | raw | 0.33 | 0.41 | 0.46 | 0.37 | 12s |
| bge-m3 | 0.6B | **translated** | **1.00** | 1.00 | 1.00 | 1.00 | 12s |
| qwen3-0.6b | 0.6B | raw | 0.24 | 0.30 | 0.35 | 0.27 | 38s |
| qwen3-0.6b | 0.6B | translated | 0.98 | 1.00 | 1.00 | 0.99 | 38s |
| qwen3-4b | 4B | raw | **0.43** | 0.48 | 0.56 | 0.46 | 253s |
| qwen3-4b | 4B | translated | 0.95 | 0.98 | 0.98 | 0.97 | 253s |
| harrier-0.6b | 0.6B | raw | 0.25 | 0.38 | 0.44 | 0.31 | 45s |
| harrier-0.6b | 0.6B | **translated** | **1.00** | 1.00 | 1.00 | 1.00 | 45s |

**Rerank ablation** (best embedder, translated query, over top-20):

| step | R@1 |
|---|---|
| bi-encoder only | **1.00** |
| + macro-fingerprint rerank | 0.98 |
| + cross-encoder rerank (bge-reranker-v2-m3) | 0.98 |

## Findings

1. **The query transform dominates the embedder.** Raw cross-lingual R@1 is
   0.24–0.43 for *every* model; translation lifts all of them to 0.95–1.00. The
   language gap, not embedding quality, is the bottleneck — closing it with a
   ~$0.02 translation pass beats any model swap.
2. **Bigger is not better here.** qwen3-4b leads on the *raw* path (0.43 — model
   scale does help bridge languages) but is *worst* translated (0.95) and ~20×
   slower to embed; the smallest/oldest models (bge-m3, harrier-0.6b) reach 1.00
   translated. Once the language gap is closed the task is easy and model size is
   irrelevant. This is a direct counterpoint to picking by MTEB rank.
3. **Reranking doesn't help — it slightly hurts.** With translated retrieval
   already saturated, macro and cross-encoder rerank can only demote a correct
   top-1 (macro pulls toward a wrong food with closer macros). The standard
   retrieve-then-rerank second stage adds nothing *on this regime*; it would
   likely help on a harder, more ambiguous tail (see limitations).

**Conclusion / production choice:** translate → **bge-m3** (cheapest, fastest,
tied-best translated). The reconciler uses local embeddings (Ollama `bge-m3`) plus
a single batched translation call; no reranker, no large model. So the food
pipeline lands opposite to biomarkers: there a **rule-based** system won and the
LLM was unnecessary; here a **learned + LLM-translation** system is essential and
rules are useless — the two write-ups together show matching the method to the
resolution regime, not defaulting to one tool.

## Limitations

- The 63-food gold is weighted to common foods and **saturates at ~1.0** on the
  translated path, so it cannot separate embedders *after* translation — only the
  (non-saturated) raw path discriminates them. A harder gold set (rare, composite,
  or brand-specific foods) is needed to test whether a stronger embedder or a
  cross-encoder rerank pays off on the tail; that is the natural next iteration.
- Translation quality is itself a dependency (a mistranslation propagates). It was
  spot-checked and is high, but not measured here.
- HyDE and hybrid dense+sparse retrieval (bge-m3 supports it natively) are
  grounded options left untested.

## Reproduce

```bash
uv run python scripts/build_foods.py                    # USDA foods + ingredients
uv run python scripts/reconcile_foods.py                # Yazio → USDA (translate + embed)
uv run python scripts/benchmark_food_embedders.py --rerank   # this benchmark
```
