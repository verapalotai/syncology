"""Biomarker entity resolution — canonical registry + rule-based resolver.

Lab reports name the same analyte many ways: a trailing ``(A)`` lab-section
marker doubles most entries, abbreviations hide in parentheses
(``Follikulus stimuláló hormon (FSH)``), and the source language is Hungarian.
This module resolves a raw ``test_name`` to a canonical biomarker key.

Two resolvers share the registry: this rule-based one (normalization + an
alias dictionary + fuzzy fallback) and the LLM one in ``llm.py``. The write-up
compares them on precision / recall / cost. Ground truth is a hand-labeled map
(``labels.py``); the registry's aliases are what a developer would reasonably
write, not a copy of that map, so rule-based recall is a real measurement.

**Specimen disambiguation:** a few names collide across specimens — ``Fehérvérsejt``
at ``/ltr(H)`` is *urine* WBC, while ``Fehérvérsejtszám`` at ``Giga/L`` is *blood*
WBC. Resolution therefore considers the unit, not just the name.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Biomarker:
    key: str  # canonical id, snake_case
    name_en: str
    category: str  # hematology | lipid | liver | kidney | electrolyte | hormone | thyroid | ...
    unit: str | None  # canonical unit
    aliases: tuple[str, ...] = ()  # HU / EN / abbreviation forms (matched after normalization)
    urine: bool = False  # specimen is urine (disambiguates blood-vs-urine name clashes)


# Canonical registry. Aliases are natural HU/EN forms; normalization handles
# case, accents, the "(A)" suffix, and punctuation, so they need not be
# pre-normalized. Abbreviations in parentheses are indexed separately too.
REGISTRY: tuple[Biomarker, ...] = (
    # --- Complete blood count ---
    Biomarker("wbc", "White blood cell count", "hematology", "Giga/L",
              ("feherversejtszam", "feherversejt szam", "white blood cell", "wbc")),
    Biomarker("rbc", "Red blood cell count", "hematology", "Tera/L",
              ("vorosversejtszam", "red blood cell", "rbc")),
    Biomarker("hemoglobin", "Hemoglobin", "hematology", "g/L", ("hemoglobin", "hgb")),
    Biomarker("hematocrit", "Hematocrit", "hematology", "L/L", ("hematokrit", "hct")),
    Biomarker("mcv", "Mean corpuscular volume", "hematology", "fL", ("mcv",)),
    Biomarker("mch", "Mean corpuscular hemoglobin", "hematology", "pg", ("mch",)),
    Biomarker("mchc", "Mean corpuscular hemoglobin concentration", "hematology", "g/L", ("mchc",)),
    Biomarker("rdw", "Red cell distribution width", "hematology", "%", ("rdw-cv", "rdw cv", "rdw")),
    Biomarker("mpv", "Mean platelet volume", "hematology", "fL", ("mpv",)),
    Biomarker("platelets", "Platelet count", "hematology", "Giga/L", ("trombocitaszam", "platelet")),
    Biomarker("neutrophils_abs", "Neutrophils (absolute)", "hematology", "Giga/L",
              ("neutrofil granulocita #", "neutrophil")),
    Biomarker("neutrophils_pct", "Neutrophils (%)", "hematology", "%", ("neutrofil granulocita %",)),
    Biomarker("lymphocytes_abs", "Lymphocytes (absolute)", "hematology", "Giga/L",
              ("limfocita #", "lymphocyte")),
    Biomarker("lymphocytes_pct", "Lymphocytes (%)", "hematology", "%", ("limfocita %",)),
    Biomarker("monocytes_abs", "Monocytes (absolute)", "hematology", "Giga/L", ("monocita #", "monocyte")),
    Biomarker("monocytes_pct", "Monocytes (%)", "hematology", "%", ("monocita %",)),
    Biomarker("eosinophils_abs", "Eosinophils (absolute)", "hematology", "Giga/L",
              ("eozinofil granulocita #", "eosinophil")),
    Biomarker("eosinophils_pct", "Eosinophils (%)", "hematology", "%", ("eozinofil granulocita %",)),
    Biomarker("basophils_abs", "Basophils (absolute)", "hematology", "Giga/L",
              ("bazofil granulocita #", "basophil")),
    Biomarker("basophils_pct", "Basophils (%)", "hematology", "%", ("bazofil granulocita %",)),
    Biomarker("nrbc_abs", "Nucleated red blood cells (absolute)", "hematology", "Giga/L",
              ("magvas vvt abszolut szam", "nrbc")),
    Biomarker("esr", "Erythrocyte sedimentation rate", "hematology", "mm/óra",
              ("versejtsullyedes", "esr", "we")),
    # --- Lipids ---
    Biomarker("cholesterol_total", "Total cholesterol", "lipid", "mmol/L", ("koleszterin", "cholesterol")),
    Biomarker("hdl", "HDL cholesterol", "lipid", "mmol/L", ("hdl koleszterin", "hdl")),
    Biomarker("ldl", "LDL cholesterol", "lipid", "mmol/L", ("ldl koleszterin", "ldl")),
    Biomarker("triglycerides", "Triglycerides", "lipid", "mmol/L",
              ("triglicerid", "trigliceridek", "triglyceride")),
    # --- Liver ---
    Biomarker("ast", "AST (GOT)", "liver", "U/L", ("got asat", "got", "asat", "ast")),
    Biomarker("alt", "ALT (GPT)", "liver", "U/L", ("gpt alat", "gpt", "alat", "alt")),
    Biomarker("ggt", "Gamma-GT", "liver", "U/L", ("gamma gt ggt", "gamma gt", "ggt")),
    Biomarker("alp", "Alkaline phosphatase", "liver", "U/L", ("alkalikus foszfataz", "alp")),
    Biomarker("bilirubin_total", "Total bilirubin", "liver", "umol/L",
              ("total bilirubin", "bilirubin")),
    Biomarker("bilirubin_direct", "Direct bilirubin", "liver", "umol/L",
              ("direkt bilirubin", "direct bilirubin")),
    Biomarker("total_protein", "Total protein", "liver", "g/L", ("osszfeherje", "total protein")),
    Biomarker("albumin", "Albumin", "liver", "g/L", ("albumin",)),
    Biomarker("transferrin", "Transferrin", "iron", "g/L", ("transzferrin", "transferrin")),
    Biomarker("transferrin_saturation", "Transferrin saturation", "iron", "%",
              ("transzferrin szaturacio", "tsat")),
    # --- Kidney / metabolic ---
    Biomarker("creatinine", "Creatinine", "kidney", "umol/L", ("kreatinin", "creatinine")),
    Biomarker("urea", "Urea", "kidney", "mmol/L", ("karbamid", "urea", "bun")),
    Biomarker("uric_acid", "Uric acid", "kidney", "umol/L", ("hugysav", "uric acid")),
    Biomarker("egfr", "eGFR (CKD-EPI)", "kidney", "mL/min/1.73m2", ("egfr-epi", "egfr epi", "egfr")),
    Biomarker("homocysteine", "Homocysteine", "metabolic", "umol/L", ("homocisztein", "homocysteine")),
    # --- Electrolytes ---
    Biomarker("sodium", "Sodium", "electrolyte", "mmol/L", ("natrium", "na", "sodium")),
    Biomarker("potassium", "Potassium", "electrolyte", "mmol/L", ("kalium", "k", "potassium")),
    # --- Glucose ---
    Biomarker("glucose_fasting", "Fasting glucose", "metabolic", "mmol/L",
              ("glukoz", "glukoz ehgyomri 0 perces vercukor plazma",
               "glukoz 0 terheles elott fluoridos cso", "glucose")),
    # --- Iron / vitamins ---
    Biomarker("iron", "Iron", "iron", "umol/L", ("vas fe", "vas", "fe", "iron")),
    Biomarker("ferritin", "Ferritin", "iron", "ug/L", ("ferritin",)),
    Biomarker("vitamin_b12", "Vitamin B12", "vitamin", "pmol/L", ("b12 vitamin", "vitamin b12", "b12")),
    Biomarker("folate", "Folate", "vitamin", "nmol/L", ("folsav", "folate", "folic acid")),
    Biomarker("vitamin_d", "Vitamin D (25-OH)", "vitamin", "nmol/L",
              ("d vitamin 25oh", "25 oh vitamin d", "vitamin d")),
    # --- Reproductive / adrenal hormones ---
    Biomarker("fsh", "Follicle-stimulating hormone", "hormone", "IU/L",
              ("fsh", "follikulus stimulalo hormon")),
    Biomarker("lh", "Luteinizing hormone", "hormone", "IU/L", ("lh", "luteinizalo hormon")),
    Biomarker("prolactin", "Prolactin", "hormone", "mIU/L", ("prolaktin", "prl", "prolactin")),
    Biomarker("estradiol", "Estradiol", "hormone", "pmol/L", ("osztradiol", "e2", "estradiol")),
    Biomarker("progesterone", "Progesterone", "hormone", "nmol/L", ("progeszteron", "progesterone")),
    Biomarker("testosterone_total", "Total testosterone", "hormone", "nmol/L",
              ("total tesztoszteron", "total testosterone")),
    Biomarker("testosterone_free", "Free testosterone", "hormone", "nmol/L",
              ("szabad tesztoszteron", "free testosterone")),
    Biomarker("testosterone_free_pct", "Free testosterone (%)", "hormone", "%",
              ("szabad tesztoszteron %",)),
    Biomarker("testosterone_bioactive", "Bioactive testosterone", "hormone", "nmol/L",
              ("bioaktiv tesztoszteron",)),
    Biomarker("testosterone_bioactive_pct", "Bioactive testosterone (%)", "hormone", "%",
              ("bioaktiv tesztoszteron %",)),
    Biomarker("androstenedione", "Androstenedione", "hormone", "nmol/L", ("androsztendion",)),
    Biomarker("dhea_s", "DHEA sulfate", "hormone", "umol/L", ("dhea szulfat", "dhea-s", "dheas")),
    Biomarker("shbg", "Sex hormone-binding globulin", "hormone", "nmol/L",
              ("shbg", "szexhormon koto feherje")),
    Biomarker("amh", "Anti-Müllerian hormone", "hormone", "ng/mL",
              ("anti muller hormon amh plusz", "amh")),
    Biomarker("cortisol", "Cortisol", "hormone", "nmol/L", ("kortizol", "kortizol szerum", "cortisol")),
    # --- Thyroid ---
    Biomarker("tsh", "Thyroid-stimulating hormone", "thyroid", "mIU/L",
              ("tsh", "tireoidea stimulalo hormon")),
    Biomarker("ft3", "Free T3", "thyroid", "pmol/L", ("szabad t3", "ft3", "free t3")),
    Biomarker("ft4", "Free T4", "thyroid", "pmol/L", ("szabad t4", "ft4", "free t4")),
    Biomarker("tpo_ab", "Thyroid peroxidase antibody", "thyroid", "IU/mL",
              ("atpo", "tireoidea peroxidaz elleni antitest", "tireoidea peroxidaz auto antitest",
               "atpo gen2", "anti tpo")),
    Biomarker("tg_ab", "Thyroglobulin antibody", "thyroid", "IU/mL",
              ("tireoglobulin elleni antitest", "anti tg gen2", "anti tg")),
    Biomarker("trab", "TSH receptor antibody (TSI)", "thyroid", "IU/L",
              ("tireotropin receptor stimulalo immunglobulinok", "tsi", "trab")),
    # --- Inflammation ---
    Biomarker("crp", "C-reactive protein", "inflammation", "mg/L", ("c reaktiv protein crp", "crp")),
    Biomarker("hscrp", "hs-CRP", "inflammation", "mg/L",
              ("c reaktiv protein ultraszenzitiv hscrp", "hscrp", "hs crp")),
    # --- Urinalysis (qualitative; specimen = urine) ---
    Biomarker("urine_ph", "Urine pH", "urinalysis", None, ("ph",), urine=True),
    Biomarker("urine_protein", "Urine protein", "urinalysis", None, ("feherje", "protein"), urine=True),
    Biomarker("urine_ketones", "Urine ketones", "urinalysis", None, ("keton", "ketone"), urine=True),
    Biomarker("urine_nitrite", "Urine nitrite", "urinalysis", None, ("nitrit", "nitrite"), urine=True),
    Biomarker("urine_urobilinogen", "Urine urobilinogen", "urinalysis", None,
              ("urobilinogen",), urine=True),
    Biomarker("urine_specific_gravity", "Urine specific gravity", "urinalysis", "g/l",
              ("fajsuly", "specific gravity"), urine=True),
    Biomarker("urine_bacteria", "Urine bacteria", "urinalysis", "/ltr(H)", ("bakterium",), urine=True),
    Biomarker("urine_epithelial", "Urine epithelial cells", "urinalysis", "/ltr(H)",
              ("laphamsejt",), urine=True),
    Biomarker("urine_wbc", "Urine white blood cells", "urinalysis", "/ltr(H)",
              ("feherversejt",), urine=True),
    Biomarker("urine_rbc", "Urine red blood cells", "urinalysis", "/ltr(H)",
              ("vorosversejt",), urine=True),
    Biomarker("urine_hemoglobin", "Urine hemoglobin", "urinalysis", None,
              ("hemoglobin vvt",), urine=True),
    # --- Other ---
    Biomarker("hpv_dna", "HPV DNA", "molecular", None, ("hpv dns", "hpv dna")),
)

BY_KEY: dict[str, Biomarker] = {b.key: b for b in REGISTRY}

# --- normalization ---------------------------------------------------------

_A_SUFFIX = re.compile(r"\s*\(a\)\s*$")  # trailing lab-section marker


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Lowercase, deaccent, drop the ``(A)`` suffix, keep ``#``/``%`` as tokens."""
    s = _strip_accents(name).lower().strip()
    s = _A_SUFFIX.sub("", s)
    s = s.replace("#", " # ").replace("%", " % ")
    s = re.sub(r"[()\[\]\-,/.'`´]", " ", s)  # drop punctuation, keep inner words
    return re.sub(r"\s+", " ", s).strip()


