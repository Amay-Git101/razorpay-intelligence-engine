"""Tests for the shared reconcile -> decide -> policy -> action -> verify
sequencing extracted from manual_run/ into pipeline/orchestration.py.

Pure Python -- every pipeline-stage function (reconcile_order,
make_decision, propose_action, verify_action) is monkeypatched here.
This file proves the SEQUENCING is correct (which functions get called,
in what order, with what data); it does not re-test reconciliation,
RuleBasedEngine, Policy, Action, capture, or Verification themselves --
those already have their own comprehensive, live-DB-tested suites.
"""

from __future__ import annotations

import uuid

import pytest

import pipeline.orchestration as pipeline
from policy.orchestration import NotPolicyGated


class _FakeConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _install_two_event_fixture(monkeypatch):
    old_id = uuid.uuid4()
    new_id = uuid.uuid4()
    old_event = {"id": old_id, "event_type": "payment.attempt.failed", "order_id": "order_x"}
    new_event = {"id": new_id, "event_type": "payment.attempt.authorized", "order_id": "order_x"}
    monkeypatch.setattr(pipeline, "reconcile_order", lambda *a, **k: [new_id])
    monkeypatch.setattr(pipeline, "list_events_for_order", lambda conn, order_id: [old_event, new_event])
    return old_id, new_id, new_event


def test_zero_new_events_returns_empty_result_without_calling_make_decision(monkeypatch):
    monkeypatch.setattr(pipeline, "reconcile_order", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(pipeline, "make_decision", lambda *a, **k: calls.append(a) or uuid.uuid4())

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert result.new_event_count == 0
    assert result.events == []
    assert calls == []


def test_only_reconciliation_returned_event_ids_are_processed(monkeypatch):
    old_id, new_id, new_event = _install_two_event_fixture(monkeypatch)
    processed = []
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: processed.append(event["id"]) or uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "NO_ACTION"})

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert processed == [new_id]  # old_id, present in list_events_for_order, was never touched
    assert result.new_event_count == 1
    assert result.events[0].decision_type == "NO_ACTION"


def test_unresolved_returned_event_id_raises_rather_than_silently_substituting(monkeypatch):
    unresolvable_id = uuid.uuid4()
    unrelated_event = {"id": uuid.uuid4(), "event_type": "payment.attempt.failed", "order_id": "order_x"}
    monkeypatch.setattr(pipeline, "reconcile_order", lambda *a, **k: [unresolvable_id])
    monkeypatch.setattr(pipeline, "list_events_for_order", lambda conn, order_id: [unrelated_event])
    calls = []
    monkeypatch.setattr(pipeline, "make_decision", lambda *a, **k: calls.append(a) or uuid.uuid4())

    with pytest.raises(pipeline.UnresolvedEventError):
        pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert calls == []


def test_no_action_decision_does_not_call_propose_action(monkeypatch):
    _install_two_event_fixture(monkeypatch)
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "NO_ACTION"})
    propose_calls = []
    monkeypatch.setattr(pipeline, "propose_action", lambda *a, **k: propose_calls.append(a) or {})

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert propose_calls == []
    assert result.events[0].action_skipped_reason == "NO_ACTION"
    assert result.events[0].action_id is None
    assert result.events[0].action_status is None


def test_non_no_action_decision_calls_propose_action_with_no_write_client(monkeypatch):
    _install_two_event_fixture(monkeypatch)
    decision_id = uuid.uuid4()
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: decision_id)
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    propose_calls = []
    monkeypatch.setattr(
        pipeline, "propose_action",
        lambda conn, did, write_client=None: propose_calls.append((did, write_client)) or {"id": uuid.uuid4(), "status": "BLOCKED"},
    )

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert len(propose_calls) == 1
    assert propose_calls[0] == (decision_id, None)
    assert result.events[0].action_status == "BLOCKED"


def test_verifying_action_calls_verify_action(monkeypatch):
    _install_two_event_fixture(monkeypatch)
    action_id = uuid.uuid4()
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(pipeline, "propose_action", lambda *a, **k: {"id": action_id, "status": "VERIFYING"})
    verify_calls = []
    monkeypatch.setattr(pipeline, "verify_action", lambda conn, aid: verify_calls.append(aid) or {"status": "VERIFIED_SUCCESS"})

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert verify_calls == [action_id]
    assert result.events[0].verification_status == "VERIFIED_SUCCESS"


@pytest.mark.parametrize("status", ["BLOCKED", "APPROVAL_PENDING", "AUTHORIZED"])
def test_non_verifying_action_does_not_call_verify_action(monkeypatch, status):
    _install_two_event_fixture(monkeypatch)
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(pipeline, "propose_action", lambda *a, **k: {"id": uuid.uuid4(), "status": status})
    verify_calls = []
    monkeypatch.setattr(pipeline, "verify_action", lambda *a, **k: verify_calls.append(a) or {})

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert verify_calls == []
    assert result.events[0].action_status == status
    assert result.events[0].verification_status is None


def test_approval_pending_never_calls_grant_or_reject_approval(monkeypatch):
    _install_two_event_fixture(monkeypatch)
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_CAPTURE"})
    monkeypatch.setattr(pipeline, "propose_action", lambda *a, **k: {"id": uuid.uuid4(), "status": "APPROVAL_PENDING"})

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert result.events[0].action_status == "APPROVAL_PENDING"
    assert not hasattr(pipeline, "grant_approval")
    assert not hasattr(pipeline, "reject_approval")


def test_unsupported_decision_type_is_reported_not_raised(monkeypatch):
    _install_two_event_fixture(monkeypatch)
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "RECOMMEND_MERCHANT_ACTION"})

    def _raise_not_gated(*a, **k):
        raise NotPolicyGated("unsupported decision_type")

    monkeypatch.setattr(pipeline, "propose_action", _raise_not_gated)

    result = pipeline.run_reconciliation_pipeline(_FakeConn(), object(), "m1", "order_x")

    assert result.events[0].action_skipped_reason == "decision_type is not policy-gated"


def test_each_event_is_committed_before_the_next_is_processed(monkeypatch):
    """Two new events -- confirms commit() is called once per event, not
    once for the whole batch (repeat-run/partial-progress safety)."""
    id_1, id_2 = uuid.uuid4(), uuid.uuid4()
    event_1 = {"id": id_1, "event_type": "payment.attempt.authorized", "order_id": "order_x"}
    event_2 = {"id": id_2, "event_type": "payment.attempt.authorized", "order_id": "order_x"}
    monkeypatch.setattr(pipeline, "reconcile_order", lambda *a, **k: [id_1, id_2])
    monkeypatch.setattr(pipeline, "list_events_for_order", lambda conn, order_id: [event_1, event_2])
    monkeypatch.setattr(pipeline, "make_decision", lambda conn, mid, event: uuid.uuid4())
    monkeypatch.setattr(pipeline, "get_decision", lambda conn, did: {"decision_type": "NO_ACTION"})

    conn = _FakeConn()
    result = pipeline.run_reconciliation_pipeline(conn, object(), "m1", "order_x")

    assert len(result.events) == 2
    assert conn.commits == 2
