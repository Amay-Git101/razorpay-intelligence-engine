"""Enforces architecture invariants mechanically, via source inspection,
rather than relying on convention/code review alone:

1. Only src/action/* may import the Razorpay write client.
2. src/verification/* may never import the Razorpay write client either
   -- Verification is strictly read-only.
3. Across the ENTIRE src/ tree, only src/verification/* may reference
   the literal strings VERIFIED_SUCCESS / VERIFIED_FAILED -- exclusively
   Verification's terminal statuses to write.

Pure Python, no DB required.
"""

from __future__ import annotations

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
    # module SETTING that status on an action. This test is about who
    # writes the status, not who can name it.
    excluded_packages = {"verification", "domain"}
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
