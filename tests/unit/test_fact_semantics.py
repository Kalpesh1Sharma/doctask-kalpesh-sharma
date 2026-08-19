from domain.fact_semantics import (
    canonical_evidence_field_concept,
    canonical_fact_concept_with_evidence,
    canonical_fact_display_value,
)


def test_evidence_label_preserves_specific_field_when_open_schema_is_generic() -> None:
    assert canonical_fact_concept_with_evidence(
        "Loan",
        "has amount",
        ("Loan Amount: $275,000",),
    ) == "loan amount"
    assert canonical_fact_concept_with_evidence(
        "Loan",
        "has amount",
        ("Estimated Cash to Close: $12,300",),
    ) == "cash to close"
    assert canonical_fact_concept_with_evidence(
        "Borrower",
        "has name",
        ("Borrower: Priya Raman",),
    ) == "borrower"


def test_evidence_concept_falls_back_when_labels_are_absent_or_ambiguous() -> None:
    assert canonical_evidence_field_concept("plain prose without a field label") == ""
    assert canonical_fact_concept_with_evidence(
        "Loan",
        "has amount",
        ("plain prose",),
    ) == "amount"
    assert canonical_fact_concept_with_evidence(
        "Loan",
        "has amount",
        ("Loan Amount: $275,000", "Estimated Cash to Close: $12,300"),
    ) == "amount"


def test_specific_structured_concept_is_not_overwritten_by_synonymous_evidence_label() -> None:
    assert canonical_fact_concept_with_evidence(
        "contract",
        "notice_days",
        ("Notice period: 45 days",),
    ) == "notice days"


def test_relational_phrase_can_still_be_repaired_from_exact_evidence_label() -> None:
    assert canonical_fact_concept_with_evidence(
        "Property",
        "located at",
        ("Property Address: 442 Cedar Park Drive, Denver, CO 80206",),
    ) == "property address"


def test_copula_predicate_and_loan_context_match_exact_evidence_field() -> None:
    assert canonical_fact_concept_with_evidence(
        "15 years",
        "is loan term",
        ("Loan Term: 15 years",),
    ) == "loan term"
    assert canonical_fact_concept_with_evidence(
        "442 Cedar Park Drive, Denver, CO 80206",
        "is property address of loan",
        ("Property Address: 442 Cedar Park Drive, Denver, CO 80206",),
    ) == "property address"
    assert canonical_fact_concept_with_evidence(
        "Priya Raman",
        "is borrower of loan",
        ("Borrower: Priya Raman",),
    ) == "borrower"


def test_display_value_repairs_empty_legacy_object_from_exact_evidence() -> None:
    assert canonical_fact_display_value(
        "No",
        "has escrow account",
        {},
        ("Escrow Account: No",),
    ) == "No"
    assert canonical_fact_display_value(
        "$275,000",
        "is loan amount",
        {},
        ("Loan Amount: $275,000",),
    ) == "$275,000"
    assert canonical_fact_display_value(
        "Loan Amount",
        "is",
        "$275,000",
        ("Loan Amount: $275,000",),
    ) == "$275,000"
