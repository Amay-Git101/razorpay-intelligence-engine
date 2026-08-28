-- Deterministic audit-trail ordering.
--
-- Root cause: audit_entries.created_at defaults to now(), which in
-- PostgreSQL returns the START TIME of the enclosing transaction
-- (transaction_timestamp()) -- identical for every statement executed
-- within that transaction, not the wall-clock moment of each individual
-- INSERT. Every test in this project runs its whole scenario inside one
-- long-lived transaction (committed only via rollback at teardown), so
-- every audit_entries row written during a single test shares the exact
-- same created_at value. `order by created_at asc` therefore has no
-- discriminating power within a test, and ties are broken by whatever
-- order the query planner happens to return -- unspecified, and
-- observably different between a single-column indexed WHERE clause
-- (which happened to look right, via incidental physical/insertion
-- order) and an OR-combined WHERE clause spanning two indexes (which
-- does not).
--
-- created_at is left untouched -- it still means "as of this business
-- transaction", which may be a real, desired semantic elsewhere. This
-- adds a separate, purpose-built, strictly monotonic column used only
-- for ordering. `id` (a random gen_random_uuid()) cannot serve this
-- purpose -- UUIDv4 has no time-ordering property at all.

alter table audit_entries add column sequence_number bigint generated always as identity;

create index idx_audit_entries_sequence_number on audit_entries(sequence_number);
