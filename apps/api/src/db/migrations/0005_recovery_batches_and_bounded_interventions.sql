-- Revenue Recovery gate.
--
-- Three additions, all additive -- no existing column, constraint, trigger,
-- or row is altered in a way that could invalidate data written by 0001-0004:
--
--   1. Two new bounded intervention action types (ESCALATE_TO_MERCHANT,
--      STOP_RECOVERY) and two new decision types that select them
--      (RECOMMEND_ESCALATION, RECOMMEND_STOP). Both new action types are
--      INTERNAL-ONLY: neither makes any external API call and neither can
--      move money. They exist so that "determine the right intervention"
--      is a real choice between distinct executable outcomes rather than a
--      single capture path.
--
--   2. An AI_DIAGNOSIS_RECORDED audit checkpoint, so a model-produced
--      diagnosis is a first-class, append-only entry in the same audit
--      trail as every deterministic stage -- never a side channel.
--
--   3. recovery_batches / recovery_batch_items: the batch is what makes
--      "measured money recovered across a batch" answerable. The amount
--      at risk is FROZEN into recovery_batch_items.amount_at_risk at
--      detection time, so the denominator of any recovery percentage is
--      the amount that was actually at risk when the batch was detected --
--      not a value re-read later, after recovery has already changed it.
--
-- recovery_batches.source is a CHECK-constrained label, not a comment:
-- the database itself refuses to store a batch that does not declare
-- whether its money is real Razorpay Test Mode money or synthetic. This
-- is what makes "never present synthetic outcomes as real money
-- recovered" an enforced invariant instead of a documentation promise.

-- ---------------------------------------------------------------------
-- 1. Bounded intervention action types + selecting decision types
-- ---------------------------------------------------------------------
alter table actions drop constraint if exists actions_action_type_check;
alter table actions add constraint actions_action_type_check
    check (action_type in (
        'CUSTOMER_RETRY_PROMPT',
        'CAPTURE_PAYMENT',
        'ESCALATE_TO_MERCHANT',
        'STOP_RECOVERY'
    ));

alter table decisions drop constraint if exists decisions_decision_type_check;
alter table decisions add constraint decisions_decision_type_check
    check (decision_type in (
        'RECOMMEND_RETRY_PROMPT',
        'RECOMMEND_CAPTURE',
        'RECOMMEND_MERCHANT_ACTION',
        'RECOMMEND_ESCALATION',
        'RECOMMEND_STOP',
        'NO_ACTION'
    ));

-- ---------------------------------------------------------------------
-- 2. AI diagnosis audit checkpoint
-- ---------------------------------------------------------------------
alter table audit_entries drop constraint if exists audit_entries_checkpoint_check;
alter table audit_entries add constraint audit_entries_checkpoint_check
    check (checkpoint in (
        'EVENT_INGESTED', 'RECONCILIATION_ANOMALY', 'AI_DIAGNOSIS_RECORDED',
        'DECISION_CREATED', 'POLICY_EVALUATED', 'ACTION_BLOCKED',
        'APPROVAL_PENDING', 'APPROVAL_GRANTED', 'ACTION_AUTHORIZED',
        'ACTION_EXECUTED', 'VERIFICATION_COMPLETED'
    ));

-- ---------------------------------------------------------------------
-- 3. Recovery batches
-- ---------------------------------------------------------------------
create table recovery_batches (
    id                uuid primary key default gen_random_uuid(),
    merchant_id       uuid not null references merchants(id),
    -- Enforced provenance of the MONEY in this batch, not of the code.
    -- 'razorpay_test_mode' means every item references a real Razorpay
    -- payment observed through the live API. 'synthetic' means the items
    -- were generated locally and no outcome in this batch may ever be
    -- reported as real money recovered.
    source            text not null check (source in ('razorpay_test_mode', 'synthetic')),
    detected_count    integer not null default 0 check (detected_count >= 0),
    revenue_at_risk   bigint  not null default 0 check (revenue_at_risk >= 0),
    detection_version text not null,
    created_at        timestamptz not null default now()
);

create index idx_recovery_batches_merchant_id on recovery_batches(merchant_id);

create table recovery_batch_items (
    id                 uuid primary key default gen_random_uuid(),
    batch_id           uuid not null references recovery_batches(id) on delete cascade,
    order_id           text not null references orders(id),
    payment_attempt_id text not null references payment_attempts(id),
    -- Frozen at detection time. See header note on denominators.
    amount_at_risk     bigint not null check (amount_at_risk >= 0),
    risk_reason_codes  jsonb  not null,
    -- Filled in once the item has been processed through the pipeline.
    -- Null means detected-but-not-yet-processed, which is a real and
    -- reportable state (batch "pending"), never an error.
    decision_id        uuid references decisions(id),
    created_at         timestamptz not null default now(),
    -- One row per payment attempt per batch: re-running detection for the
    -- same batch can never double-count the same at-risk money.
    unique (batch_id, payment_attempt_id)
);

create index idx_recovery_batch_items_batch_id on recovery_batch_items(batch_id);
create index idx_recovery_batch_items_decision_id on recovery_batch_items(decision_id);
