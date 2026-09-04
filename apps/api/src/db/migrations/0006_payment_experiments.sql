-- Guided-experiment gate.
--
-- Adds the grouping the four guided problem journeys need and nothing else.
-- Everything here is additive: no existing table, column, constraint or
-- trigger from 0001-0005 is altered.
--
-- WHY A TABLE RATHER THAN A TAG ON orders
-- Problem 03 ("is this one payment failing, or are many?") only means
-- anything if the set of payments being judged is fixed BEFORE their
-- outcomes are known. If the cohort were reconstructed later by querying
-- "recent orders", the set being analysed would shift with every new order
-- and the failure rate would be computed over a denominator that moved --
-- the same denominator dishonesty that risk/detection.py exists to prevent,
-- one layer up. Recording the cohort at creation time freezes the
-- denominator: 4 of 6 is 4 of THOSE six, permanently.
--
-- WHY source IS CHECK-CONSTRAINED TO REAL TEST MODE
-- These orders are created by calling Razorpay's Orders API. Every row here
-- therefore corresponds to an order that genuinely exists in Razorpay Test
-- Mode and can be paid through real Checkout. There is deliberately no
-- 'synthetic' option: a synthetic order cannot be paid, so a synthetic
-- experiment cohort would be a cohort whose outcomes could never be real.
-- The constraint makes that impossible to record rather than merely
-- discouraged. (Synthetic backlog data keeps its own separate, already
-- labelled home in recovery_batches.)

create table payment_experiments (
    id           uuid primary key default gen_random_uuid(),
    merchant_id  uuid not null references merchants(id),
    -- Which guided journey created this cohort. Kept as data so the API can
    -- report what an experiment was for without the frontend having to
    -- remember, and so a stray cohort is always attributable.
    kind         text not null check (kind in (
                     'capture_decision',
                     'failure_pattern',
                     'customer_history'
                 )),
    source       text not null default 'razorpay_test_mode'
                     check (source = 'razorpay_test_mode'),
    label        text,
    created_at   timestamptz not null default now()
);

create index idx_payment_experiments_merchant_id on payment_experiments(merchant_id);

create table payment_experiment_orders (
    id            uuid primary key default gen_random_uuid(),
    experiment_id uuid not null references payment_experiments(id) on delete cascade,
    order_id      text not null references orders(id),
    -- Position in the cohort, so the UI can present a stable "Payment 1..6"
    -- ordering that does not reshuffle as outcomes arrive.
    position      integer not null check (position >= 1),
    created_at    timestamptz not null default now(),
    -- An order belongs to a cohort at most once: re-running creation for the
    -- same experiment can never double-count the same order in the
    -- denominator.
    unique (experiment_id, order_id),
    unique (experiment_id, position)
);

create index idx_payment_experiment_orders_experiment_id
    on payment_experiment_orders(experiment_id);
