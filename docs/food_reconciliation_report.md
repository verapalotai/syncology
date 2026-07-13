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
- **Two gold sets**, both concept-level (a retrieval is correct if the matched USDA
  description's word-stems cover an accepted keyword — stem- and order-robust, so
  "goat cheese"↔"Cheese, goat" and "cherry"↔"Cherries, raw" match, while the real
  errors still fail):
  - **Common** (N=63) — highest-frequency foods; a floor/sanity set.
  - **Hard** (N=204) — deeper into the logged tail, weighted to compounds
    (`cherry tomato`), prepared forms, and cross-lingual false friends; the
    discriminative set.
- **Axes**: embedder × query transform (raw name vs English translation) × rerank
  (none / macro fingerprint / cross-encoder `bge-reranker-v2-m3`).
- **Metrics**: this is concept-level *known-item* retrieval — one correct food,
  realized as a *set* of acceptable USDA rows — so the task-appropriate metrics are
  **Success@{1,5,10}** (a correct row in the top k) and **MRR**, with **nDCG@10** as
  a graded supplement. We deliberately omit MAP / R-precision: those reward ranking
  the *whole* relevant set (every "apple" row) high, which is not the goal.
- **Rigor**: every metric carries a **bootstrap 95% CI** (10k resamples over
  queries); system pairs are compared with **McNemar's exact test** on paired
  Success@1; and the hard set is **stratified by difficulty** (simple / compound /
  prepared / branded / regional-OOV) to locate the error tail. Code:
  `resolve/food_eval.py`, `scripts/eval_food_rigor.py`.
- Instruction-tuned models get their task instruction on the query side only; the
  corpus is embedded plain (asymmetric bi-encoder retrieval). All on Apple MPS.

## Results (hard set, N=204)

| embedder | size | query | R@1 | R@5 | R@10 | MRR | embed (13.7k) |
|---|---|---|---|---|---|---|---|
| bge-m3 | 0.6B | raw | 0.30 | 0.40 | 0.45 | 0.35 | 12s |
| bge-m3 | 0.6B | **translated** | **0.88** | 0.93 | 0.94 | 0.90 | 12s |
| qwen3-0.6b | 0.6B | raw | 0.25 | 0.31 | 0.33 | 0.28 | 38s |
| qwen3-0.6b | 0.6B | translated | 0.85 | 0.93 | 0.94 | 0.88 | 38s |
| qwen3-4b | 4B | raw | **0.39** | 0.49 | 0.54 | 0.43 | 253s |
| qwen3-4b | 4B | translated | 0.83 | 0.89 | 0.92 | 0.86 | 253s |
| harrier-0.6b | 0.6B | raw | 0.28 | 0.36 | 0.42 | 0.32 | 45s |
| harrier-0.6b | 0.6B | translated | 0.85 | 0.93 | 0.93 | 0.88 | 45s |

The common set (N=63) **saturates** — every model reaches R@1 0.98–1.00 translated,
so it cannot rank embedders; only the hard set discriminates.

**Rerank ablation** (best embedder, translated query, over top-20):

| step | common R@1 | hard R@1 |
|---|---|---|
| bi-encoder only | 1.00 | **0.88** |
| + macro-fingerprint rerank | 0.98 | 0.87 |
| + cross-encoder rerank (bge-reranker-v2-m3) | 0.98 | 0.83 |

## Statistical rigor — CIs, strata, significance

Aggregate point estimates hide whether a gap is real. Three additions make the
hard-set (N=204) comparison defensible.

**Success@1 with bootstrap 95% CIs** (translated query, 10k resamples):

| embedder | size | Success@1 [95% CI] | MRR | nDCG@10 |
|---|---|---|---|---|
| bge-m3 | 0.6B | 0.877 [0.828, 0.922] | 0.897 | 0.769 |
| qwen3-0.6b | 0.6B | 0.853 [0.804, 0.902] | 0.880 | 0.787 |
| qwen3-4b | 4B | 0.828 [0.775, 0.877] | 0.858 | 0.782 |
| harrier-0.6b | 0.6B | 0.853 [0.804, 0.902] | 0.876 | 0.821 |

