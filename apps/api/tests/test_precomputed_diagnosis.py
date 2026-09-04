"""The offline diagnosis path.

Two things matter here and neither is about accuracy:

  * A cache miss must escalate, never guess. This is the same property the
    live API path has, and it is what keeps "the model is unavailable" a safe
    state rather than a silent degradation.
  * The corpus must actually be reachable from a real database row. A corpus
    that never matches anything would fail silently -- every payment would
    escalate, the batch would look plausible, and the AI layer would in effect
    be switched off without anyone noticing. The round-trip test below is what
    stops that.
"""

from __future__ import annotations

import json

import pytest

from diagnosis.diagnoser import DiagnosisUnavailable
from diagnosis.precomputed import (
    CORPUS_PATH,
    FINGERPRINT_KEYS,
    MODEL_VERSION,
    PrecomputedDiagnoser,
    fingerprint,
)
from diagnosis.signals import ALLOWED_SIGNAL_KEYS, FailureSignals, build_failure_signals
from domain.contracts import FailureClass, RootCause
from evaluation.diagnosis_harness import evaluate


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def diagnoser() -> PrecomputedDiagnoser:
    return PrecomputedDiagnoser()


def test_fingerprint_keys_are_a_subset_of_what_the_model_was_shown(corpus):
    """The cache may only be keyed on evidence the model actually saw. Keying
    on anything else would mean serving a classification for inputs that did
    not produce it -- and if that 'anything else' were an amount, it would
    quietly undo the entire no-amount safety property."""
    assert set(FINGERPRINT_KEYS) <= set(ALLOWED_SIGNAL_KEYS)


def test_corpus_entries_all_load(corpus, diagnoser):
    assert len(diagnoser) == len(corpus["entries"])
    assert len(diagnoser) > 0


def test_every_corpus_classification_uses_the_closed_enums(corpus):
    for entry in corpus["entries"]:
        c = entry["classification"]
        RootCause(c["root_cause"])          # raises if the corpus invented a category
        FailureClass(c["failure_class"])
        assert 0.0 <= c["confidence"] <= 1.0
        assert len(c["rationale"]) <= 400
        FailureClass(entry["ground_truth"]["failure_class"])


def test_every_corpus_entry_is_reachable_from_a_database_shaped_row(corpus, diagnoser):
    """Round-trip: corpus evidence -> a payment_attempts row shaped exactly as
    the seeder writes it -> build_failure_signals -> lookup. If this breaks,
    the AI layer silently stops contributing and everything escalates."""
    for entry in corpus["entries"]:
        evidence = entry["evidence"]
        row = {
            "status": "failed",
            "amount": 1234500,  # present on every real row, and must be ignored
            "error_source": evidence["error_source"],
            "error_step": evidence["error_step"],
            "error_reason": evidence["error_reason"],
            "method": evidence["method"],
            "raw_reference": {
                "error_code": evidence["error_code"],
                "error_description": evidence["error_description"],
                "bank": evidence.get("bank"),
                "wallet": evidence.get("wallet"),
            },
        }
        signals = build_failure_signals(row, prior_attempt_count=0)
        diagnosis = diagnoser.diagnose(signals)
        assert diagnosis.failure_class.value == entry["classification"]["failure_class"], entry["id"]


def test_prior_attempt_count_does_not_change_the_fingerprint(diagnoser, corpus):
    """A repeat attempt is the same failure evidence. If the attempt counter
    were part of the key, every retry would miss the cache and escalate."""
    evidence = corpus["entries"][0]["evidence"]
    base = {k: evidence.get(k) for k in FINGERPRINT_KEYS}
    first = diagnoser.diagnose(FailureSignals(**base, prior_attempt_count=0))
    third = diagnoser.diagnose(FailureSignals(**base, prior_attempt_count=3))
    assert first == third


def test_unknown_evidence_escalates_rather_than_guessing(diagnoser):
    """The critical safety test. Evidence sharing an error_reason with a known
    entry must still miss -- the lookup must not fall back to the reason code,
    which is precisely the thing the model exists to improve on."""
    unseen = FailureSignals(
        error_code="BAD_REQUEST_ERROR",
        error_source="issuer",
        error_step="payment_authorization",
        error_reason="payment_failed",     # a reason that IS in the corpus
        error_description="A description that has never been classified before.",
        method="card",
        bank="XXXX",
    )
    with pytest.raises(DiagnosisUnavailable):
        diagnoser.diagnose(unseen)


def test_offline_diagnoses_are_labelled_as_replays(diagnoser, corpus):
    """A persisted diagnosis must say whether it came from a live call or a
    replay, so the two are distinguishable in the database after the fact."""
    evidence = corpus["entries"][0]["evidence"]
    diagnosis = diagnoser.diagnose(FailureSignals(**{k: evidence.get(k) for k in FINGERPRINT_KEYS}))
    assert diagnosis.model_version == MODEL_VERSION
    assert MODEL_VERSION.endswith("/offline")


def test_fingerprint_is_insensitive_to_case_and_whitespace_only():
    a = fingerprint({"error_reason": "payment_failed", "error_description": "Payment failed."})
    b = fingerprint({"error_reason": "  PAYMENT_FAILED ", "error_description": "payment failed. "})
    c = fingerprint({"error_reason": "payment_failed", "error_description": "Payment failed slightly differently."})
    assert a == b
    assert a != c, "materially different descriptions must not collide"


# ---------------------------------------------------------------------------
# The evaluation harness's structural claim
# ---------------------------------------------------------------------------

def test_the_corpus_contains_error_reason_collisions():
    """If this ever became false, the honest conclusion would be that a lookup
    table suffices for this data and the model is not pulling its weight."""
    result = evaluate()
    assert result.collisions, "no collisions means error_reason alone would be sufficient"


def test_every_baseline_error_is_irreducible():
    """The baseline is fitted on the answers, so any error it makes must come
    from a genuine collision rather than from a badly chosen mapping. If these
    diverge, the baseline is not the ceiling it claims to be and the
    comparison is unfair."""
    result = evaluate()
    assert len(result.irreducible_baseline_errors) == len(result.baseline.errors)


def test_the_caveat_disclaims_the_models_own_score():
    """The model's accuracy on this corpus is guaranteed by construction. The
    report must say so -- quoting it as a capability result would be
    misleading, and this test is what stops the disclaimer being edited out
    quietly."""
    result = evaluate()
    assert "BY CONSTRUCTION" in result.caveat
    assert "never be quoted as an accuracy result" in result.caveat
    assert "not evidence of capability" in result.caveat
