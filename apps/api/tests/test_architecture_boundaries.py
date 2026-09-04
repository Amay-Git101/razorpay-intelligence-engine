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
    contain no SQL of its own -- it may only call
    pipeline.orchestration.run_reconciliation_pipeline() (the shared
    reconcile/decide/propose/verify sequence, also used by the API),
    feedback.calibration.recompute_baselines(), and the one read-only
    repository lookup needed to validate a merchant (get_merchant).
11. src/manual_run/* is strictly downstream: nothing under src/context/,
    src/intelligence/, src/policy/, src/action/, src/verification/,
    src/reconciliation/, src/observability/, src/evaluation/,
    src/feedback/, src/pipeline/, or src/api/ may import it.
12. src/pipeline/* contains the one shared reconcile/decide/propose/
    verify sequence -- it must never import the Razorpay write client,
    never call capture_payment(), never call a mutation repository
    function, and must contain no SQL of its own; it may call only
    reconcile_order, make_decision, propose_action, verify_action, and
    the one read-only lookup (list_events_for_order) needed to resolve
    reconciliation-returned event ids.
13. src/pipeline/* is strictly downstream, symmetrically with the other
    downstream modules: nothing under src/context/, src/intelligence/,
    src/policy/, src/action/, src/verification/, src/reconciliation/,
    src/observability/, src/evaluation/, or src/feedback/ may import
    it. src/manual_run/ and src/api/ MAY import it (that dependency
    direction is the whole point).
14. src/api/* is a leaf delivery layer: it must never import the
    Razorpay write client, never call capture_payment(), never call a
    mutation repository function for any table, never import
    manual_run (API and manual_run are siblings depending on
    pipeline.orchestration, neither depends on the other), and must
    contain no SQL of its own -- it may call only existing repository
    read functions, existing observability read functions, and
    pipeline.orchestration.run_reconciliation_pipeline().
15. src/api/* is strictly downstream: nothing under src/context/,
    src/intelligence/, src/policy/, src/action/, src/verification/,
    src/reconciliation/, src/observability/, src/evaluation/,
    src/feedback/, src/pipeline/, or src/manual_run/ may import it.
16. apps/web/ (the static frontend -- plain HTML/CSS/JS, not a src/
    Python package) must contain no Razorpay credentials, no
    DATABASE_URL, no reference to psycopg/PostgreSQL, no direct
    Razorpay API call, and no duplicated Policy/RuleBasedEngine
    business-rule literals -- it may only talk to the backend via the
    existing relative-URL HTTP API. No backend module under src/ may
    reference apps/web/.

Pure Python, no DB required.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


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
# nothing else that changes state. The reconcile/decide/propose/verify
# sequence itself now lives in pipeline.orchestration.run_reconciliation_pipeline
# (shared with the API), so manual_run only needs to invoke that one
# entry point plus the opt-in calibration step.
_ALLOWED_MANUAL_RUN_ORCHESTRATION_CALLS = (
    "run_reconciliation_pipeline", "recompute_baselines",
)

# The exact read-only repository lookups manual_run/ is allowed to call.
_ALLOWED_MANUAL_RUN_REPOSITORY_READS = {
    "merchants": {"get_merchant"},
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


# ---------------------------------------------------------------------------
# pipeline/ is the one shared reconcile/decide/propose/verify sequence --
# manual_run and api both depend on it, it depends on neither of them.
# ---------------------------------------------------------------------------

_ALLOWED_PIPELINE_ORCHESTRATION_CALLS = ("reconcile_order", "make_decision", "propose_action", "verify_action")

_ALLOWED_PIPELINE_REPOSITORY_READS = {
    "canonical_events": {"list_events_for_order"},
    "decisions": {"get_decision"},
}


def test_pipeline_module_never_imports_razorpay_write_client_or_calls_capture_payment():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "pipeline"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: razorpay_write_client")
        if _CAPTURE_PAYMENT_CALL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct capture_payment() call")
    assert offenders == [], f"src/pipeline/ must not touch the Razorpay write path directly: {offenders}"


def test_pipeline_module_never_calls_mutation_repository_functions_beyond_the_pipeline_itself():
    # The pipeline legitimately calls make_decision/propose_action/
    # verify_action, which themselves write via insert_decision/
    # insert_action/update_action_status/etc -- this test is about
    # pipeline/ never calling those mutation repository functions
    # DIRECTLY, bypassing the orchestration functions that own them.
    offenders = []
    for path in _all_py_files(SRC_ROOT / "pipeline"):
        text = path.read_text(encoding="utf-8")
        for fn in _FORBIDDEN_FEEDBACK_MUTATION_FUNCTIONS:
            if fn in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {fn}")
    assert offenders == [], f"src/pipeline/ must not call mutation repository functions directly: {offenders}"


def test_pipeline_module_repository_imports_are_limited_to_event_resolution():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "pipeline"):
        text = path.read_text(encoding="utf-8")
        for module_name, imported_names in _IMPORT_FROM_REPOSITORY_PATTERN.findall(text):
            allowed = _ALLOWED_PIPELINE_REPOSITORY_READS.get(module_name, set())
            for name in (n.strip() for n in imported_names.split(",")):
                if name not in allowed:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: repository.{module_name}.{name}")
    assert offenders == [], f"src/pipeline/ may only resolve reconciliation-returned events: {offenders}"


def test_pipeline_module_contains_no_sql_of_its_own():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "pipeline"):
        text = path.read_text(encoding="utf-8")
        if "cursor(" in text or ".execute(" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct cursor/execute usage")
        if _MUTATION_SQL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: mutation SQL keyword")
    assert offenders == [], f"src/pipeline/ must contain no SQL of its own: {offenders}"


def test_pipeline_module_uses_the_real_pipeline_functions():
    text = (SRC_ROOT / "pipeline" / "orchestration.py").read_text(encoding="utf-8")
    missing = [fn for fn in _ALLOWED_PIPELINE_ORCHESTRATION_CALLS if fn not in text]
    assert missing == [], f"src/pipeline/orchestration.py should call every stage of the pipeline: missing {missing}"


def test_production_modules_never_import_pipeline():
    forbidden_markers = ("from pipeline", "import pipeline", "pipeline.orchestration")
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
        f"pipeline/ depends on the production modules, never the reverse: {offenders}"
    )


# ---------------------------------------------------------------------------
# api/ is a leaf delivery layer
# ---------------------------------------------------------------------------

_ALLOWED_API_REPOSITORY_READS = {
    "merchants": {"get_merchant", "list_merchants"},
    "orders": {"get_order", "list_orders_for_merchant"},
    # get_payment_attempt added with the guided-journey gate: the
    # customer-history endpoint needs one payment's stored Razorpay object to
    # find the payer. Read-only; insert_payment_attempt and
    # update_payment_attempt_status remain forbidden here.
    "payment_attempts": {"list_payment_attempts_for_order", "get_payment_attempt"},
    "decisions": {"list_decisions_for_order"},
    "actions": {"get_action_for_decision"},
    "audit": {"list_audit_trail"},
    # Batch reads added with the revenue-recovery gate. Both are read-only
    # projections; the batch-executing functions (insert_batch,
    # link_item_decision, finalize_batch) are deliberately NOT allowed here.
    "recovery_batches": {"list_batch_items_with_outcomes", "list_batches_for_merchant", "list_recent_batches"},
    # Cohort reads added with the guided-journey gate. insert_experiment and
    # insert_experiment_order are deliberately NOT allowed: the API creates a
    # cohort only by calling provisioning.test_orders.create_test_orders,
    # which is the one place that also creates the real Razorpay orders the
    # cohort rows are supposed to point at. Letting the delivery layer write
    # cohort rows directly would allow a cohort of orders that do not exist.
    "payment_experiments": {"get_experiment", "list_experiment_orders_with_state"},
}


def test_api_module_never_imports_razorpay_write_client_or_calls_capture_payment():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: razorpay_write_client")
        if _CAPTURE_PAYMENT_CALL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct capture_payment() call")
    assert offenders == [], f"src/api/ must not touch the Razorpay write path directly: {offenders}"


def test_api_module_never_calls_mutation_repository_functions():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        for fn in _FORBIDDEN_FEEDBACK_MUTATION_FUNCTIONS:
            if fn in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {fn}")
    assert offenders == [], f"src/api/ must not mutate production tables: {offenders}"


def test_api_module_repository_imports_are_limited_to_the_allowed_read_lookups():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        for module_name, imported_names in _IMPORT_FROM_REPOSITORY_PATTERN.findall(text):
            allowed = _ALLOWED_API_REPOSITORY_READS.get(module_name, set())
            for name in (n.strip() for n in imported_names.split(",")):
                if name not in allowed:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: repository.{module_name}.{name}")
    assert offenders == [], f"src/api/ may only import the specific read-only repository lookups it needs: {offenders}"


def test_api_module_contains_no_sql_of_its_own():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        if "cursor(" in text or ".execute(" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct cursor/execute usage")
        if _MUTATION_SQL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: mutation SQL keyword")
    assert offenders == [], f"src/api/ must contain no SQL of its own: {offenders}"


def test_api_module_never_imports_manual_run():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        if "manual_run" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"src/api/ must depend on pipeline.orchestration directly, never on manual_run/: {offenders}"
    )


def test_api_module_uses_the_shared_pipeline_function_for_reconciliation():
    text = (SRC_ROOT / "api" / "app.py").read_text(encoding="utf-8")
    assert "run_reconciliation_pipeline" in text, (
        "src/api/app.py's reconcile endpoint must call the shared "
        "pipeline.orchestration.run_reconciliation_pipeline(), not reimplement it"
    )


def test_api_module_never_prints_credential_like_values():
    forbidden_on_a_print_line = ("key_secret", "KEY_SECRET", "database_url", "DATABASE_URL", "os.environ")
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "print(" not in line:
                continue
            for forbidden in forbidden_on_a_print_line:
                if forbidden in line:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{line_number}: print() references {forbidden}")
    assert offenders == [], f"src/api/ must never print a credential-bearing value: {offenders}"


def test_api_module_does_not_duplicate_decision_or_policy_business_rules():
    forbidden_business_rule_literals = (
        "AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE", "GATEWAY_SIDE_FAILURE", "CUSTOMER_CANCELLED",
        "MAX_ATTEMPTS_REACHED", "max_auto_capture_amount", "approval_band_upper",
        "AMOUNT_EXCEEDS_HARD_LIMIT", "WITHIN_APPROVAL_BAND",
    )
    offenders = []
    for path in _all_py_files(SRC_ROOT / "api"):
        text = path.read_text(encoding="utf-8")
        for literal in forbidden_business_rule_literals:
            if literal in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {literal}")
    assert offenders == [], f"src/api/ must not duplicate RuleBasedEngine/Policy's own business-rule literals: {offenders}"


def test_production_modules_never_import_api():
    forbidden_markers = ("from api", "import api", "api.app", "api.schemas")
    offenders = []
    for package in (
        "context", "intelligence", "policy", "action", "verification",
        "reconciliation", "observability", "evaluation", "feedback", "pipeline", "manual_run",
    ):
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                if marker in text:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}: {marker}")
    assert offenders == [], (
        f"api/ is a leaf delivery layer -- no existing backend module may import it: {offenders}"
    )


# ---------------------------------------------------------------------------
# apps/web/ (static frontend) boundaries
# ---------------------------------------------------------------------------

def _all_web_files() -> list[Path]:
    return [p for p in WEB_ROOT.rglob("*") if p.is_file()]


def test_frontend_contains_no_razorpay_or_database_credentials():
    forbidden = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "DATABASE_URL", "postgresql://", "sk_live", "rzp_live")
    offenders = []
    for path in _all_web_files():
        text = path.read_text(encoding="utf-8")
        for forbidden_token in forbidden:
            if forbidden_token in text:
                offenders.append(f"{path.relative_to(WEB_ROOT)}: {forbidden_token}")
    assert offenders == [], f"apps/web/ must never contain a credential-bearing value: {offenders}"


def test_frontend_never_accesses_postgres_directly():
    offenders = []
    for path in _all_web_files():
        text = path.read_text(encoding="utf-8").lower()
        for forbidden_token in ("psycopg", "postgres"):
            if forbidden_token in text:
                offenders.append(f"{path.relative_to(WEB_ROOT)}: {forbidden_token}")
    assert offenders == [], f"apps/web/ must never access PostgreSQL directly: {offenders}"


def test_frontend_never_calls_the_razorpay_rest_api():
    """REPLACES an earlier rule that forbade the frontend any contact with
    a Razorpay domain at all.

    That rule was written when this project had no Checkout integration and
    the frontend genuinely needed none. Real Razorpay Checkout runs in the
    browser by design -- it is loaded from Razorpay's own domain and takes
    the PUBLISHABLE key -- so keeping the old rule would have meant either
    no real payment step or a hand-drawn imitation of Razorpay's payment
    form, and the second of those is far worse than what the rule was
    protecting against.

    What actually needed protecting is narrower and is now enforced
    directly: the browser must never reach Razorpay's server REST API,
    which is the interface that takes the secret. checkout.razorpay.com is
    permitted; api.razorpay.com is not. The companion tests in
    test_frontend.py additionally forbid any hardcoded key and any secret
    in the bundle, so the surface this covers is strictly larger than the
    rule it replaces.
    """
    offenders = []
    for path in _all_web_files():
        text = path.read_text(encoding="utf-8").lower()
        if "api.razorpay.com" in text:
            offenders.append(str(path.relative_to(WEB_ROOT)))
    assert offenders == [], f"apps/web/ must never call Razorpay's server REST API: {offenders}"


def test_frontend_takes_the_publishable_key_from_the_server():
    """The key reaches the browser only from /checkout-config, which is the
    one place that can refuse to serve a live key. A key literal anywhere
    in the bundle would route around that refusal."""
    offenders = []
    for path in _all_web_files():
        for match in re.finditer(r"rzp_(test|live)_\w+", path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(WEB_ROOT)}: {match.group(0)}")
    assert offenders == [], f"a Razorpay key is hardcoded in apps/web/: {offenders}"


def test_frontend_does_not_duplicate_policy_or_engine_business_rules():
    forbidden_business_rule_literals = (
        "max_auto_capture_amount", "approval_band_upper", "AMOUNT_EXCEEDS_HARD_LIMIT",
        "AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE", "GATEWAY_SIDE_FAILURE", "MAX_ATTEMPTS_REACHED",
    )
    offenders = []
    for path in _all_web_files():
        text = path.read_text(encoding="utf-8")
        for literal in forbidden_business_rule_literals:
            if literal in text:
                offenders.append(f"{path.relative_to(WEB_ROOT)}: {literal}")
    assert offenders == [], f"apps/web/ must not duplicate backend business-rule literals: {offenders}"


def test_only_api_app_mounts_the_static_frontend():
    # api/app.py's StaticFiles mount is the one deliberate, approved
    # place the frontend directory is wired in -- nothing else under
    # src/ should reference StaticFiles or the frontend at all.
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if path == SRC_ROOT / "api" / "app.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "StaticFiles" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"only api/app.py may mount the static frontend: {offenders}"


# ---------------------------------------------------------------------------
# AI layer containment
#
# The safety argument for this project is that a language model informs a
# decision but never carries authority. That is an architectural claim, so it
# is checked architecturally: the modules that hold authority must not be able
# to see the model at all.
# ---------------------------------------------------------------------------

_AUTHORITY_PACKAGES = ("policy", "action", "verification", "repository")


def test_authority_holding_packages_never_import_the_diagnosis_layer():
    """Policy decides whether money moves, Action moves it, Verification
    confirms it, and Repository persists it. None of them may import the AI
    layer -- if one did, a model output could reach a money decision without
    passing through the deterministic engine that is supposed to mediate it."""
    offenders = []
    for package in _AUTHORITY_PACKAGES:
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(from|import)\s+diagnosis\b", text, re.MULTILINE):
                offenders.append(f"{path.relative_to(SRC_ROOT)}")
    assert offenders == [], f"authority-holding packages must not import diagnosis/: {offenders}"


def test_authority_holding_packages_never_import_an_llm_sdk():
    offenders = []
    for package in _AUTHORITY_PACKAGES:
        for path in _all_py_files(SRC_ROOT / package):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(from|import)\s+anthropic\b", text, re.MULTILINE):
                offenders.append(f"{path.relative_to(SRC_ROOT)}")
    assert offenders == [], f"authority-holding packages must not import an LLM SDK: {offenders}"


def test_diagnosis_layer_touches_no_database_and_no_razorpay_client():
    """The AI layer is a pure classifier over a projected struct. It cannot
    read the database, so it cannot widen its own inputs beyond the
    allowlist; and it cannot reach Razorpay, so it cannot act."""
    offenders = []
    for path in _all_py_files(SRC_ROOT / "diagnosis"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("psycopg", "repository", "razorpay_client", "razorpay_write_client"):
            if re.search(rf"^\s*(from|import)\s+{forbidden}\b", text, re.MULTILINE):
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {forbidden}")
    assert offenders == [], f"diagnosis/ must stay pure: {offenders}"


def test_risk_detection_uses_no_model():
    """Detection is deterministic by design -- whether revenue is at risk is
    a fact about observed state, not a judgement. A model appearing here
    would add cost and a failure mode without adding information."""
    offenders = []
    for path in _all_py_files(SRC_ROOT / "risk"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("anthropic", "diagnosis"):
            if re.search(rf"^\s*(from|import)\s+{forbidden}\b", text, re.MULTILINE):
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {forbidden}")
    assert offenders == [], f"risk/ must stay deterministic: {offenders}"


def test_only_the_diagnosis_package_imports_the_llm_sdk():
    """Exactly one module in the codebase may talk to a model. Anything else
    importing the SDK is a second, unreviewed path to it."""
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if path.parent.name == "diagnosis":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+anthropic\b", text, re.MULTILINE):
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"only diagnosis/ may import the Anthropic SDK: {offenders}"


def test_the_seeder_can_only_produce_failed_payments():
    """Synthetic payments are only ever 'failed', which is what makes the
    synthetic batch provably free of external calls: no failed payment can
    reach the Razorpay write path. A seeder that could emit 'authorized'
    would be able to trigger a capture against a payment id that does not
    exist at Razorpay."""
    text = (SRC_ROOT / "seed" / "synthetic_backlog.py").read_text(encoding="utf-8")
    assert '"authorized"' not in text
    assert '"captured"' not in text
    assert text.count('status="failed"') >= 1


# ---------------------------------------------------------------------------
# provisioning/ (real Razorpay order creation)
# ---------------------------------------------------------------------------


def test_only_provisioning_and_api_reference_the_order_client():
    """Order creation is a real Razorpay write, even though it moves no
    money. It gets the same containment treatment as the capture adapter:
    one module defines it, one layer constructs it, and nothing else may
    reach it."""
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if _top_level_package(path) in ("provisioning", "api"):
            continue
        text = path.read_text(encoding="utf-8")
        if "razorpay_order_client" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"the order client is reachable from outside provisioning//api: {offenders}"


def test_provisioning_cannot_move_money():
    """The package that creates orders must not be able to capture one.
    Creating a request for payment and taking the money are different
    authorities and they stay in different packages."""
    offenders = []
    for path in _all_py_files(SRC_ROOT / "provisioning"):
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(f"{path.relative_to(SRC_ROOT)}: capture adapter import")
        if _CAPTURE_PAYMENT_CALL_PATTERN.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)}: direct capture_payment() call")
    assert offenders == [], f"src/provisioning/ must not touch the money-moving path: {offenders}"


def test_the_order_client_posts_only_to_the_orders_endpoint():
    """One capability, checked in the source rather than trusted from the
    class name: the only path this adapter POSTs to is /orders."""
    text = (SRC_ROOT / "provisioning" / "razorpay_order_client.py").read_text(encoding="utf-8")
    posted_paths = re.findall(r"_client\.post\(\s*[\"']([^\"']+)[\"']", text)
    assert posted_paths == ["/orders"], f"unexpected Razorpay write paths: {posted_paths}"


def test_order_creation_always_requests_manual_capture():
    """payment_capture is pinned to a module constant rather than passed in
    by a caller, so no call site can create an auto-capturing order and
    remove the decision this whole system exists to make."""
    text = (SRC_ROOT / "provisioning" / "razorpay_order_client.py").read_text(encoding="utf-8")
    assert "MANUAL_CAPTURE = 0" in text
    assert '"payment_capture": MANUAL_CAPTURE' in text


# ---------------------------------------------------------------------------
# risk/failure_patterns.py and context/customer_history.py are read-only
# ---------------------------------------------------------------------------


def test_failure_pattern_analysis_never_writes():
    """It reports on observed payments. A module that could also change
    them could make its own conclusions come true."""
    text = (SRC_ROOT / "risk" / "failure_patterns.py").read_text(encoding="utf-8")
    assert not _MUTATION_SQL_PATTERN.search(text), "failure_patterns.py must be read-only"


def test_customer_history_never_writes_and_never_calls_out():
    text = (SRC_ROOT / "context" / "customer_history.py").read_text(encoding="utf-8")
    assert not _MUTATION_SQL_PATTERN.search(text), "customer_history.py must be read-only"
    for forbidden in ("razorpay_client", "razorpay_order_client", "anthropic", "httpx"):
        assert forbidden not in text, f"customer_history.py must not reach {forbidden}"


def test_customer_history_does_not_put_the_raw_identity_in_the_context():
    """The persisted context carries counts and a fingerprint. The address
    itself stays in the payment row it already lived in."""
    text = (SRC_ROOT / "context" / "builder.py").read_text(encoding="utf-8")
    assert "identity_fingerprint" in text
    assert "customer_email" not in text
