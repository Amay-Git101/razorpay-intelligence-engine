"""Tests for the static frontend and its integration with the FastAPI app.

Deliberately does NOT introduce browser automation (no Playwright/
Selenium) -- these are asset-existence, HTTP-serving, and source-text
checks only. The existing backend/API test suite remains the source of
truth for backend behavior; these tests only prove the frontend is
present, served correctly, and doesn't duplicate or leak anything it
shouldn't.

Pure Python -- no DATABASE_URL, no network, no live Postgres (the
static-file and route-availability checks below don't touch the DB;
FastAPI's TestClient serves requests in-process).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import app

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

client = TestClient(app)


# ---------------------------------------------------------------------------
# Frontend assets exist
# ---------------------------------------------------------------------------


def test_frontend_directory_exists():
    assert WEB_DIR.is_dir()


def test_frontend_assets_exist():
    assert (WEB_DIR / "index.html").is_file()
    assert (WEB_DIR / "style.css").is_file()
    assert (WEB_DIR / "app.js").is_file()


# ---------------------------------------------------------------------------
# FastAPI serves the frontend
# ---------------------------------------------------------------------------


def test_root_serves_index_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Decision Intelligence Console" in response.text


def test_style_css_is_served():
    response = client.get("/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_app_js_is_served():
    response = client.get("/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Existing API endpoints remain available (static mount doesn't shadow them)
# ---------------------------------------------------------------------------


def test_health_endpoint_still_reachable():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_mount_does_not_shadow_api_routes():
    paths = {getattr(route, "path", None) for route in app.routes}
    for expected in (
        "/health", "/merchants", "/merchants/{merchant_id}/payments",
        "/merchants/{merchant_id}/metrics", "/orders/{order_id}", "/orders/{order_id}/timeline",
        "/merchants/{merchant_id}/orders/{order_id}/reconcile",
    ):
        assert expected in paths

    # The catch-all static mount must be registered LAST, so Starlette
    # tries every explicit API route first, in order, before ever
    # falling through to it.
    route_types = [type(route).__name__ for route in app.routes]
    assert route_types[-1] == "Mount"
    assert "Mount" not in route_types[:-1]


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------


def test_frontend_js_contains_no_credential_like_values():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    forbidden = ("RAZORPAY_KEY", "razorpay_key_secret", "DATABASE_URL", "postgresql://", "sk_live", "rzp_live")
    offenders = [f for f in forbidden if f.lower() in text.lower()]
    assert offenders == [], f"app.js references credential-like tokens: {offenders}"


def test_frontend_js_never_hardcodes_a_host_or_port():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "localhost" not in text.lower()
    assert "127.0.0.1" not in text


# ---------------------------------------------------------------------------
# Reconcile uses the correct endpoint
# ---------------------------------------------------------------------------


def test_frontend_reconcile_call_targets_the_correct_endpoint():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "/orders/${" in text and "/reconcile" in text
    assert "method: \"POST\"" in text or "method:'POST'" in text or "'POST'" in text or '"POST"' in text


# ---------------------------------------------------------------------------
# No duplicated backend business logic in the frontend
# ---------------------------------------------------------------------------


def test_frontend_does_not_duplicate_policy_or_engine_business_rules():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    forbidden_business_rule_literals = (
        "max_auto_capture_amount", "approval_band_upper", "AMOUNT_EXCEEDS_HARD_LIMIT",
        "AUTHORIZED_PAYMENT_ELIGIBLE_FOR_CAPTURE", "GATEWAY_SIDE_FAILURE", "MAX_ATTEMPTS_REACHED",
    )
    offenders = [literal for literal in forbidden_business_rule_literals if literal in text]
    assert offenders == [], f"app.js duplicates backend business-rule literals: {offenders}"


def test_frontend_never_claims_ml_accuracy_or_model_performance():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8") + (WEB_DIR / "index.html").read_text(encoding="utf-8")
    forbidden_claims = ("ai accuracy", "model accuracy", "% accurate", "prediction success rate", "calibration accuracy")
    lowered = text.lower()
    offenders = [claim for claim in forbidden_claims if claim in lowered]
    assert offenders == [], f"frontend makes an unsupported accuracy/performance claim: {offenders}"


def test_frontend_never_calls_the_engine_an_ml_model():
    text = (WEB_DIR / "app.js").read_text(encoding="utf-8") + (WEB_DIR / "index.html").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "machine learning" not in lowered
    assert "trained model" not in lowered
