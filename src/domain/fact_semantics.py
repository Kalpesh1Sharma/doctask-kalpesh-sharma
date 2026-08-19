"""Conservative deterministic normalization for open-schema Fact concepts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_TOKEN = re.compile(r"[a-z0-9]+")
_RELATION_PREFIXES = frozenset({"has", "have", "had"})
_COPULA_PREFIXES = frozenset({"are", "is", "was", "were"})
_QUALIFIER_PREFIXES = frozenset({"estimated", "estimate"})
_GENERIC_PREDICATES = frozenset({"are", "equal", "equals", "is", "value", "was", "were"})
_EVIDENCE_LABEL = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z0-9 /_-]{0,79})\s*:")
_UNDERSPECIFIED_PHRASES = frozenset({"located at"})
_CONTEXT_SUFFIXES = frozenset({("of", "loan"), ("for", "loan")})


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
    while (
        predicate_tokens
        and predicate_tokens[0] in (_RELATION_PREFIXES | _COPULA_PREFIXES)
    ):
        predicate_tokens.pop(0)
    while predicate_tokens and predicate_tokens[0] in _QUALIFIER_PREFIXES:
        predicate_tokens.pop(0)

    if not predicate_tokens or (
        len(predicate_tokens) == 1 and predicate_tokens[0] in _GENERIC_PREDICATES
    ):
        return canonical_field_concept(subject)
    return " ".join(predicate_tokens)


def canonical_evidence_field_concept(source_text: str) -> str:
    """Derive a field concept from an exact key/value evidence span when safe.

    Evidence spans such as ``Loan Amount: $275,000`` retain semantic detail that
    an open-schema extractor may compress into ``Loan / has amount``.  Only a
    simple leading label followed by a colon is accepted; otherwise callers fall
    back to the subject/predicate concept.
    """
    if not isinstance(source_text, str):
        return ""
    first_line = source_text.strip().splitlines()[0] if source_text.strip() else ""
    match = _EVIDENCE_LABEL.match(first_line)
    if match is None:
        return ""
    return canonical_field_concept(match.group("label"))


def canonical_fact_concept_with_evidence(
    subject: str,
    predicate: str,
    source_texts: Iterable[str],
) -> str:
    """Prefer an unambiguous exact-evidence label, then fall back conservatively.

    The durable Fact remains untouched.  This helper is only for deterministic
    semantic comparison/projection.  If evidence labels disagree or are absent,
    the existing subject/predicate normalization remains authoritative.
    """
    base_concept = canonical_fact_concept(subject, predicate)
    concepts: set[str] = set()
    for source_text in source_texts:
        concept = canonical_evidence_field_concept(source_text)
        if concept:
            concepts.add(concept)

    if len(concepts) != 1:
        return base_concept

    evidence_concept = next(iter(concepts))
    if evidence_concept == base_concept:
        return base_concept

    # Exact evidence labels are a repair signal only when the open-schema Fact
    # concept is clearly underspecified (for example ``amount``, ``term``,
    # ``name``, ``status`` or ``located at``).  A specific structured concept
    # such as ``notice days`` remains authoritative even if its evidence uses a
    # synonymous label such as ``Notice period``.  This keeps evidence-based
    # repair conservative while still separating ``Loan Amount`` from
    # ``Estimated Cash to Close``.
    base_tokens = _tokens(base_concept)
    if len(base_tokens) <= 1 or base_concept in _UNDERSPECIFIED_PHRASES:
        return evidence_concept

    evidence_tokens = _tokens(evidence_concept)
    remainder = tuple(base_tokens[len(evidence_tokens) :])
    if (
        base_tokens[: len(evidence_tokens)] == evidence_tokens
        and remainder in _CONTEXT_SUFFIXES
    ):
        return evidence_concept
    return base_concept


def canonical_fact_display_value(
    subject: str,
    predicate: str,
    object_value: object,
    source_texts: Iterable[str],
) -> object:
    """Return a read-only human-facing value without mutating the durable Fact.

    Some valid open-schema extractions encode the field concept in the predicate
    and the literal value in ``subject`` while persisting an empty JSON object as
    ``object_value``.  Review UIs still need to show and compare the grounded
    literal.  Prefer the durable ``object_value`` whenever it is meaningful; only
    repair empty placeholders from an exact, unambiguous ``Label: value`` evidence
    span (or, as a last resort, from ``subject`` when the predicate itself carries
    the concept).
    """
    if not _is_empty_placeholder(object_value):
        return object_value

    evidence_values: set[str] = set()
    for source_text in source_texts:
        value = _evidence_field_value(source_text)
        if value:
            evidence_values.add(value)
    if len(evidence_values) == 1:
        return next(iter(evidence_values))

    subject_concept = canonical_field_concept(subject)
    predicate_concept = canonical_fact_concept(subject, predicate)
    if predicate_concept and predicate_concept != subject_concept:
        return subject
    return object_value


def _is_empty_placeholder(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return not value
    if isinstance(value, (list, tuple)):
        return not value
    return False


def _evidence_field_value(source_text: str) -> str:
    if not isinstance(source_text, str):
        return ""
    first_line = source_text.strip().splitlines()[0] if source_text.strip() else ""
    match = _EVIDENCE_LABEL.match(first_line)
    if match is None:
        return ""
    return first_line[match.end() :].strip()
