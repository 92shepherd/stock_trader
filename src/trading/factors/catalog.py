"""Factor catalog — metadata layer over the 5 factor dicts in src.research.

Why a catalog separate from the registry:
    ALL_FACTORS in research/factor_eval.py is the source of truth for
    factor computation functions and default kwargs. But it doesn't
    carry category labels or human-readable descriptions — those live
    only in the per-category dicts (BASELINE_FACTORS, PEAD_FACTORS, ...)
    and the docstrings of each factor function.

    Bots need a flat lookup with metadata so the REST API can present
    "give me all available factors and what they mean". This module
    flattens the categories into one dict and tags each factor with
    its category.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from src.research.factors_baseline import BASELINE_FACTORS
from src.research.factors_pead import PEAD_FACTORS
from src.research.factors_quality import QUALITY_FACTORS
from src.research.factors_rating import RATING_FACTORS
from src.research.factors_revision import REVISION_FACTORS


@dataclass(frozen=True)
class FactorMetadata:
    """Lightweight metadata about a single factor.

    Attributes:
        name: factor_name (key in ALL_FACTORS). Stable identifier.
        category: 'baseline' / 'pead' / 'quality' / 'rating' / 'revision'.
        description: one-sentence summary (from function docstring).
        higher_is_better: True if a higher raw_value is the "bullish"
            signal (the standard convention used by every factor in
            ALL_FACTORS — values are already sign-flipped during
            calculation when needed). Bots can rely on this.
    """
    name: str
    category: str
    description: str
    higher_is_better: bool = True


def _first_sentence(docstring: str | None) -> str:
    """Extract the first sentence of a docstring as a short description."""
    if not docstring:
        return "<no description>"
    text = docstring.strip()
    paragraph = text.split("\n\n", 1)[0].strip()
    paragraph = " ".join(line.strip() for line in paragraph.splitlines()).strip()
    if "." in paragraph:
        return paragraph.split(".", 1)[0].strip() + "."
    return paragraph[:200]


# Category -> dict of factor_name -> spec
FACTOR_CATEGORIES: dict[str, dict[str, dict]] = {
    "baseline": BASELINE_FACTORS,
    "pead": PEAD_FACTORS,
    "quality": QUALITY_FACTORS,
    "rating": RATING_FACTORS,
    "revision": REVISION_FACTORS,
}


@lru_cache(maxsize=1)
def _build_catalog() -> dict[str, FactorMetadata]:
    """Build the flat name -> metadata map. Cached at import."""
    out: dict[str, FactorMetadata] = {}
    for category, fdict in FACTOR_CATEGORIES.items():
        for name, spec in fdict.items():
            fn = spec.get("fn")
            doc = _first_sentence(fn.__doc__ if fn else None)
            out[name] = FactorMetadata(
                name=name,
                category=category,
                description=doc,
                higher_is_better=True,
            )
    return out


def list_factor_names() -> list[str]:
    """All known factor names, sorted alphabetically."""
    return sorted(_build_catalog().keys())


def get_factor_metadata(name: str) -> FactorMetadata | None:
    """Look up metadata for a factor. Returns None if unknown."""
    return _build_catalog().get(name)


def all_factor_metadata() -> list[FactorMetadata]:
    """All factor metadata, sorted by (category, name)."""
    cat = _build_catalog()
    return sorted(cat.values(), key=lambda m: (m.category, m.name))


def is_known_factor(name: str) -> bool:
    """True if `name` is in the catalog."""
    return name in _build_catalog()
