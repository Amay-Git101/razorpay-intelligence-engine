"""Minimal HTTP API surface for the frontend.

This package is an outer delivery layer only: it serializes existing
domain/repository state and calls existing orchestration functions
(pipeline.orchestration.run_reconciliation_pipeline,
observability.metrics.*) -- it never contains business logic, SQL, or
a second write path. See app.py's module docstring for the endpoint
list and schemas.py for the response contracts.
"""