def _parenthetical_abbrevs(name: str) -> list[str]:
    """Abbreviations inside parentheses, e.g. '(FSH)', '(K)' — minus the '(A)' suffix."""
    stripped = _A_SUFFIX.sub("", name)
    return [normalize(m) for m in re.findall(r"\(([^)]*)\)", stripped) if m.strip()]


def _base_name(name: str) -> str:
    """Normalized name with all parenthetical groups removed (unit/abbrev noise)."""
    return normalize(re.sub(r"\([^)]*\)", " ", name))


@dataclass
class ResolveResult:
    key: str | None
    method: str  # exact | abbrev | fuzzy | none
    score: float = 0.0


def _build_index() -> tuple[dict[str, str], dict[str, str]]:
    """Return (exact alias index, abbreviation index), both normalized -> key."""
    exact: dict[str, str] = {}
    abbrev: dict[str, str] = {}
    for b in REGISTRY:
        exact.setdefault(normalize(b.name_en), b.key)
        for a in b.aliases:
            na = normalize(a)
            exact.setdefault(na, b.key)
            if " " not in na and len(na) <= 6:  # short single token → also an abbreviation
                abbrev.setdefault(na, b.key)
    return exact, abbrev


_EXACT, _ABBREV = _build_index()
_FUZZY_CUTOFF = 0.86


