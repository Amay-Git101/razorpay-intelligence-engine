# apps/web

Static frontend for the Decision Intelligence Console — plain HTML/CSS/JS, no build step, no
framework, no Node dependency. Served directly by the FastAPI app's `StaticFiles` mount
(`apps/api/src/api/app.py`).

```text
index.html   Landing/overview page + Console (dashboard) + Order detail views (hash-routed)
app.js       All application logic -- talks to the backend only via relative fetch() calls
style.css    Dark fintech console aesthetic
```

It never accesses the database or Razorpay directly, never contains a credential, and never
duplicates Policy/RuleBasedEngine business-rule literals -- all enforced by
`apps/api/tests/test_frontend.py` and `apps/api/tests/test_architecture_boundaries.py`.

See the [repository root README](../../README.md) for the full product story, architecture,
and demo instructions.
