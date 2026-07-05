# Marts & Cycle-Phase Analysis — Build & Design Report

A narrative account of the transform layer built on top of the Apple Health
warehouse: category-value normalization, the day-grain marts, and — the
centrepiece — sympto-thermal (STM) cycle-phase inference. It documents *what* was
built, *why* each decision was made, how the cycle model **evolved** as better
information arrived, and how it was verified.

Companion to `apple_health_ingestion_report.md` (the parser). As there, methods
and aggregate counts are public; individual health values are not — so the
row-level previews below use **synthetic** examples, never real measurements.

---

## 1. Where this sits

The parser lands raw measurements in `measurements`. This layer turns them into
queryable, domain-meaningful marts:

```
measurements ──▶ measurements_categorized ──▶ daily_activity
   (parser)         (§2 normalization)    ├─▶ daily_nutrition
                                          └─▶ cycle_days ──▶ cycle_phases   (§4)
                                                        └──▶ cycle_summary  (§4)
```

Everything here is a rebuildable, idempotent transform: views over
`measurements`, plus two materialized tables (`cycle_phases`, `cycle_summary`)
derived by a Python inference pass. Module: `src/syncology/transform/`.

---

## 2. Category-value normalization — first taste of entity resolution

HealthKit stores category records as raw enum strings, e.g.
`HKCategoryValueVaginalBleedingMedium` or
`HKCategoryValueCervicalMucusQualityEggWhite`. These are the cycle domain's
signal, but unusable as-is.

`transform/category_values.py` maps each raw enum to a tidy `label` and, where the
category is meaningfully ordered, an integer `ordinal`:

| metric | example raw value | label | ordinal |
|---|---|---|---|
| MenstrualFlow | `…VaginalBleedingMedium` | `medium` | 2 |
| CervicalMucusQuality | `…EggWhite` | `egg_white` | 5 (rising fertility) |
| OvulationTestResult | `…LuteinizingHormoneSurge` | `lh_surge` | 2 |
| IntermenstrualBleeding | `HKCategoryValueNotApplicable` | `present` | — |

**Design decisions:**
- **Explicit, auditable mapping, not buried branches.** The map is materialized as
  a small `category_value_map` table and exposed via the
  `measurements_categorized` view. Entity resolution should be inspectable — you
  can `SELECT * FROM category_value_map` and see every decision.
- **Ordinals only where ordered.** Flow intensity and mucus fertility are ordinal;
  nominal or presence-only values (`present`, `seven_day_limit`) get `NULL`.
