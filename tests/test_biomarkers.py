"""Tests for the rule-based biomarker resolver."""

from __future__ import annotations

from syncology.resolve.biomarkers import BY_KEY, REGISTRY, RuleResolver, normalize


def test_registry_keys_unique():
    keys = [b.key for b in REGISTRY]
    assert len(keys) == len(set(keys))


def test_normalize_strips_accents_case_and_a_suffix():
    assert normalize("Koleszterin (A)") == "koleszterin"
    assert normalize("Húgysav") == "hugysav"
    assert normalize("Limfocita # (A)") == "limfocita #"  # # kept as a token


def test_exact_and_a_suffix_collapse():
    r = RuleResolver()
    assert r.resolve("Koleszterin").key == "cholesterol_total"
    assert r.resolve("Koleszterin (A)").key == "cholesterol_total"  # (A) variant → same key


def test_parenthetical_abbreviation():
    r = RuleResolver()
    # full Hungarian name + abbreviation both resolve to the same biomarker
    assert r.resolve("FSH").key == "fsh"
    assert r.resolve("Follikulus stimuláló hormon (FSH) (A)").key == "fsh"


def test_hungarian_to_english_synonyms():
    r = RuleResolver()
    assert r.resolve("GOT (ASAT)").key == "ast"
    assert r.resolve("GPT (ALAT)").key == "alt"
    assert r.resolve("Karbamid").key == "urea"
    assert r.resolve("Vérsejtsüllyedés").key == "esr"


def test_triglyceride_naming_variants_collapse():
    r = RuleResolver()
    assert r.resolve("Triglicerid").key == "triglycerides"
    assert r.resolve("Trigliceridek (A)").key == "triglycerides"


def test_specimen_disambiguation_by_unit():
    r = RuleResolver()
    # same-ish name, different specimen — the unit decides
    assert r.resolve("Fehérvérsejtszám (A)", "Giga/L").key == "wbc"       # blood
    assert r.resolve("Fehérvérsejt", "/ltr(H)").key == "urine_wbc"        # urine
    assert r.resolve("Vörösvérsejtszám", "Tera/L").key == "rbc"
    assert r.resolve("Vörösvérsejt", "/ltr(H)").key == "urine_rbc"


def test_base_name_strips_embedded_unit_parenthetical():
    r = RuleResolver()
    assert r.resolve("eGFR-EPI (mL/min/1.73m2)").key == "egfr"


def test_unknown_name_is_unresolved():
    r = RuleResolver()
    res = r.resolve("Egészen ismeretlen vizsgálat")
    assert res.key is None
    assert res.method == "none"


def test_all_registry_categories_present_for_known_keys():
    # sanity: a few keys map to the expected category
    assert BY_KEY["tsh"].category == "thyroid"
    assert BY_KEY["ldl"].category == "lipid"
    assert BY_KEY["urine_ph"].category == "urinalysis"
