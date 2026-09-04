"""Does the language model actually earn its place?

The obvious objection to putting a model in this pipeline is that Razorpay
already hands you a structured `error_reason`, so a lookup table should do the
same job for free. This harness tests that objection directly rather than
asserting it away.

THE BASELINE IS DELIBERATELY THE STRONGEST POSSIBLE ONE
    Not a hand-written mapping someone could accuse of being a strawman.
    `best_possible_error_reason_lookup()` FITS the mapping to the evaluation
    data itself: for each error_reason it picks whichever failure class is most
    common among that reason's own entries. No lookup keyed on error_reason can
    beat it on this corpus, because it is chosen with full knowledge of the
    answers. It is the ceiling for that entire class of solution.

    Its errors are therefore not fixable by writing a better table. They are
    irreducible: they come from error_reason values that genuinely map to more
    than one failure class, where the distinguishing information exists only in
    the free-text description. That gap is the model's actual job.

READ THE RESULT HONESTLY
    Both the ground-truth labels and the evidence text were authored by this
    project, so this is NOT an independent benchmark and the accuracy figures
    should never be quoted as one. What the harness does support is the
    narrower structural claim above, which is checkable by inspection: the
    collisions are visible in the corpus file, and `irreducible_baseline_errors`
    lists exactly which entries they cost.

    A reviewer who distrusts the numbers can still verify the structure.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from diagnosis.precomputed import CORPUS_PATH


class ClassifierScore(BaseModel):
    name: str
    correct: int
    total: int
    accuracy: float
    errors: list[str]


class CollisionReport(BaseModel):
    """An error_reason that maps to more than one failure class in the corpus.

    Every one of these is a case no error_reason lookup can resolve, whatever
    mapping it chooses.
    """

    error_reason: str
    classes: list[str]
    entry_ids: list[str]


class DiagnosisEvaluation(BaseModel):
    corpus_version: str
    entry_count: int
    baseline: ClassifierScore
    model: ClassifierScore
    collisions: list[CollisionReport]
    irreducible_baseline_errors: list[str]
    caveat: str


CAVEAT = (
    "The model's score here is self-consistent BY CONSTRUCTION and is not evidence of capability: "
    "the same author wrote both the classifications and the ground-truth labels, so agreement between "
    "them is guaranteed and must never be quoted as an accuracy result. Disregard the model's "
    "percentage entirely. The finding that does survive is structural and independently checkable: the "
    "error_reason values listed below map to more than one failure class in this corpus, so NO lookup "
    "keyed on error_reason can classify them correctly, however it is written. Those cases are what a "
    "description-reading classifier exists to handle. Establishing that it handles them well on real "
    "traffic would need a corpus this project does not have."
)


def load_corpus(corpus_path: Path | None = None) -> dict[str, Any]:
    return json.loads((corpus_path or CORPUS_PATH).read_text(encoding="utf-8"))


def best_possible_error_reason_lookup(entries: list[dict[str, Any]]) -> dict[str, str]:
    """The most accurate error_reason -> failure_class table that exists for
    this corpus, fitted on the corpus's own answers. Ties break toward the
    safer class (AMBIGUOUS routes to a human, so it is never the dangerous
    choice), which if anything makes the baseline look better, not worse."""
    by_reason: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        by_reason[entry["evidence"]["error_reason"]][entry["ground_truth"]["failure_class"]] += 1

    table: dict[str, str] = {}
    for reason, counts in by_reason.items():
        top = max(counts.values())
        winners = sorted(cls for cls, n in counts.items() if n == top)
        table[reason] = "AMBIGUOUS" if "AMBIGUOUS" in winners else winners[0]
    return table


def find_collisions(entries: list[dict[str, Any]]) -> list[CollisionReport]:
    by_reason: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        reason = entry["evidence"]["error_reason"]
        by_reason[reason][entry["ground_truth"]["failure_class"]].append(entry["id"])

    collisions = []
    for reason, classes in sorted(by_reason.items()):
        if len(classes) > 1:
            collisions.append(
                CollisionReport(
                    error_reason=reason,
                    classes=sorted(classes),
                    entry_ids=sorted(i for ids in classes.values() for i in ids),
                )
            )
    return collisions


def evaluate(corpus_path: Path | None = None) -> DiagnosisEvaluation:
    corpus = load_corpus(corpus_path)
    entries = corpus["entries"]
    table = best_possible_error_reason_lookup(entries)

    baseline_correct, baseline_errors = 0, []
    model_correct, model_errors = 0, []

    for entry in entries:
        truth = entry["ground_truth"]["failure_class"]

        predicted = table[entry["evidence"]["error_reason"]]
        if predicted == truth:
            baseline_correct += 1
        else:
            baseline_errors.append(f"{entry['id']}: predicted {predicted}, actual {truth}")

        model_predicted = entry["classification"]["failure_class"]
        if model_predicted == truth:
            model_correct += 1
        else:
            model_errors.append(f"{entry['id']}: predicted {model_predicted}, actual {truth}")

    total = len(entries)
    collisions = find_collisions(entries)
    colliding_ids = {i for c in collisions for i in c.entry_ids}

    return DiagnosisEvaluation(
        corpus_version=corpus["corpus_version"],
        entry_count=total,
        baseline=ClassifierScore(
            name="best possible error_reason lookup (fitted on this corpus)",
            correct=baseline_correct,
            total=total,
            accuracy=baseline_correct / total if total else 0.0,
            errors=baseline_errors,
        ),
        model=ClassifierScore(
            name="Claude Opus 5 classification from failure evidence",
            correct=model_correct,
            total=total,
            accuracy=model_correct / total if total else 0.0,
            errors=model_errors,
        ),
        collisions=collisions,
        irreducible_baseline_errors=[e for e in baseline_errors if e.split(":")[0] in colliding_ids],
        caveat=CAVEAT,
    )


def main() -> None:
    result = evaluate()
    print(f"\nDiagnosis evaluation -- corpus {result.corpus_version}, {result.entry_count} entries\n")
    print(
        f"  HEADLINE: {len(result.collisions)} error_reason values map to more than one failure "
        f"class, costing the best possible lookup table {len(result.irreducible_baseline_errors)} "
        "unavoidable errors."
    )
    print("  The model's own percentage below is self-consistent by construction -- disregard it.")
    print()
    for score in (result.baseline, result.model):
        print(f"  {score.name}")
        print(f"    {score.correct}/{score.total} correct ({score.accuracy * 100:.1f}%)")
        for err in score.errors:
            print(f"      wrong: {err}")
        print()

    print("  error_reason values that map to more than one failure class:")
    if not result.collisions:
        print("    (none -- on this corpus a lookup table would be sufficient)")
    for collision in result.collisions:
        print(f"    {collision.error_reason}: {', '.join(collision.classes)}")
        print(f"      entries: {', '.join(collision.entry_ids)}")

    print(
        f"\n  {len(result.irreducible_baseline_errors)} of the baseline's "
        f"{len(result.baseline.errors)} errors fall on those collisions -- "
        "no error_reason lookup can fix them."
    )
    print(f"\n  CAVEAT: {result.caveat}\n")


if __name__ == "__main__":
    main()
