"""Serves previously-computed classifications instead of calling the API.

WHY THIS EXISTS, STATED PLAINLY
    This project has no runtime Anthropic API key. Rather than pretend to
    live inference or drop the AI layer, diagnosis runs OFFLINE: Claude
    Opus 5 classified each distinct failure-evidence pattern in
    datasets/diagnosis/failure_corpus.json, and this class serves those
    classifications at runtime, keyed by the evidence.

    Offline batch inference with online cached serving is an ordinary
    production architecture, not a workaround -- classifications are stable
    per evidence pattern, so recomputing one per payment would spend money to
    get the same answer. What matters is that the claim made about it is
    exact. The deployed system does NOT call a language model at request
    time. It replays model output that was produced earlier, and every
    replayed field carries the model_version that produced it.

    AnthropicDiagnoser remains in the codebase and implements the same
    DiagnosisModel protocol. Supplying ANTHROPIC_API_KEY and passing it
    instead makes the identical pipeline call the API live -- nothing else
    changes. The seam is real, not decorative.

A CACHE MISS IS NOT A GUESS
    Evidence not present in the corpus raises DiagnosisUnavailable, which
    RecoveryEngine routes to human escalation. The lookup never falls back to
    a nearest match, a default class, or the bare error_reason. This is the
    same safety property the live path has: an unavailable diagnosis produces
    more human review, never more automation.

WHAT IS AND IS NOT IN THE FINGERPRINT
    The fingerprint covers the evidence fields that determine the
    classification. prior_attempt_count is deliberately excluded: it varies
    per payment, it does not change what the failure WAS, and including it
    would turn every repeat attempt into a cache miss. How many times a
    payment has been tried is a stopping-rule input, and the stopping rule is
    deterministic code that reads it directly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from domain.contracts import Diagnosis, FailureClass, RootCause

from .diagnoser import DiagnosisUnavailable
from .signals import FailureSignals

CORPUS_PATH = Path(__file__).resolve().parents[4] / "datasets" / "diagnosis" / "failure_corpus.json"

# The evidence fields the classification depends on. Must stay a subset of
# signals.ALLOWED_SIGNAL_KEYS -- asserted by a test, so this cannot silently
# drift into keying on something the model was never shown.
FINGERPRINT_KEYS = ("error_code", "error_source", "error_step", "error_reason", "error_description", "method", "bank")

MODEL_ID = "claude-opus-5"
PROMPT_VERSION = "diagnosis_v1"
# Distinct from AnthropicDiagnoser's MODEL_VERSION by the "offline" marker, so
# a persisted diagnosis always says whether it came from a live call or a
# replay. A reviewer can tell the two apart in the database.
MODEL_VERSION = f"{MODEL_ID}/{PROMPT_VERSION}/offline"


def _normalise(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def fingerprint(evidence: dict[str, Any]) -> str:
    """Stable identity for one failure-evidence pattern.

    Normalised (trimmed, lowercased) so that incidental whitespace or casing
    differences between the corpus and a database row do not cause a spurious
    miss -- but nothing more aggressive than that. No fuzzy matching, no
    stemming, no nearest-neighbour: two genuinely different descriptions must
    produce two different fingerprints, because they may warrant different
    interventions.
    """
    joined = "|".join(f"{key}={_normalise(evidence.get(key))}" for key in FINGERPRINT_KEYS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class PrecomputedDiagnoser:
    """Implements the DiagnosisModel protocol from a corpus file."""

    def __init__(self, corpus_path: Path | None = None):
        self.corpus_path = corpus_path or CORPUS_PATH
        self._by_fingerprint: dict[str, Diagnosis] = {}
        self._load()

    def _load(self) -> None:
        if not self.corpus_path.is_file():
            raise DiagnosisUnavailable(f"diagnosis corpus not found at {self.corpus_path}")
        raw = json.loads(self.corpus_path.read_text(encoding="utf-8"))

        for entry in raw["entries"]:
            classification = entry["classification"]
            self._by_fingerprint[fingerprint(entry["evidence"])] = Diagnosis(
                root_cause=RootCause(classification["root_cause"]),
                failure_class=FailureClass(classification["failure_class"]),
                retry_advisable=classification["retry_advisable"],
                confidence=classification["confidence"],
                rationale=classification["rationale"],
                model_version=MODEL_VERSION,
            )

    def __len__(self) -> int:
        return len(self._by_fingerprint)

    def diagnose(self, signals: FailureSignals) -> Diagnosis:
        key = fingerprint(signals.model_dump())
        diagnosis = self._by_fingerprint.get(key)
        if diagnosis is None:
            raise DiagnosisUnavailable(
                "no precomputed classification for this failure evidence "
                f"(fingerprint {key[:12]}) -- routing to human escalation rather than guessing"
            )
        return diagnosis
