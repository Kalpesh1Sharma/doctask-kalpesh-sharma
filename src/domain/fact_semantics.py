"""Conservative deterministic normalization for open-schema Fact concepts."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+")
_RELATION_PREFIXES = frozenset({"has", "have", "had"})
_QUALIFIER_PREFIXES = frozenset({"estimated", "estimate"})
_GENERIC_PREDICATES = frozenset({"are", "equal", "equals", "is", "value", "was", "were"})


def _tokens(value: str) -> list[str]:
    return _TOKEN.findall(value.casefold())


def canonical_field_concept(value: str) -> str:
    """Normalize a configured field name to the same vocabulary used for Facts."""
    tokens = _tokens(value)
    while tokens and tokens[0] in _QUALIFIER_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)


def canonical_fact_concept(subject: str, predicate: str) -> str:
    """Return a conservative semantic key without mutating the source Fact.

    Extraction is intentionally open-schema, so equivalent facts can arrive as
    ``interest rate`` and ``has Interest Rate``.  Candidate generation may
    normalize those lightweight relation/estimate wrappers while all durable
    source text and evidence remain untouched.

    Generic predicates such as ``equals`` carry no concept themselves, so the
    subject remains the concept key in that case.
    """
    predicate_tokens = _tokens(predicate)
    while predicate_tokens and predicate_tokens[0] in _RELATION_PREFIXES:
        predicate_tokens.pop(0)
    while predicate_tokens and predicate_tokens[0] in _QUALIFIER_PREFIXES:
        predicate_tokens.pop(0)

    if not predicate_tokens or (
        len(predicate_tokens) == 1 and predicate_tokens[0] in _GENERIC_PREDICATES
    ):
        return canonical_field_concept(subject)
    return " ".join(predicate_tokens)
