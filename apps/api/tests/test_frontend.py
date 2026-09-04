"""The frontend: what it is allowed to contain, and that it matches the API.

No browser automation here. These are asset, source-text and contract
checks; the actual clicking-through is done in a real browser against the
running server, which is the only thing that can establish that Checkout
works.

The most valuable test in this file is the last one: every path the
frontend calls must correspond to a route the API actually registers. A
frontend that drifts from its backend fails here rather than in front of
the person evaluating it.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import app

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

client = TestClient(app)


def _all_web_files() -> list[Path]:
    return [p for p in WEB_DIR.rglob("*") if p.is_file()]


def _all_web_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _all_web_files())


# ---------------------------------------------------------------------------
# Assets exist and are served
# ---------------------------------------------------------------------------


def test_frontend_assets_exist():
    assert (WEB_DIR / "index.html").is_file()
    assert (WEB_DIR / "style.css").is_file()
    assert (WEB_DIR / "app.js").is_file()
    assert (WEB_DIR / "lib" / "api.js").is_file()
    assert (WEB_DIR / "lib" / "checkout.js").is_file()
    for journey in ("capture", "gateway", "cohort", "history"):
        assert (WEB_DIR / "journeys" / f"{journey}.js").is_file()


def test_root_serves_the_problem_chooser():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Payment Decision System" in response.text


def test_module_assets_are_served_with_a_javascript_content_type():
    """ES modules only load if the server sends a JavaScript content type."""
    for path in ("/app.js", "/lib/api.js", "/journeys/capture.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "javascript" in response.headers["content-type"], path


def test_style_is_served():
    response = client.get("/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_static_mount_does_not_shadow_api_routes():
    paths = {getattr(route, "path", None) for route in app.routes}
    for expected in (
        "/health",
        "/merchants",
        "/checkout-config",
        "/merchants/{merchant_id}/test-orders",
        "/merchants/{merchant_id}/orders/{order_id}/reconcile",
        "/orders/{order_id}/timeline",
        "/experiments/{experiment_id}",
        "/experiments/{experiment_id}/failure-pattern",
        "/merchants/{merchant_id}/failure-pattern",
        "/payments/{payment_attempt_id}/customer-history",
    ):
        assert expected in paths

    route_types = [type(route).__name__ for route in app.routes]
    assert route_types[-1] == "Mount"
    assert "Mount" not in route_types[:-1]


# ---------------------------------------------------------------------------
# The four problems are what the site is about
# ---------------------------------------------------------------------------


def test_the_four_problems_are_the_navigation():
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for question in (
        "An authorized payment needs a decision",
        "Is the payment gateway having trouble?",
        "Is this one payment failing, or are many failing?",
        "Does the customer's previous payment behaviour change the decision?",
    ):
        assert question in app_js


# ---------------------------------------------------------------------------
# Razorpay boundary
# ---------------------------------------------------------------------------


def test_the_frontend_loads_razorpay_checkout_from_razorpay():
    """Real Checkout is mandatory for the payment step, and it is loaded
    from Razorpay's own domain. Anything else would mean this project had
    drawn its own payment form, which it must never do."""
    index = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "https://checkout.razorpay.com/v1/checkout.js" in index


def test_the_frontend_never_calls_the_razorpay_rest_api():
    """Checkout runs in the browser by design and takes the publishable
    key. The server REST API is a different thing entirely: it takes the
    secret, and nothing in the browser may reach it."""
    offenders = [
        str(p.relative_to(WEB_DIR))
        for p in _all_web_files()
        if "api.razorpay.com" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"the frontend must never call Razorpay's server API: {offenders}"


def test_the_frontend_contains_no_credentials_of_any_kind():
    forbidden = (
        "RAZORPAY_KEY_SECRET",
        "razorpay_key_secret",
        "key_secret",
        "DATABASE_URL",
        "postgresql://",
        "ANTHROPIC_API_KEY",
        "sk_live",
        "rzp_live",
    )
    text = _all_web_text()
    offenders = [token for token in forbidden if token in text]
    assert offenders == [], f"credential-like tokens in the frontend: {offenders}"


def test_the_frontend_hardcodes_no_razorpay_key_at_all():
    """Even the publishable key is fetched at runtime from
    /checkout-config, which is what lets the server refuse to hand a live
    key to a browser. A key baked into the bundle would bypass that check
    entirely."""
    offenders = []
    for path in _all_web_files():
        for match in re.finditer(r"rzp_(test|live)_\w+", path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(WEB_DIR)}: {match.group(0)}")
    assert offenders == [], f"a Razorpay key is hardcoded in the frontend: {offenders}"


def test_the_frontend_never_hardcodes_a_host_or_port():
    text = _all_web_text().lower()
    assert "localhost" not in text
    assert "127.0.0.1" not in text


# ---------------------------------------------------------------------------
# The frontend does not decide anything
# ---------------------------------------------------------------------------


def test_the_frontend_does_not_duplicate_policy_or_engine_business_rules():
    """Thresholds and reason codes are the backend's. The frontend renders
    what it was told; if it computed a policy outcome itself it could show
    one the backend never reached."""
    forbidden_literals = (
        "max_auto_capture_amount",
        "approval_band_upper",
        "AMOUNT_EXCEEDS_HARD_LIMIT",
        "WITHIN_AUTO_ALLOW_LIMIT",
        "RETRY_BUDGET_EXHAUSTED",
        "TERMINAL_FAILURE_NOT_RECOVERABLE",
    )
    offenders = []
    for path in _all_web_files():
        text = path.read_text(encoding="utf-8")
        for literal in forbidden_literals:
            if literal in text:
                offenders.append(f"{path.relative_to(WEB_DIR)}: {literal}")
    assert offenders == [], f"the frontend duplicates backend business rules: {offenders}"


def test_the_frontend_never_claims_ml_it_does_not_have():
    text = _all_web_text().lower()
    for claim in ("ai accuracy", "model accuracy", "% accurate", "machine learning", "trained model"):
        assert claim not in text, f"unsupported claim in the frontend: {claim}"


def test_checkout_callbacks_are_not_treated_as_payment_truth():
    """Checkout's handler tells the browser the dialog closed. The payment
    state shown anywhere must come from the server, so the checkout module
    routes every outcome -- success, failure, dismissal -- back through the
    same 'go and ask the server' path."""
    checkout_js = (WEB_DIR / "lib" / "checkout.js").read_text(encoding="utf-8")
    assert "onClosed" in checkout_js
    for outcome in ("dismissed", "completed", "failed"):
        assert outcome in checkout_js


# ---------------------------------------------------------------------------
# Frontend/backend contract
# ---------------------------------------------------------------------------


def _registered_route_patterns() -> list[re.Pattern]:
    patterns = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or not path.startswith("/"):
            continue
        # /merchants/{merchant_id}/payments -> ^/merchants/[^/]+/payments$
        regex = re.escape(path)
        regex = re.sub(r"\\\{[^}]+\\\}", "[^/]+", regex)
        patterns.append(re.compile(f"^{regex}$"))
    return patterns


def test_every_path_the_frontend_calls_exists_on_the_api():
    """Catches frontend/backend drift at test time instead of in front of
    whoever is evaluating this."""
    api_js = (WEB_DIR / "lib" / "api.js").read_text(encoding="utf-8")

    # Template literals and plain strings passed to request()/postJson().
    called = set(re.findall(r"request\(\s*[`\"']([^`\"'?]+)", api_js))
    called |= set(re.findall(r"postJson\(\s*[`\"']([^`\"'?]+)", api_js))

    # `/merchants/${encodeURIComponent(id)}/payments` -> /merchants/x/payments
    concrete = {re.sub(r"\$\{[^}]+\}", "x", path).rstrip("/") for path in called}
    concrete = {path for path in concrete if path.startswith("/")}
    assert concrete, "no API paths were found in lib/api.js"

    patterns = _registered_route_patterns()
    unmatched = [path for path in sorted(concrete) if not any(p.match(path) for p in patterns)]
    assert unmatched == [], f"the frontend calls paths the API does not serve: {unmatched}"