class RuleResolver:
    """Rule-based resolver: normalize → exact alias → parenthetical abbrev → fuzzy."""

    def __init__(self, fuzzy_cutoff: float = _FUZZY_CUTOFF):
        self.exact = _EXACT
        self.abbrev = _ABBREV
        self.fuzzy_cutoff = fuzzy_cutoff

    def resolve(self, name: str, unit: str | None = None) -> ResolveResult:
        n = normalize(name)
        # Specimen guard: urine sediment uses the /ltr(H) unit; the blood CBC
        # names differ only by the "szam" suffix, so a bare WBC/RBC name at that
        # unit must map to the urine biomarker.
        if unit and "ltr" in unit.lower():
            for key, alias in (("urine_wbc", "feherversejt"), ("urine_rbc", "vorosversejt")):
                if n == alias:
                    return ResolveResult(key, "exact", 1.0)

        if n in self.exact:
            return ResolveResult(self.exact[n], "exact", 1.0)
        # Parenthetical abbreviation (e.g. "(FSH)", "(anti-Tg GEN2)") — check the
        # abbreviation index and the full alias index.
        for ab in _parenthetical_abbrevs(name):
            if ab in self.abbrev:
                return ResolveResult(self.abbrev[ab], "abbrev", 1.0)
            if ab in self.exact:
                return ResolveResult(self.exact[ab], "abbrev", 1.0)
        # Base name with parenthetical noise (units, abbreviations) removed.
        base = _base_name(name)
        if base and base != n and base in self.exact:
            return ResolveResult(self.exact[base], "base", 1.0)
        match = difflib.get_close_matches(n, self.exact.keys(), n=1, cutoff=self.fuzzy_cutoff)
        if match:
            return ResolveResult(self.exact[match[0]], "fuzzy",
                                 difflib.SequenceMatcher(None, n, match[0]).ratio())
        return ResolveResult(None, "none", 0.0)


@dataclass
class Coverage:
    total: int = 0
    resolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
