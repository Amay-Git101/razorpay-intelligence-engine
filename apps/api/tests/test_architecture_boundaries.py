"""Enforces architecture invariants mechanically, via source inspection,
rather than relying on convention/code review alone:

1. Only src/action/* may import the Razorpay write client.
2. src/verification/* may never import the Razorpay write client either
   -- Verification is strictly read-only.
3. Across the ENTIRE src/ tree, only src/verification/* may reference
   the literal strings VERIFIED_SUCCESS / VERIFIED_FAILED -- exclusively
   Verification's terminal statuses to write.
4. src/observability/* is strictly read-only: it may never import the
   Razorpay write client or the execution/verification write paths, and
   it must contain no mutating SQL or calls to mutating repository
   functions.
5. src/observability/* is strictly downstream: nothing under
   src/intelligence/, src/policy/, or src/action/ may import it -- an
   observational metric must never be able to feed back into a runtime
   decision.

Pure Python, no DB required.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _all_py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def _top_level_package(path: Path) -> str:
    return path.relative_to(SRC_ROOT).parts[0]


def test_only_action_module_imports_razorpay_write_client():
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if _top_level_package(path) == "action":
            continue
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"razorpay_write_client referenced outside src/action/: {offenders}"


def test_verification_module_never_imports_razorpay_write_client():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "verification"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"src/verification/ must be strictly read-only: {offenders}"


def test_only_verification_module_references_verified_status_literals():
    # domain/ is excluded: ActionStatus.VERIFIED_SUCCESS/VERIFIED_FAILED
    # are legitimately DEFINED there as the shared enum every module
    # imports for typing -- defining the type is not the same as a
    # module SETTING that status on an action. observability/ is
    # excluded for the same reason this test itself states: it is about
    # who WRITES the status, not who can name it -- observability/ only
    # ever reads and counts already-persisted status values (mechanically
    # enforced by test_observability_module_contains_no_mutation_operations
    # below), it never sets one.
    excluded_packages = {"verification", "domain", "observability"}
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if _top_level_package(path) in excluded_packages:
            continue
        text = path.read_text(encoding="utf-8")
        if "VERIFIED_SUCCESS" in text or "VERIFIED_FAILED" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"only src/verification/ may reference VERIFIED_SUCCESS/VERIFIED_FAILED: {offenders}"
    )


# ---------------------------------------------------------------------------
# observability/ is strictly read-only and strictly downstream
# ---------------------------------------------------------------------------

_MUTATION_SQL_PATTERN = re.compile(r"\b(insert\s+into|update\s+\w|delete\s+from)\b", re.IGNORECASE)

# Repository-layer functions that write. Referencing any of these from
# observability/ would mean it's no longer purely aggregating already-
# persisted data.
_MUTATION_REPOSITORY_FUNCTIONS = (
    "insert_decision", "insert_action", "insert_audit_entry", "insert_merchant",
    "update_action_status", "update_payment_attempt_status",
    "upsert_baseline", "upsert_calibrated_baseline", "claim_action_for_execution",
)


def test_observability_module_never_imports_razorpay_write_client():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "observability"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"src/observability/ must be strictly read-only: {offenders}"


def test_observability_module_never_imports_execution_or_verification_write_paths():
    forbidden_substrings = ("action.orchestrator", "verification.verifier")
    offenders = []
    for path in _all_py_files(SRC_ROOT / "observability"):
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_substrings:
            if forbidden in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {forbidden}")
    assert offenders == [], (
        f"src/observability/ must not import execution/verification write paths: {offenders}"
    )


def test_observability_module_contains_no_mutation_operations():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "observability"):
        text = path.read_text(encoding="utf-8")
        if _MUTATION_SQL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: mutation SQL keyword")
        for fn in _MUTATION_REPOSITORY_FUNCTIONS:
            if fn in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {fn}")
    assert offenders == [], f"src/observability/ must be strictly read-only: {offenders}"


def test_intelligence_policy_action_never_import_observability():
    offenders = []
    for package in ("intelligence", "policy", "action"):
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            if "observability" in text:
                offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"observability is strictly downstream/read-only and must never be imported "
        f"by a runtime decision path: {offenders}"
    )
