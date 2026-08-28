-- Corrects a deviation from the approved Phase 2 Revision 2 design.
--
-- decisions.payment_attempt_id was specified as a SOFT reference
-- (nullable text, no physical FK) because not every canonical event is
-- payment-attempt-scoped -- e.g. an order.paid event has no
-- payment_attempt_id at all, but should still be able to produce a
-- Decision row. 0001_init.sql incorrectly added
-- `references payment_attempts(id)`, which would make such decisions
-- impossible to insert. This migration removes that constraint on any
-- database where 0001 already ran with the FK present.
--
-- `if exists` makes this safe to run against a database created from a
-- freshly-corrected 0001_init.sql too, where the constraint never
-- existed in the first place.

alter table decisions drop constraint if exists decisions_payment_attempt_id_fkey;
