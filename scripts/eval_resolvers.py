"""Compare rule-based vs LLM biomarker resolution: accuracy, precision, recall, cost.

Scores both resolvers against the hand-verified gold labels, then runs a small
novel-name ablation (names absent from the alias dictionary) to show the
generalization tradeoff. Prints metrics only — no health values.

Usage:
    uv run python scripts/eval_resolvers.py [--db DB_PATH] [--no-llm]
"""

from __future__ import annotations

import argparse

from syncology import db
from syncology.resolve import llm
from syncology.resolve.biomarkers import RuleResolver
from syncology.resolve.labels import GOLD

# Haiku 4.5 pricing, USD per million tokens (input / output).
_PRICE_IN, _PRICE_OUT = 1.0, 5.0


def _score(pred: dict[str, str | None], gold: dict[str, str]) -> dict:
    total = len(gold)
    correct = sum(1 for name, key in gold.items() if pred.get(name) == key)
    predicted = sum(1 for name in gold if pred.get(name) is not None)
    wrong = sum(
        1 for name, key in gold.items()
        if pred.get(name) is not None and pred.get(name) != key
    )
    abstain = total - predicted
    return {
        "accuracy": correct / total,
        "precision": correct / predicted if predicted else 0.0,
        "recall": correct / total,
        "abstain": abstain,
        "wrong": wrong,
        "correct": correct,
        "total": total,
    }


def _fmt(name: str, m: dict, cost: str) -> str:
    return (f"  {name:<16} acc={m['accuracy']:6.1%}  prec={m['precision']:6.1%}  "
            f"rec={m['recall']:6.1%}  wrong={m['wrong']:<3} abstain={m['abstain']:<3} {cost}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH)
    ap.add_argument("--no-llm", action="store_true", help="skip the API resolver")
    args = ap.parse_args()

    con = db.connect(args.db)
    units = dict(con.execute(
        "SELECT test_name, any_value(unit) FROM lab_results GROUP BY test_name"
    ).fetchall())
    con.close()

    names = list(GOLD)
    pairs = [(n, units.get(n)) for n in names]
    rule = RuleResolver()
    rule_pred = {n: rule.resolve(n, u).key for n, u in pairs}

    print("=" * 74)
    print(f"BIOMARKER RESOLUTION — rule-based vs LLM  ({len(GOLD)} gold labels)")
    print("=" * 74)
    print(_fmt("rule-based", _score(rule_pred, GOLD), "cost=$0 (local)"))

    llm_pred = None
    if not args.no_llm:
        llm_pred, ti, to = llm.resolve_batch(pairs)
        cost = ti / 1e6 * _PRICE_IN + to / 1e6 * _PRICE_OUT
        print(_fmt(f"llm ({llm.config.BULK_MODEL.split('-')[1]})", _score(llm_pred, GOLD),
                   f"cost=${cost:.4f} ({ti:,}in/{to:,}out tok)"))

        # Where the two resolvers disagree (illustrative, names only).
        disagree = [(n, rule_pred[n], llm_pred.get(n), GOLD[n])
                    for n in names if rule_pred[n] != llm_pred.get(n)]
        print(f"\ndisagreements (rule vs llm): {len(disagree)}")
        for n, rk, lk, gk in disagree[:12]:
            print(f"  {n[:40]:<40} rule={rk}  llm={lk}  gold={gk}")

    # Novel-name ablation: names not in the alias dictionary (English, reordered,
    # a genuinely new analyte). Shows dictionary brittleness vs LLM generalization.
    print("\n" + "-" * 74)
    print("NOVEL-NAME ABLATION (names absent from the alias dictionary)")
    print("-" * 74)
    novel = [
        ("Testosterone, total", "nmol/L", "testosterone_total"),
        ("Szérum kreatinin", "umol/L", "creatinine"),      # reordered HU
        ("Serum cholesterol", "mmol/L", "cholesterol_total"),
        ("HbA1c", "%", None),                               # not in registry → abstain
    ]
    novel_pairs = [(n, u) for n, u, _ in novel]
    novel_rule = {n: rule.resolve(n, u).key for n, u in novel_pairs}
    novel_llm = llm.resolve_batch(novel_pairs)[0] if not args.no_llm else {}
    print(f"  {'name':<24}{'expected':<20}{'rule':<20}{'llm':<20}")
    for n, u, exp in novel:
        print(f"  {n:<24}{str(exp):<20}{str(novel_rule[n]):<20}{str(novel_llm.get(n)):<20}")
    print("=" * 74)


if __name__ == "__main__":
    main()
