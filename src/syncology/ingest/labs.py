"""Lab-PDF → structured biomarkers.

Blood-panel PDFs are the most sensitive data in the project, so a **local** model
is preferred (:func:`extract_panel`, Ollama native API — values never leave the
machine). When no local model fits reliably on the available hardware, an
**Anthropic API** fallback is used with explicit consent (:func:`extract_panel_api`);
there the report text transits the API.

Text is pulled with pdfplumber (all panels are text PDFs; see
scripts/triage_labs.py) and parsed into a validated :class:`LabPanel`. Names are
kept **as written** (Hungarian); canonicalization to English and reference-range
reconciliation is the entity-resolution step that follows. Loading is idempotent:
each row carries a hash of ``(source_file, test, date)``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

import duckdb
import httpx
import pdfplumber
from pydantic import BaseModel, Field

from syncology import config

LAB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lab_results (
    row_key      VARCHAR PRIMARY KEY,
    panel_date   DATE NOT NULL,
    source_file  VARCHAR NOT NULL,
    test_name    VARCHAR NOT NULL,   -- as written (Hungarian)
    value_num    DOUBLE,
    value_str    VARCHAR,            -- qualitative result (e.g. "negatív")
    unit         VARCHAR,
    ref_low      DOUBLE,
    ref_high     DOUBLE,
    ref_text     VARCHAR,            -- reference range as written, if non-numeric
    flag         VARCHAR,            -- H / L / normal / null
    model        VARCHAR,            -- provenance: extracting model
    extracted_at TIMESTAMP
);
"""


class LabResult(BaseModel):
    """One biomarker row, values kept as written in the source."""

    test_name: str = Field(description="Test name exactly as written, e.g. 'TSH', 'Glükóz'")
    value_num: float | None = Field(None, description="Numeric result, or null if qualitative")
    value_str: str | None = Field(None, description="Qualitative result if not numeric")
    unit: str | None = Field(None, description="Unit as written, e.g. 'mIU/l', 'mmol/l'")
    ref_low: float | None = Field(None, description="Reference range lower bound, if numeric")
    ref_high: float | None = Field(None, description="Reference range upper bound, if numeric")
    ref_text: str | None = Field(None, description="Reference range as written if non-numeric")
    flag: str | None = Field(None, description="Abnormal flag if present: 'H', 'L', or null")


class LabPanel(BaseModel):
    results: list[LabResult]


_PROMPT = (
    "You extract laboratory blood-panel results from Hungarian lab reports. "
    "The reports are tabular; the columns are typically Vizsgálat (test name), "
    "Eredmény (result), Minősítés (qualifier/flag), Referencia (reference "
    "interval) and Mértékegység (unit). Return every measured analyte as one row "
    "and capture ALL of its columns:\n"
    "- test_name: exactly as written, do NOT translate.\n"
    "- value_num: the numeric result; use value_str instead for qualitative "
    "results (e.g. 'negatív', 'pozitív').\n"
    "- unit: the value from the unit column (Mértékegység), e.g. 'mmol/l', "
    "'g/l', 'mIU/l' — do not omit it when present.\n"
    "- ref_low / ref_high: parse a numeric reference interval like '3.1 - 5.6'; "
    "put a non-numeric range (e.g. '< 5', 'negatív') in ref_text.\n"
    "- flag: 'H' or 'L' if the qualifier column marks the result high/low, else null.\n"
    "Ignore the page header, patient identifiers, addresses and footers — extract "
    "measured analytes only."
)


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(LAB_SCHEMA_SQL)


def _date_from_name(path: Path) -> dt.date:
    """Authoritative panel date from a ``lab_YYYYMMDD.pdf`` filename."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
    if not m:
        raise ValueError(f"no YYYYMMDD date in filename: {path.name}")
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def extract_text(path: str | Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _ollama_native_url() -> str:
    """Ollama's native /api/chat endpoint (from the configured base URL)."""
    return config.OLLAMA_BASE_URL.rsplit("/v1", 1)[0].rstrip("/") + "/api/chat"


def extract_panel(
    text: str,
    *,
    model: str = config.LOCAL_MODEL,
    num_ctx: int = 16384,
    num_predict: int = 8192,
    timeout: float = 300.0,
) -> LabPanel:
    """Parse report text into a validated :class:`LabPanel` using a local model.

    Uses Ollama's native API (not the OpenAI-compatible shim) so we can: constrain
    generation to the Pydantic JSON schema (``format``), lift the default context
    window (``num_ctx`` — a full panel + prompt exceeds Ollama's ~4k default), and
    disable step-by-step "thinking" (which otherwise consumes the token budget).
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": text},
        ],
        "format": LabPanel.model_json_schema(),
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    resp = httpx.post(_ollama_native_url(), json=payload, timeout=timeout)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    return LabPanel.model_validate_json(content)


def extract_panel_api(
    text: str, *, model: str = config.BULK_MODEL, max_tokens: int = 8192
) -> tuple[LabPanel, int, int]:
    """Parse report text into a :class:`LabPanel` via the Anthropic API.

    Fallback for when no local model fits reliably. Values transit the API, so
    this is only used with explicit consent. Returns ``(panel, input_tokens,
    output_tokens)`` so callers can report cost. Uses ``instructor``'s native
    Anthropic tool-use path (reliable structured output — no list-looping).
    """
    import instructor
    from anthropic import Anthropic

    client = instructor.from_anthropic(Anthropic(api_key=config.anthropic_api_key()))
    panel, completion = client.messages.create_with_completion(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        max_retries=2,
        system=_PROMPT,
        messages=[{"role": "user", "content": text}],
        response_model=LabPanel,
    )
    usage = completion.usage
    return panel, usage.input_tokens, usage.output_tokens


def _row_key(source_file: str, test_name: str, panel_date: dt.date) -> str:
    natural = "\x1f".join((source_file, test_name, panel_date.isoformat()))
    return hashlib.sha1(natural.encode("utf-8")).hexdigest()


def load_panel(
    con: duckdb.DuckDBPyConnection,
    path: str | Path,
    panel: LabPanel,
    *,
    model: str = config.LOCAL_MODEL,
) -> int:
    """Insert a panel's results idempotently; return the number of new rows."""
    path = Path(path)
    panel_date = _date_from_name(path)
    now = dt.datetime.now()
    before = con.execute("SELECT count(*) FROM lab_results").fetchone()[0]
    rows = [
        (
            _row_key(path.name, r.test_name, panel_date),
            panel_date,
            path.name,
            r.test_name,
            r.value_num,
            r.value_str,
            r.unit,
            r.ref_low,
            r.ref_high,
            r.ref_text,
            r.flag,
            model,
            now,
        )
        for r in panel.results
    ]
    if rows:
        con.executemany(
            "INSERT OR IGNORE INTO lab_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    after = con.execute("SELECT count(*) FROM lab_results").fetchone()[0]
    return after - before
