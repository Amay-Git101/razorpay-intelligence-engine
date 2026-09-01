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
6. src/evaluation/* never imports the Razorpay write client or the
   execution/verification write paths, never imports policy/action/
   verification/reconciliation at all, and never imports psycopg (it
   must remain fully independent of the database). It MAY import
   intelligence.rule_based and domain.contracts -- that dependency
   direction is intentional, since RuleBasedEngine is the system under
   evaluation.
7. src/evaluation/* is strictly downstream: nothing under
   src/context/, src/intelligence/, src/policy/, src/action/,
   src/verification/, src/reconciliation/, or src/observability/ may
   import it -- an evaluation harness must never become a dependency of
   any production runtime path.
8. src/feedback/* may write ONLY expectation_baselines (via the
   existing, unmodified intelligence.calibration.upsert_calibrated_baseline()
   / repository.expectation_baselines.upsert_baseline()) -- it must
   contain no other mutating SQL and must never call a mutation
   repository function for any other table (actions, decisions,
   merchants, orders, payment_attempts, canonical_events,
   audit_entries). It must never import the Razorpay write client or
   the execution/verification write paths, and must never import
   observability or evaluation.
9. src/feedback/* is strictly downstream, symmetrically with
   observability/ and evaluation/: nothing under src/context/,
   src/intelligence/, src/policy/, src/action/, src/verification/,
   src/reconciliation/, src/observability/, or src/evaluation/ may
   import it.
10. src/manual_run/* is tooling, not a new orchestration layer: it must
    never import the Razorpay write client, never call capture_payment(),
    never call any mutation repository function for orders,
    payment_attempts, canonical_events, decisions, or actions, and must
    contain no SQL of its own -- it may only call the existing
    orchestration functions (reconcile_order, make_decision,
    propose_action, verify_action, recompute_baselines) and the specific
    read-only repository lookups needed to validate a merchant and
    resolve reconciliation-returned event ids (get_merchant,
    list_events_for_order, get_decision).
11. src/manual_run/* is strictly downstream: nothing under src/context/,
    src/intelligence/, src/policy/, src/action/, src/verification/,
    src/reconciliation/, src/observability/, src/evaluation/, or
    src/feedback/ may import it.

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
    # who WRITES the status, not who can name it -- observability/ and
    # feedback/ only ever read already-persisted status values
    # (mechanically enforced by test_observability_module_contains_no_mutation_operations
    # and test_feedback_module_writes_only_expectation_baselines below),
    # neither ever sets one.
    excluded_packages = {"verification", "domain", "observability", "feedback"}
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


# ---------------------------------------------------------------------------
# evaluation/ is independent of the database and strictly downstream, but
# is intentionally allowed to depend on intelligence.rule_based -- that is
# the system under evaluation.
# ---------------------------------------------------------------------------

def test_evaluation_module_never_imports_razorpay_write_client_or_execution_paths():
    forbidden_substrings = ("razorpay_write_client", "action.orchestrator", "verification.verifier")
    offenders = []
    for path in _all_py_files(SRC_ROOT / "evaluation"):
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_substrings:
            if forbidden in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {forbidden}")
    assert offenders == [], f"src/evaluation/ must not import execution/write paths: {offenders}"


def test_evaluation_module_never_imports_psycopg():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "evaluation"):
        text = path.read_text(encoding="utf-8")
        if "psycopg" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"src/evaluation/ must remain independent of the database: {offenders}"


def test_evaluation_module_never_imports_policy_action_verification_reconciliation():
    forbidden_markers = (
        "from policy", "import policy",
        "from action", "import action",
        "from verification", "import verification",
        "from reconciliation", "import reconciliation",
    )
    offenders = []
    for path in _all_py_files(SRC_ROOT / "evaluation"):
        text = path.read_text(encoding="utf-8")
        for marker in forbidden_markers:
            if marker in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {marker}")
    assert offenders == [], (
        f"src/evaluation/ may depend only on intelligence.rule_based and domain.contracts: {offenders}"
    )


def test_production_runtime_never_imports_evaluation():
    forbidden_markers = ("from evaluation", "import evaluation", "evaluation.harness")
    offenders = []
    for package in ("context", "intelligence", "policy", "action", "verification", "reconciliation", "observability"):
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                if marker in text:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: {marker}")
    assert offenders == [], (
        f"evaluation/ is strictly downstream and must never be imported by production "
        f"runtime code: {offenders}"
    )


# ---------------------------------------------------------------------------
# feedback/ may write ONLY expectation_baselines, and is strictly downstream
# ---------------------------------------------------------------------------

# The one write path this module is allowed to use -- everything else that
# looks like a mutation is forbidden.
_ALLOWED_FEEDBACK_MUTATION_CALLS = ("upsert_calibrated_baseline", "upsert_baseline")

# Mutation repository functions targeting any table OTHER than
# expectation_baselines. Referencing any of these from feedback/ would mean
# it's no longer confined to its one allowed write path.
_FORBIDDEN_FEEDBACK_MUTATION_FUNCTIONS = (
    "insert_decision", "insert_action", "insert_audit_entry", "insert_merchant",
    "insert_canonical_event", "insert_payment_attempt", "upsert_order",
    "update_action_status", "update_payment_attempt_status", "claim_action_for_execution",
)


def test_feedback_module_never_imports_razorpay_write_client_or_execution_paths():
    forbidden_substrings = ("razorpay_write_client", "action.orchestrator", "verification.verifier")
    offenders = []
    for path in _all_py_files(SRC_ROOT / "feedback"):
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_substrings:
            if forbidden in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {forbidden}")
    assert offenders == [], f"src/feedback/ must not import execution/verification write paths: {offenders}"


def test_feedback_module_never_imports_observability_or_evaluation():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "feedback"):
        text = path.read_text(encoding="utf-8")
        if "observability" in text or "evaluation" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"src/feedback/ must not depend on observability/ or evaluation/: {offenders}"


def test_feedback_module_writes_only_expectation_baselines():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "feedback"):
        text = path.read_text(encoding="utf-8")
        if _MUTATION_SQL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: mutation SQL keyword directly present")
        for fn in _FORBIDDEN_FEEDBACK_MUTATION_FUNCTIONS:
            if fn in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {fn}")
    assert offenders == [], f"src/feedback/ may write only expectation_baselines: {offenders}"


def test_feedback_module_uses_the_allowed_calibration_write_path():
    # Positive check, complementing the negative one above: feedback/
    # must actually route its one permitted write through the existing
    # validated wrapper, not bypass it with a hand-rolled upsert.
    text = (SRC_ROOT / "feedback" / "calibration.py").read_text(encoding="utf-8")
    assert any(call in text for call in _ALLOWED_FEEDBACK_MUTATION_CALLS), (
        "src/feedback/calibration.py must write expectation_baselines through "
        "upsert_calibrated_baseline()/upsert_baseline(), not a bespoke write"
    )


def test_production_runtime_and_other_downstream_modules_never_import_feedback():
    forbidden_markers = ("from feedback", "import feedback", "feedback.calibration")
    offenders = []
    for package in (
        "context", "intelligence", "policy", "action", "verification",
        "reconciliation", "observability", "evaluation",
    ):
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                if marker in text:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: {marker}")
    assert offenders == [], (
        f"feedback/ is strictly downstream and must never be imported by production "
        f"runtime code or the other downstream modules: {offenders}"
    )


# ---------------------------------------------------------------------------
# manual_run/ is tooling, not a new orchestration layer
# ---------------------------------------------------------------------------

# The exact orchestration functions manual_run/ is allowed to call --
# nothing else that changes state.
_ALLOWED_MANUAL_RUN_ORCHESTRATION_CALLS = (
    "reconcile_order", "make_decision", "propose_action", "verify_action", "recompute_baselines",
)

# The exact read-only repository lookups manual_run/ is allowed to call.
_ALLOWED_MANUAL_RUN_REPOSITORY_READS = {
    "merchants": {"get_merchant"},
    "canonical_events": {"list_events_for_order"},
    "decisions": {"get_decision"},
}

# A real capture call always looks like `<something>.capture_payment(`.
# Checking for the dotted call form (rather than the bare word) avoids
# flagging this test file's own or manual_run's own prose describing why
# it never does this.
_CAPTURE_PAYMENT_CALL_PATTERN = re.compile(r"\.capture_payment\s*\(")

_IMPORT_FROM_REPOSITORY_PATTERN = re.compile(r"from repository\.(\w+) import ([\w, ]+)")


def test_manual_run_never_imports_razorpay_write_client_or_calls_capture_payment():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: razorpay_write_client")
        if _CAPTURE_PAYMENT_CALL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct capture_payment() call")
    assert offenders == [], f"src/manual_run/ must not touch the Razorpay write path directly: {offenders}"


def test_manual_run_never_calls_mutation_repository_functions():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        text = path.read_text(encoding="utf-8")
        for fn in _FORBIDDEN_FEEDBACK_MUTATION_FUNCTIONS:
            if fn in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {fn}")
    assert offenders == [], f"src/manual_run/ must not mutate orders/payment_attempts/canonical_events/decisions/actions: {offenders}"


def test_manual_run_repository_imports_are_limited_to_the_allowed_read_lookups():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        text = path.read_text(encoding="utf-8")
        for module_name, imported_names in _IMPORT_FROM_REPOSITORY_PATTERN.findall(text):
            allowed = _ALLOWED_MANUAL_RUN_REPOSITORY_READS.get(module_name, set())
            for name in (n.strip() for n in imported_names.split(",")):
                if name not in allowed:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: repository.{module_name}.{name}")
    assert offenders == [], (
        f"src/manual_run/ may only import the specific read-only repository lookups needed "
        f"for merchant/event resolution: {offenders}"
    )


def test_manual_run_contains_no_sql_of_its_own():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        text = path.read_text(encoding="utf-8")
        if "cursor(" in text or ".execute(" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct cursor/execute usage")
        if _MUTATION_SQL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: mutation SQL keyword")
    assert offenders == [], f"src/manual_run/ must contain no SQL of its own -- only calls to already-tested functions: {offenders}"


def test_manual_run_uses_only_the_allowed_orchestration_functions():
    # Positive check: confirms the runner actually calls the pipeline
    # through the intended functions, not a private reimplementation.
    text = (SRC_ROOT / "manual_run" / "run_reconciliation.py").read_text(encoding="utf-8")
    missing = [fn for fn in _ALLOWED_MANUAL_RUN_ORCHESTRATION_CALLS if fn not in text]
    assert missing == [], f"src/manual_run/run_reconciliation.py should call every stage of the pipeline: missing {missing}"


def test_manual_run_never_prints_credential_like_values():
    # A structural, line-level heuristic: no `print(` statement may also
    # reference a credential-bearing name on the same line. This does not
    # prove no secret is ever printed (a heuristic can't), but it catches
    # the obvious mistake of interpolating a credential variable into a
    # user-facing print() call.
    forbidden_on_a_print_line = ("key_secret", "KEY_SECRET", "database_url", "DATABASE_URL", "os.environ")
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "print(" not in line:
                continue
            for forbidden in forbidden_on_a_print_line:
                if forbidden in line:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{line_number}: print() references {forbidden}")
    assert offenders == [], f"src/manual_run/ must never print a credential-bearing value: {offenders}"


def test_manual_run_does_not_duplicate_decision_or_policy_business_rules():
    # manual_run/ should only ever branch on the coarse decision_type /
    # action status strings it prints -- never on the underlying
    # rule/policy literals those modules alone own.
    forbidden_business_rule_literals = (
        "AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE", "GATEWAY_SIDE_FAILURE", "CUSTOMER_CANCELLED",
        "MAX_ATTEMPTS_REACHED", "max_auto_capture_amount", "approval_band_upper",
        "AMOUNT_EXCEEDS_HARD_LIMIT", "WITHIN_APPROVAL_BAND",
    )
    offenders = []
    for path in _all_py_files(SRC_ROOT / "manual_run"):
        text = path.read_text(encoding="utf-8")
        for literal in forbidden_business_rule_literals:
            if literal in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {literal}")
    assert offenders == [], f"src/manual_run/ must not duplicate RuleBasedEngine/Policy's own business-rule literals: {offenders}"


def test_production_and_downstream_modules_never_import_manual_run():
    forbidden_markers = ("from manual_run", "import manual_run", "manual_run.run_reconciliation")
    offenders = []
    for package in (
        "context", "intelligence", "policy", "action", "verification",
        "reconciliation", "observability", "evaluation", "feedback",
    ):
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                if marker in text:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: {marker}")
    assert offenders == [], (
        f"manual_run/ is tooling, not a dependency of the pipeline -- nothing under the "
        f"production or other downstream packages may import it: {offenders}"
    )
