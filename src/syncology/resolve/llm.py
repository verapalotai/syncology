"""LLM-based biomarker resolver — zero-shot mapping to canonical keys.

The counterpart to the rule-based resolver, for the write-up's precision /
recall / cost comparison. It is given only the canonical registry (keys + English
names + categories) and the raw ``(name, unit)`` pairs — never the alias
dictionary or the gold labels — and must pick a key or abstain (``null``). One
batched API call resolves all names, so cost is reported per run.

Values never reach this path: only test names and units are sent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from syncology import config
from syncology.resolve.biomarkers import BY_KEY, REGISTRY


class Mapping(BaseModel):
    index: int = Field(description="The 1-based number of the input test name")
    canonical_key: str | None = Field(
        None, description="A key from the canonical list, or null if none fits"
    )


class MappingList(BaseModel):
    mappings: list[Mapping]


def _registry_catalog() -> str:
    lines = [f"{b.key}: {b.name_en} ({b.category}{', urine' if b.urine else ''})" for b in REGISTRY]
    return "\n".join(lines)


_SYSTEM = (
    "You map Hungarian laboratory test names to a fixed set of canonical biomarker "
    "keys. You are given the canonical list as 'key: English name (category)'. For "
    "each input test name (with its measurement unit), return the single best "
    "matching key, or null if none of the keys fit. Use the unit to disambiguate "
    "specimen: a blood cell count is in Giga/L or Tera/L, while a urine-sediment "
    "count uses a '/ltr' unit and must map to the urine_* key. Return only keys "
    "from the provided list.\n\nCANONICAL KEYS:\n"
)


def resolve_batch(
    pairs: list[tuple[str, str | None]], *, model: str = config.BULK_MODEL
) -> tuple[dict[str, str | None], int, int]:
    """Resolve ``(name, unit)`` pairs to canonical keys in one call.

    Returns ``(mapping, input_tokens, output_tokens)``. Keys not present in the
    registry are coerced to ``None`` (the model is instructed to abstain, but we
    guard against hallucinated keys).
    """
    import instructor
    from anthropic import Anthropic

    client = instructor.from_anthropic(Anthropic(api_key=config.anthropic_api_key()))
    catalog = _registry_catalog()
    # Number the inputs; the model returns keys by index so we never depend on it
    # echoing the (accented, Hungarian) names back byte-for-byte.
    listing = "\n".join(f"{i}. {name}  [unit: {unit or 'none'}]"
                        for i, (name, unit) in enumerate(pairs, start=1))
    result, completion = client.messages.create_with_completion(
        model=model,
        max_tokens=8192,
        temperature=0,
        max_retries=2,
        system=_SYSTEM + catalog,
        messages=[{"role": "user",
                   "content": f"Map each numbered test name to a key:\n{listing}"}],
        response_model=MappingList,
    )
    mapping: dict[str, str | None] = {name: None for name, _ in pairs}
    for m in result.mappings:
        if 1 <= m.index <= len(pairs):
            key = m.canonical_key if m.canonical_key in BY_KEY else None
            mapping[pairs[m.index - 1][0]] = key
    usage = completion.usage
    return mapping, usage.input_tokens, usage.output_tokens