- **Future-proofed.** Values not yet in the export (e.g. `watery` mucus, an Apple
  Watch's new enums) are pre-mapped; anything genuinely unseen is surfaced by
  `unmapped_values()` rather than silently dropped.
- **Presence sentinel handled.** `HKCategoryValueNotApplicable` is HealthKit's
  "the event happened" marker (IntermenstrualBleeding) → mapped to `present`.

Verified: all 197 category rows in the export map, **0 unmapped**.

---

## 3. Daily marts

Three day-grain views, all bucketing on **local** calendar date
(`start_ts AT TIME ZONE 'Europe/Budapest'`) so a 23:00 reading lands on the right
day rather than rolling into the next UTC day.

- **`daily_activity`** (1,643 days) — steps, distance, active/basal energy,
  flights, FULL-OUTER joined to `activity_summary` for the Activity rings.
- **`daily_nutrition`** (262 days) — macro totals from the `Dietary*` metrics
  plus `meals_logged` (`count(DISTINCT correlation_id)`, reusing the parser's
  meal-correlation work).
- **`cycle_days`** (601 days) — the raw per-day cycle signals (BBT, flow ordinal,
  mucus ordinal, intermenstrual bleeding, LH surge) that feed inference.

A real-data honesty note surfaced here: without an Apple Watch the Activity
`exercise`/`stand` rings are empty, so **step count is the activity proxy** until
the watch fills those rings through the same pipeline.

---

## 4. Cycle-phase inference (STM) — the centrepiece

### 4.1 The biology, briefly

Two hormones drive the observable cycle. Oestrogen (rising follicle) thins and
wets cervical mucus and does *not* affect basal body temperature (BBT).
Progesterone (from the corpus luteum, after ovulation) **raises BBT** and thickens
mucus. So the cycle shows two temperature plateaus — a low follicular phase and a
raised luteal phase — with ovulation at the transition. The sympto-thermal method
(STM) reads exactly these primary signs: **cervical mucus predicts** ovulation is
coming; **temperature confirms** it happened.

### 4.2 Temperature — the only confirmer

`_detect_temp_shift` implements the STM coverline rule:

- **Coverline** = the highest of the **6 low** readings (no offset).
- Need **3 readings above** the coverline; the **3rd ≥ 0.2 °C** above it.
- **Exception 1** — if the 3rd isn't a full 0.2 above, a **4th reading above the
  line** confirms instead.
- **Exception 2 (conservative)** — if any reading in the run dips to/below the
  line, that run is void; scanning continues.

Ovulation is dated to the last low day before the rise. Temperature is the *only*
sign that confirms ovulation occurred — mucus and LH only predict.

### 4.3 Mucus — the predictor, and the cross-check

- **Change point** = the first day with *any* mucus after menses (before
  ovulation all mucus counts as fertile).
- **Peak day** = the last day of peak-type mucus (watery / egg-white); mucus
  "confirms" at **peak + 3 days**.
- **Cross-check** — confirmation day = the **later** of (temperature confirm) and
  (peak + 3), because you're only certain once *both* signs agree. A temperature
  shift is still required.

LH surge is treated as a fertile hint only, never a confirmer — deliberately,
because LH tests are unreliable under PMOS/PCOS (can read positive repeatedly).

### 4.4 Two outputs per day

- **Clinical `phase`** — `menstruation` / `follicular` / `ovulation` / `luteal` /
  `unknown`, for health analytics ("avg BBT by phase").
- **`fertility_zone`** — the STM zones: `infertile_pre` (menses → first mucus),
  `fertile` (first mucus → confirmation), `infertile_post` (after confirmation),
  `unknown`. `fertile_window` is the boolean shorthand.

### 4.5 The conservative core: explicit `unknown`

PMOS cycles are frequently long, irregular, or anovulatory. The model **never
forces** a phase: `follicular`/`luteal` are assigned only around a *confirmed*
ovulation; every ambiguous or anovulatory stretch stays `unknown`. Honesty over
false structure.

### 4.6 The physiological guard (false-early shift rejection)

The luteal phase runs to ~16 days at most. In long PMOS cycles an early
temperature blip can pass the 3-over-6 test and be mistaken for the real shift —
producing an impossible 20–35-day "luteal". The guard catches this: a detected
shift whose implied luteal (`next_menses − ovulation`) exceeds **16 days** is
treated as a false-early shift — **skipped**, with scanning continuing for a
plausible later shift.

- A valid later shift → ovulatory with a realistic luteal.
- None → `anovulatory`.
- Either way → `suspect_ovulation = True` (rejected, but recorded — never
  silently dropped).

This is skip-and-rescan, not plain reject, precisely so it recovers the *real*
signal instead of discarding the cycle.

### 4.7 Anomaly flagging vs population norms

`cycle_summary` z-scores each cycle's length and luteal length against population
norms (cycle μ=29.30, σ=3.89; luteal μ=13.27, σ=2.67; from Fehring et al. 2012,
cited — not copied). Anything beyond 2σ flags. Plus a `short_luteal` flag for
luteal ≤ 10 days — the low-progesterone signal. For PMOS this turns a wall of
"unknown" into something diagnostic: *which* cycles are out of norm, and how.

---

## 5. How the model evolved (the interesting part)

The cycle model was rebuilt twice as better information arrived — a good record of
letting ground truth override earlier guesses.

**v1 — conservative sympto-thermal, flat threshold.** First cut used a crude
"3 readings all ≥ (max of 6 lows) + 0.1 °C" rule. A threshold sweep (0.10–0.30 °C)
was run to pick the margin with eyes open; 0.10 was chosen. Result: 5/20 cycles
ovulatory. Plausible-looking, but the rule was not the real STM rule.

**Interlude — reviewing a peer implementation.** A cloned Oura-based tracker used
the *same* FAM thermal-shift idea, which validated the approach, and contributed
one genuinely useful idea we lacked: **population-norm anomaly flagging** (Fehring
norms). Adopted as `cycle_summary` z-scores; its other parts (notebook-coupled,
single-cycle, buggy return types) were not.

**v2 — the real STM rules as ground truth.** Authoritative STM material specified
the *precise* rule: coverline = highest of 6 lows with **no offset**, only the
**3rd** high needs +0.2, plus the 4th-temp and dip-voids exceptions, plus the
mucus peak-day cross-check and the three fertility zones. This is a different,
more correct rule than v1's flat threshold. Re-running the sweep under it showed
detection is **stable across 0.15–0.30 °C** (the proper rule is robust to the
margin), so the standard 0.2 needs no tuning — the earlier 0.10-vs-0.20 dilemma
dissolved. Detection rose to 15/20.

**v3 — the physiological guard.** v2 still produced a few impossible 20–35-day
luteals from false-early shifts. The 16-day luteal guard fixed this, and did more
than reject: for most affected cycles it found the *later* genuine shift,
revealing real short luteals.

**Net effect of the guard on real data:**

| | before guard | after guard |
|---|---|---|
| Ovulatory cycles | 15/20 | 14/20 |
| Luteal-phase days | 217 | **107** |
| Follicular days | 214 | 281 |
| Biphasic BBT gap | 0.19 °C | **0.32 °C** |

The impossible long "luteals" became their real short ones; removing false-early
shifts stopped polluting the luteal group with follicular-temperature days, so the
biphasic separation *sharpened*. Better clinical accuracy, not just cleaner tables.

---

## 6. Verification & real-data validation

The definition-of-done cross-domain query works (avg BBT by inferred phase), and
its output is the model's best validity check:

```
follicular  36.50 °C   ┐ low plateau
ovulation   36.54 °C   │
luteal      36.83 °C   ┘ raised plateau  → clean ~0.32 °C biphasic shift
```

That the inferred phases reproduce the textbook biphasic curve — without ever
being told the temperatures — is strong evidence the inference is tracking real
signal.

Aggregate results on the export (~1.8 yrs of cycle data, 20 cycles):
- **14/20 ovulatory (70%)**, 6 with a guard-rejected suspect shift, 8 flagged vs
  norms; a majority of ovulatory cycles show **short luteal phases (≤10 d)** — the
  low-progesterone picture consistent with PMOS.
- Phase mix: menstruation 60, follicular 281, ovulation 14, luteal 107,
  unknown 139 days. Zones: infertile_pre 350, fertile 212, infertile_post 39.

Tests: **26 passing** across the transform modules — including Exception 1
(4th-temp), Exception 2 (dip voids run), the mucus cross-check, fertility zones,
short-luteal, and both guard behaviours (reject-as-suspect; accept a plausible
later shift). All fixtures are synthetic. Ruff clean.

A telling test moment: adding the guard broke three older fixtures — because they
had placed ovulation *unphysiologically early* (a 28-day cycle ovulating on day 8
→ 20-day luteal). The guard correctly rejected them; the fixtures were wrong, and
were fixed to realistic timing. The guard caught bad test data before bad real
data.

---

## 7. Files built (annotated)

```
src/syncology/transform/
  category_values.py   # HealthKit enum -> label + ordinal; map table + view
  marts.py             # daily marts + STM cycle inference + cycle_summary
scripts/
  normalize_categories.py       # build + report category normalization
  build_marts.py                # build all marts, print phase/summary report
  sweep_ovulation_threshold.py  # BBT-margin sensitivity sweep
tests/
  test_category_values.py       # 5 tests
  test_marts.py                 # 21 tests (marts + STM rules + guard)
```

Key surfaces in `marts.py`:
- `analyze_cycles(rows, shift_c)` → `(per_day, cycles)` — the whole inference,
  pure over dict rows (easy to test).
- `_detect_temp_shift(series, shift_c, cycle_end, max_luteal)` → the coverline
  rule + exceptions + the luteal guard.
- `Cycle` dataclass — per-cycle markers (ovulation, temp-confirm, peak,
  confirmation, lengths, short/suspect flags).
- `write_cycle_phases` / `write_cycle_summary` — materialize the two tables.
- `POP_NORMS`, `FLAG_SIGMA`, `_MAX_LUTEAL_DAYS`, `DEFAULT_SHIFT_C` — the tunable
  clinical constants, all documented at the definition.

---

## 8. Database preview (synthetic)

`cycle_phases` — one row per signal day:

```
day         phase         fertility_zone   fertile_window  cycle_day
2025-01-01  menstruation  infertile_pre    false           1
2025-01-06  follicular    fertile          true            6
2025-01-14  ovulation     fertile          true            14
2025-01-20  luteal        infertile_post   false           20
```

`cycle_summary` — one row per cycle, with markers + flags (synthetic):

```
cycle  start       len  ovul_day    luteal  short  suspect  flags
  1    2025-01-01   28  2025-01-14   14     false  false    —
  2    2025-01-29   45  (rejected)   —      —      true     cycle_len z=+4.0
  3    2025-03-15   26  2025-03-27    9     true   false    luteal z=-1.6 SHORT
```

`category_value_map` (real — these are method mappings, not measurements):

```
CervicalMucusQuality  …EggWhite  egg_white  5
MenstrualFlow         …Medium    medium     2
```

---

## 9. Design decisions & trade-offs

| Decision | Alternative | Why |
|---|---|---|
| Explicit `category_value_map` table + view | Inline code branches | ER should be auditable/inspectable |
| Local-date day grain | UTC date | Late-evening readings land on the right day |
| Clinical phase **and** fertility zone | One or the other | Health analytics need phases; STM needs zones |
| Temperature is the only confirmer | Confirm on mucus/LH too | Only temperature proves ovulation occurred; LH unreliable in PMOS |
| Explicit `unknown` | Always-assign 4-phase | PMOS cycles are often anovulatory — don't fabricate |
| Guard: skip-and-rescan false-early shifts | Plain reject / keep | Recovers the *real* later shift instead of discarding the cycle |
| Flag `suspect_ovulation` | Silently drop | Never hide a rejected detection |
| 0.2 °C 3rd-high margin | Tuned per-sensor | Standard STM value; detection is margin-robust anyway |
| Fehring norms, cited | Copy peer repo's JSON | Clean provenance |
| Advanced early-infertile rules omitted | Implement −21/−8/5-3 | Contraceptive-precision, need 6–12 logged cycles; out of scope |

---

## 10. Lessons learnt

1. **Let ground truth override earlier guesses.** The flat-threshold v1 was
   reasonable but wrong; the real STM rule was both more correct and made the
   tuning question disappear. Rebuild when better information arrives.
2. **A borrowed idea beat borrowed code.** The peer repo's value was one concept
   (population-norm flagging), not its implementation.
3. **Validate against the phenomenon, not the exit code.** The biphasic BBT curve
   emerging from the inference is the real proof it works.
4. **A physiological guard is a data-quality check.** The 16-day luteal cap
   surfaced false-early shifts *and* caught unphysiological test fixtures.
5. **Flag, don't hide.** Suspect detections and out-of-norm cycles are recorded,
   not dropped — the anomalies are the clinically interesting part.
6. **Conservative + explicit `unknown` is the honest default** for a condition
   defined by irregularity.

---

## 11. How to run

```bash
uv run python scripts/normalize_categories.py       # category_value_map + view
uv run python scripts/build_marts.py                # daily marts + cycle tables + report
uv run python scripts/sweep_ovulation_threshold.py  # BBT-margin sensitivity
uv run pytest -q                                    # 26 tests
```

Cycle inference knobs live at the top of `transform/marts.py`
(`DEFAULT_SHIFT_C`, `_MAX_LUTEAL_DAYS`, `_SHORT_LUTEAL_DAYS`, `POP_NORMS`).

---

## 12. Open questions / next steps

- **Ovulation-timing uncertainty.** Temperature dates ovulation to ~2 days; the
  mucus peak (ovulation ≈ peak ± 3, ~70 % within ±1) could refine the estimate
  where both exist.
- **Cervix sign** (position/openness) is a third STM primary sign, not in the
  Apple Health export — would strengthen confirmation if ever logged.
- **Advanced early-infertile rules** (−21-day, Döring −8) are deferred; they'd
  tighten `infertile_pre` once ≥6–12 cycles are consistently logged.
- **A cervical-mucus quality mart** could track fertile-window quality over time
  (relevant to the nutrition × cycle overlap).
- The Apple Watch (~Aug 2026) adds `SleepAnalysis`/`HRV` through the same
  pipeline — no cycle-code changes needed, but sleep-adjusted BBT could reduce
  disturbed-night noise in the shift detection.
