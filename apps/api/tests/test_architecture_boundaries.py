"""Enforces two Gate 8 architecture invariants mechanically, via source
inspection, rather than relying on convention/code review alone:

1. Only src/action/* may import the Razorpay write client.
2. src/action/* never references VERIFIED_SUCCESS / VERIFIED_FAILED --
   those are exclusively Gate 9 (Verification)'s to write.

Pure Python, no DB required.
"""

from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _all_py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.is_file()]


def _is_inside_action_package(path: Path) -> bool:
    return path.relative_to(SRC_ROOT).parts[0] == "action"


def test_only_action_module_imports_razorpay_write_client():
    offenders = []
    for path in _all_py_files(SRC_ROOT):
        if _is_inside_action_package(path):
            continue
        text = path.read_text(encoding="utf-8")
        if "razorpay_write_client" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"razorpay_write_client referenced outside src/action/: {offenders}"


def test_action_module_never_references_verified_status_literals():
    offenders = []
    for path in _all_py_files(SRC_ROOT / "action"):
        text = path.read_text(encoding="utf-8")
        if "VERIFIED_SUCCESS" in text or "VERIFIED_FAILED" in text:
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"src/action/ must never reference VERIFIED_SUCCESS/VERIFIED_FAILED "
        f"(exclusively Gate 9's to write): {offenders}"
    )
