-- Phase 3 foundation schema.
-- Implements the 8-table design approved in Phase 2 Revision 2.
-- Append-only enforcement for canonical_events / audit_entries is implemented
-- as database triggers (see bottom of file) rather than a separate
-- least-privilege DB role + connection pool, per the explicit fallback
-- instruction: "If [a separate role] would introduce unnecessary
-- infrastructure complexity, enforce append-only behavior at the
-- repository/service boundary and document the limitation." The
-- repository layer (src/repository/*) additionally exposes no
-- update/delete functions for these two tables at all -- this is
-- defense in depth, not a substitute for either layer.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- merchants
-- ---------------------------------------------------------------------
create table merchants (
    id                 uuid primary key default gen_random_uuid(),
    name               text not null,
    policy_config      jsonb not null default '{}'::jsonb,
    automation_limits  jsonb not null default '{}'::jsonb,
    created_at         timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- orders (RAW mirror of the Razorpay Order entity)
-- ---------------------------------------------------------------------
create table orders (
    id            text primary key,  -- Razorpay order_... id
    merchant_id   uuid not null references merchants(id),
    amount        integer not null check (amount >= 0),
    amount_paid   integer not null check (amount_paid >= 0),
    amount_due    integer not null check (amount_due >= 0),
    status        text not null check (status in ('created', 'attempted', 'paid')),
    attempts      integer not null default 0 check (attempts >= 0),
    currency      text not null default 'INR',
    raw_reference jsonb not null,
    observed_at   timestamptz not null default now()
);

create index idx_orders_merchant_id on orders(merchant_id);

-- ---------------------------------------------------------------------
-- payment_attempts (RAW mirror of the Razorpay Payment entity)
-- One row per pay_... id for its whole life. "created" is retained as a
-- concept (DOCUMENTED, never independently observed in Phase 1) but is
-- not the enforced starting point of any CHECK -- only the enum of
-- possible values is constrained.
-- ---------------------------------------------------------------------
create table payment_attempts (
    id            text primary key,  -- Razorpay pay_... id
    order_id      text not null references orders(id),
    status        text not null check (status in ('created', 'authorized', 'captured', 'failed', 'refunded')),
    method        text,              -- intentionally unconstrained: only 'card' is VERIFIED;
                                      -- upi/netbanking/wallet/emi are plausible but UNVERIFIED,
                                      -- a CHECK here would wrongly assume they can't occur
    captured      boolean not null default false,
    error_source  text,
    error_step    text,
    error_reason  text,
    amount        integer not null check (amount >= 0),
    raw_reference jsonb not null,
    observed_at   timestamptz not null default now()
);

create index idx_payment_attempts_order_id on payment_attempts(order_id);

-- Defense-in-depth transition guard implementing the approved validity
-- matrix (Phase 2 Revision 2, section G) at the database layer, not just
-- in application code. Any UPDATE moving payment_attempts.status through
-- an unlisted (OLD, NEW) pair is rejected outright -- the row is never
-- silently overwritten.
create function guard_payment_attempt_transition() returns trigger as $$
begin
    if old.status = new.status then
        return new;
    end if;

    if (old.status, new.status) in (
        ('created', 'authorized'),
        ('created', 'captured'),
        ('created', 'failed'),
        ('authorized', 'captured'),
        ('authorized', 'failed'),
        ('captured', 'refunded')
    ) then
        return new;
    end if;

    raise exception 'invalid payment_attempt transition: % -> % (id=%)', old.status, new.status, old.id
        using errcode = 'P0001';
end;
$$ language plpgsql;

create trigger trg_guard_payment_attempt_transition
    before update on payment_attempts
    for each row
    execute function guard_payment_attempt_transition();

-- ---------------------------------------------------------------------
-- canonical_events (append-only)
-- ---------------------------------------------------------------------
create table canonical_events (
    id               uuid primary key default gen_random_uuid(),
    merchant_id      uuid not null references merchants(id),
    event_type       text not null check (event_type in (
                          'order.created',
                          'payment.attempt.failed',
                          'payment.attempt.authorized',
                          'payment.attempt.captured',
                          'order.paid',
                          'payment.attempt.anomaly'
                      )),
    source           text not null check (source in ('razorpay_api_poll', 'razorpay_webhook')),
    entity_type      text not null check (entity_type in ('order', 'payment')),
    entity_id        text not null,
    order_id         text not null references orders(id),
    occurred_at      timestamptz not null,
    ingested_at      timestamptz not null default now(),
    payload          jsonb not null,
    source_reference text,
    payload_version  text not null default 'v1'
);

create index idx_canonical_events_order_id on canonical_events(order_id);

-- ---------------------------------------------------------------------
-- expectation_baselines (reusable, recalibrated over time)
-- ---------------------------------------------------------------------
create table expectation_baselines (
    id            uuid primary key default gen_random_uuid(),
    merchant_id   uuid not null references merchants(id),
    bucket_key    text not null,
    recovery_rate numeric(5,4) not null check (recovery_rate >= 0 and recovery_rate <= 1),
    sample_size   integer not null default 0 check (sample_size >= 0),
    updated_at    timestamptz not null default now(),
    unique (merchant_id, bucket_key)
);

-- ---------------------------------------------------------------------
-- decisions (immutable once written -- no update path is exposed)
-- ---------------------------------------------------------------------
create table decisions (
    id                  uuid primary key default gen_random_uuid(),
    merchant_id         uuid not null references merchants(id),
    order_id            text not null references orders(id),
    payment_attempt_id  text references payment_attempts(id),
    event_id            uuid not null references canonical_events(id),
    context_snapshot    jsonb not null,
    expectation         jsonb not null,
    decision_type       text not null check (decision_type in (
                             'RECOMMEND_RETRY_PROMPT',
                             'RECOMMEND_CAPTURE',
                             'RECOMMEND_MERCHANT_ACTION',
                             'NO_ACTION'
                         )),
    confidence          numeric(4,3) not null check (confidence >= 0 and confidence <= 1),
    reason_codes        jsonb not null,
    expected_impact     jsonb not null default '{}'::jsonb,
    model_version       text not null,
    created_at          timestamptz not null default now()
);

create index idx_decisions_order_id on decisions(order_id);
create index idx_decisions_event_id on decisions(event_id);

-- ---------------------------------------------------------------------
-- actions
-- ---------------------------------------------------------------------
create table actions (
    id                   uuid primary key default gen_random_uuid(),
    decision_id          uuid not null references decisions(id),
    idempotency_key      text not null unique,
    action_type          text not null check (action_type in ('CUSTOMER_RETRY_PROMPT', 'CAPTURE_PAYMENT')),
    policy_evaluation    jsonb not null,
    status               text not null check (status in (
                              'PROPOSED', 'POLICY_EVALUATED', 'BLOCKED', 'APPROVAL_PENDING',
                              'AUTHORIZED', 'EXECUTING', 'EXECUTED', 'VERIFYING',
                              'VERIFIED_SUCCESS', 'VERIFIED_FAILED', 'VERIFICATION_UNCERTAIN', 'ESCALATED'
                          )),
    execution_reference  jsonb,
    verification_result  jsonb,
    outcome              jsonb,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create index idx_actions_decision_id on actions(decision_id);

-- ---------------------------------------------------------------------
-- audit_entries (append-only)
-- ---------------------------------------------------------------------
create table audit_entries (
    id           uuid primary key default gen_random_uuid(),
    event_id     uuid references canonical_events(id),
    decision_id  uuid references decisions(id),
    action_id    uuid references actions(id),
    checkpoint   text not null check (checkpoint in (
                     'EVENT_INGESTED', 'RECONCILIATION_ANOMALY', 'DECISION_CREATED',
                     'POLICY_EVALUATED', 'ACTION_BLOCKED', 'APPROVAL_PENDING',
                     'APPROVAL_GRANTED', 'ACTION_AUTHORIZED', 'ACTION_EXECUTED',
                     'VERIFICATION_COMPLETED'
                 )),
    snapshot     jsonb not null,
    created_at   timestamptz not null default now()
);

create index idx_audit_entries_decision_id on audit_entries(decision_id);
create index idx_audit_entries_action_id on audit_entries(action_id);
create index idx_audit_entries_event_id on audit_entries(event_id);

-- ---------------------------------------------------------------------
-- Append-only enforcement: canonical_events and audit_entries reject any
-- UPDATE or DELETE outright, at the database layer.
-- ---------------------------------------------------------------------
create function reject_mutation() returns trigger as $$
begin
    raise exception 'table % is append-only: % is not permitted', tg_table_name, tg_op
        using errcode = 'P0001';
end;
$$ language plpgsql;

create trigger trg_canonical_events_append_only
    before update or delete on canonical_events
    for each row
    execute function reject_mutation();

create trigger trg_audit_entries_append_only
    before update or delete on audit_entries
    for each row
    execute function reject_mutation();