**All four CIs overlap heavily.** The apparent "bge-m3 beats the 4B" ranking is
inside the noise — see the significance test below.

**Per-stratum Success@1** (bge-m3, translated) — *where the last 20% actually is*:

| stratum | N | Success@1 [95% CI] |
|---|---|---|
| prepared (`boiled beetroot`) | 54 | 0.944 [0.870, 1.000] |
| compound (`red apple`) | 21 | 0.905 [0.762, 1.000] |
| simple (`banana`) | 114 | 0.877 [0.816, 0.930] |
| branded (`Oatly Barista`) | 12 | 0.750 [0.500, 1.000] |
| regional / OOV (`lecsó`, `ajvar`) | 3 | **0.000** [0.000, 0.000] |

This corrects the intuition. Translation *solves* the cases you'd fear —
modifier-compounds and prepared forms score highest, not lowest. The residual error
is concentrated in **branded** products (75%, wide CI) and, categorically,
**regional/OOV** foods absent from USDA (0/3 — no amount of embedding or reranking
finds a row that isn't there). That is a *coverage* problem, and it names the next
move precisely: a second corpus (Open Food Facts), not a better model.

**McNemar exact tests** (paired Success@1, hard set):

| comparison | helped A | helped B | p | verdict |
|---|---|---|---|---|
| translation **vs** raw name (bge-m3) | 120 | 2 | 2.8e-33 | *** decisive |
| bge-m3 **vs** qwen3-4b (translated) | 17 | 7 | 0.064 | **ns** |
| bi-encoder **vs** +cross-encoder rerank | 13 | 4 | 0.049 | * (rerank *hurts*) |

The translation effect is overwhelming (120 queries fixed, 2 broken). The
embedder choice is **not statistically significant** — the 20× costlier 4B is
indistinguishable from the 0.6B. And the cross-encoder rerank's harm *is*
significant (p ≈ 0.05): it breaks 13 queries and fixes 4.

## A note on rigor — investigating an apparent anomaly

The common set first suggested qwen3-4b was *worst* translated (0.95 vs 1.00). Two
checks before trusting it: (1) **config** — Qwen3-Embedding is last-token pooled and
needs left padding; we verified batched vs single-item embeddings are identical
(cos = 1.000), so pooling/padding is handled correctly, not a bug. (2) **the misses
themselves** were three genuine but idiosyncratic errors (`cherry tomatoes`→
"Cherries", `white miso`→"Rice flour", `kombucha`→"Kefir"), i.e. real *modifier-
distraction* — but three foods on a saturated set is noise. On the discriminative
hard set the gap disappears: all four embedders sit at **0.83–0.88**. Lesson: a
saturated benchmark manufactured a false ranking; the harder set corrected it.

## Findings

1. **The query transform dominates the embedder.** Raw cross-lingual R@1 is
   0.25–0.39; translation lifts every model to 0.83–0.88 — a ~2.5× gain from a
   ~$0.02 translation pass that no model swap comes close to.
2. **After translation, scale buys nothing measurable.** The four embedders are
   *statistically indistinguishable* translated — every 95% CI overlaps and bge-m3
   (0.6B) vs qwen3-4b (4B) is McNemar p = 0.064 (ns). The 20× costlier 4B does not
   significantly beat the 0.6B, so the right call is to pick on **cost and latency**,
   not MTEB rank — which points at the cheapest, fastest model. (The earlier
   "cheapest *wins*" framing was an overstatement the CIs correct: it isn't better,
   it's *as good*, which is enough to choose it.)
3. **Scale helps only on the raw path.** qwen3-4b leads raw retrieval (0.39 vs
   ~0.27) — if you cannot translate, a bigger model recovers some cross-lingual
   recall. This is the one place model size pays off, and it's the path we don't use.
4. **Reranking does not help — it significantly hurts.** Macro rerank is flat
   (0.88→0.87); cross-encoder rerank *drops* to 0.83, and the harm is significant
   (McNemar p ≈ 0.05: 13 queries broken, 4 fixed). Food names are short, so the
   bi-encoder already captures the semantics and the standard retrieve-then-rerank
   second stage has nothing to add — it only adds noise. Robust across both gold sets.

**Conclusion / production choice:** translate → **bge-m3** (cheapest, fastest,
best-or-tied translated), no reranker, no large model. The food pipeline lands
opposite to biomarkers: there a **rule-based** resolver won and the LLM was
unnecessary; here **learned embeddings + LLM translation** are essential and rules
are useless. The two write-ups together argue for matching the method to the
resolution regime rather than defaulting to one tool.

## The error tail (translated bge-m3, ~12% miss)

The per-stratum table locates it precisely. Two kinds remain, and they are *not*
the compound/prepared cases translation already handles well:

- **Out-of-vocabulary / coverage (the categorical failure).** Regional foods absent
  from USDA — `lecsó`, `ajvar` — score 0/3. This is not a ranking problem; the
  correct row does not exist in the corpus, so no embedder or reranker can recover
  it. Only a second source (Open Food Facts) closes it.
- **Branded products (the soft spot).** 0.75 with a wide CI — brand tokens
  (`Oatly Barista`) dilute the generic-food signal. This is where attribute-grounded
  matching (Ditto-style, macros serialized into the record) is most likely to help.

Residual **modifier distraction** (`green apple`→"Green peas") still occurs but is
now a minority of the simple/compound tail, not its dominant cause.

## Second corpus — Open Food Facts (testing the coverage thesis)

The stratum table makes a falsifiable claim: the tail is a **coverage** gap, not a
modelling one — so a second corpus should close it and a bigger model should not. We
test it directly. **Open Food Facts** (crowd-sourced, multilingual product database)
contributes **360,892** products sold in Hungary / Germany / Austria — the countries
the diet is logged in, so the corpus is chosen by *provenance, not by peeking at the
gold*. Names are kept in their original language (an OFF product named "Lecsó" is
exactly what the raw Hungarian query should find). Same embedder (bge-m3), same gold;
we retrieve against USDA alone vs USDA + OFF.

**Per-stratum Success@1, translated query** (bge-m3):

| stratum | N | USDA | USDA + OFF |
|---|---|---|---|
| regional / OOV | 3 | **0.000** | **0.667** ↑ |
| branded | 12 | 0.750 | 0.833 ↑ |
| prepared | 54 | 0.944 | 0.944 → |
| simple | 114 | 0.877 | 0.842 ↓ |
| compound | 21 | 0.905 | 0.810 ↓ |
| **overall** | 204 | 0.877 | 0.863 |

**The coverage thesis holds — regional recovers from zero, on real OFF rows:**

```
✓ spicy ajvar  → [OFF] 'Ajvar ljuti'     (absent from USDA)
✓ lecsó        → [OFF] 'Lecsó'           (absent from USDA)
✗ edamame …    → [USDA] 'Edamame, cooked' (a mislabelled generic, not OOV)
```

Both genuinely out-of-vocabulary foods are found — and the winning row is literally
an OFF product, not a USDA near-miss. Branded improves too (0.75→0.83). This is the
result the stratification predicted: **coverage, supplied by a second corpus.**

**But naive concatenation is the wrong integration.** Merging 360k branded/regional
products *dilutes the clean head*: simple and compound dip, and overall the change is
**not significant** (McNemar: +OFF fixes 12, breaks 15, p = 0.70). On the *raw* query
path it is significantly *worse* (0.299→0.230, p = 0.024) — untranslated queries flood
into the much larger, noisier OFF name space. OFF ends up supplying the *majority* of
even the simple-stratum hits (77 of 96): mostly valid, exact-named matches
(`onion`→"Onions", `paprika`→"paprika", often cleaner than USDA's inverted
"Onions, raw"), but with enough keyword-loose noise (`oat muffin`→"oreo muffin") to
cost a few points of head precision.

**Conclusion.** The tail is coverage-bound and OFF closes it — a bigger model never
could. But you cannot just merge indices: that trades head accuracy for tail recovery
(a wash at best). The right architecture is a **cascade** — resolve against the clean
USDA generics first, fall back to OFF only when USDA confidence is low or the query is
OOV.

## Confidence-routed cascade

Naive concatenation is exactly a **max-confidence router**: argmax over the union
picks whichever corpus has the higher top-1 cosine, so OFF's spuriously-high exact
matches (`apple`→OFF "apple") outscore the correct USDA generic. That is *why* it
regresses the head. A **threshold cascade** instead routes on USDA confidence
*alone* — trust USDA's top-1 when its cosine ≥ τ, fall back to OFF only when USDA is
unconfident — so a high-scoring OFF row can never override a confident USDA hit.

The confident head and the OOV tail turn out to be cleanly **cosine-separable** (USDA
top-1 cosine: median 0.81, but the OOV foods sit near the 0.51 floor). Reporting the
whole accuracy-vs-τ frontier rather than one tuned point (translated query, bge-m3):

| τ | % → OFF | overall | head | regional | simple |
|---|---|---|---|---|---|
| ≤ 0.55 (USDA-only) | 0% | 0.877 | 0.899 | 0.000 | 0.877 |
| 0.65 | 5% | **0.897** | 0.910 | 0.667 | 0.895 |
| 0.70 | 10% | **0.897** | 0.910 | 0.667 | 0.886 |
| 0.75 | 25% | 0.892 | 0.905 | 0.667 | 0.877 |
| 0.85 | 71% | 0.863 | 0.862 | 0.667 | 0.833 |

A **broad window (τ ≈ 0.60–0.75)** clears 0.89 — above both USDA-only (0.877) and the
max-confidence merge (0.863) — so this is not a knife-edge tuned value. At a
label-free operating point (**τ = P10 of the USDA cosine** — route only the
least-confident decile, no gold used to pick it):

| system | Success@1 [95% CI] |
|---|---|
| USDA-only | 0.877 [0.828, 0.922] |
| max-confidence merge | 0.863 [0.814, 0.907] |
| **cascade @ τ=0.70** | **0.897 [0.853, 0.936]** |

Per-stratum, USDA-only → cascade, **nothing regresses**:

```
regional  0.000 → 0.667    (recovered from OFF)
compound  0.905 → 0.952
simple    0.877 → 0.886
prepared  0.944 → 0.944
branded   0.750 → 0.750
```

The cascade recovers the OOV tail **at zero head cost** — it even nudges the head up,
by letting OFF correct a few low-confidence USDA misses. It cleanly reverses the
merge's head regression (McNemar cascade-vs-merge +13 / −6). The overall +2 pp over
USDA-only is not individually significant at N=204 (regional is only 3 foods, so its
recovery moves the aggregate little) — the result is the *per-stratum dominance* and
the structural fix: **confidence routing buys coverage without paying head accuracy,
which merging could not.** Production choice: translate → USDA-first bi-encoder →
OFF fallback below a confidence threshold.

## Retrieval-systems matrix

Everything so far uses one retrieval method — dense single-vector cosine. Does a more
sophisticated retriever help? Holding the corpus (USDA) and embedder (bge-m3) fixed,
we vary only the method: **dense**, **sparse BM25**, **hybrid** (dense + BM25 fused by
reciprocal-rank fusion), and **bge-m3 late-interaction (ColBERT MaxSim)**.

**Translated query** (the production path):

| system | Success@1 [95% CI] | S@5 | MRR | nDCG@10 | vs dense (McNemar) |
|---|---|---|---|---|---|
| dense | 0.877 [0.828, 0.922] | 0.926 | 0.897 | 0.769 | — |
| sparse (BM25) | 0.848 [0.799, 0.897] | 0.912 | 0.876 | 0.841 | +12/−18, p = 0.36 |
| hybrid (RRF) | 0.882 [0.833, 0.926] | 0.936 | 0.905 | 0.810 | +8/−7, p = 1.0 |
| ColBERT | 0.877 [0.833, 0.922] | 0.922 | 0.893 | 0.838 | +3/−3, p = 1.0 |

**All four are statistically indistinguishable on Success@1** — every CI overlaps and
every McNemar test against dense is non-significant. The much-cited "hybrid always
helps" gives +0.5 pp here (p = 1.0); ColBERT's extra multi-vector cost buys nothing on
top-1. The one real difference is **nDCG@10**: BM25 and ColBERT rank *more* correct
variants into the top-10 (0.84 vs dense's 0.77) — a graded-ranking gain from lexical /
token-level matching that never reaches rank 1, where a correct row already sits.

**Raw query** (no translation) — here method choice *does* matter, and negatively:

```
dense    0.299     sparse   0.162  (McNemar p = 8e-7, worse)
colbert  0.314     hybrid   0.216  (McNemar p = 0.002, worse)
```

BM25 has essentially no cross-lingual signal (`Uborka` and `Cucumber` share no
tokens), so it collapses and *drags hybrid down with it*. Only ColBERT ties dense —
token-level matching is marginally more robust to the language gap, but not
significantly. Lexical retrieval is not an alternative to translation; it *depends*
on it.

**Synthesis — three flat axes, two real levers.** Across every sophistication knob
tested — embedder scale (§findings 2), reranking (§findings 4), and now the retrieval
algorithm — the differences are inside the noise. What actually moves Success@1 is
**query transformation** (translation: 0.30 → 0.88, ~2.5×) and **corpus coverage**
(OFF, via the cascade, recovering the OOV tail). For short-text cross-lingual entity
linking the engineering lesson is contrarian and consistent: spend on *translating the
query* and *covering the vocabulary*, not on a bigger model, a reranker, or a fancier
index.

## Limitations

- Gold labels are concept-level and semi-automatically seeded; a handful of
  residual label errors add a small shared noise floor (affecting all models
  equally, so relative ranking holds).
- Translation quality is itself a dependency (a mistranslation propagates);
  spot-checked as high but not measured.
- Difficulty strata are assigned by a name heuristic (`resolve/food_labels.classify`),
  not adjudicated by hand; the `regional` and `branded` strata are small (N=3, N=12),
  so their per-stratum CIs are wide — enough to flag *where* the tail is, not to
  measure it precisely. Enlarging those two strata is the cheapest next data step.
- The retrieval matrix holds the corpus fixed at USDA (ColBERT's per-token index does
  not scale to the 360k OFF corpus without an ANN backend); the algorithm comparison
  is therefore clean but does not re-test the fancier retrievers *on* the OFF tail.
- HyDE and attribute-serialized (Ditto-style) reranking remain untested; the stratum
  table points at Ditto-style attribute matching as the most promising remaining move
  for the `branded` soft spot.

## Reproduce

```bash
uv run python scripts/build_foods.py                    # USDA foods + ingredients
uv run python scripts/reconcile_foods.py                # Yazio → USDA (translate + embed)
uv run python scripts/benchmark_food_embedders.py --rerank   # headline benchmark
uv run python scripts/build_openfoodfacts.py            # OFF second corpus (needs the CSV)
# rigor pass — bootstrap CIs, per-stratum, McNemar (hard set):
SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
  uv run python scripts/eval_food_rigor.py
# corpus ablation — USDA vs USDA+OFF, per stratum:
SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
  uv run python scripts/eval_food_corpus.py
# confidence-routed cascade — accuracy-vs-τ frontier:
SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
  uv run python scripts/eval_food_cascade.py
# retrieval-systems matrix — dense/sparse/hybrid/ColBERT (needs the `retrieval` extra):
SYNCOLOGY_FOOD_GOLD=data/raw/personal/nutrition/food_gold_hard.json \
  uv run python scripts/eval_retrieval_matrix.py
```

The OFF CSV export (public, ~1.3 GB gz) is fetched once from
`static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz` into gitignored
`data/raw/public/openfoodfacts/`.
