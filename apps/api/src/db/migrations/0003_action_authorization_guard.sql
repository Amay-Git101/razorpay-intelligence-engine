-- Defense-in-depth: reject any INSERT/UPDATE moving actions.status into
-- AUTHORIZED or EXECUTING when the row's own persisted
-- policy_evaluation.allowed is not literally true. This is a second,
-- independent enforcement layer alongside the application-level checks
-- in src/action/orchestrator.py -- deliberately narrow: it only ever
-- checks policy_evaluation.allowed. Approval/RBAC logic (requires_approval,
-- who approved, etc.) stays entirely in application code, not the DB.
--
-- coalesce(...) is required here: a missing/null 'allowed' key must fail
-- CLOSED (treated as not-allowed), not silently pass because
-- `NULL::boolean` makes a naive `if not x then` check ambiguous in
-- plpgsql (NULL is neither true nor false, and an `if NULL` branch does
-- not raise).

create function guard_action_authorization() returns trigger as $$
begin
    if new.status in ('AUTHORIZED', 'EXECUTING') then
        if coalesce((new.policy_evaluation->>'allowed')::boolean, false) is not true then
            raise exception 'action % cannot transition to % - policy_evaluation.allowed is not true', new.id, new.status
                using errcode = 'P0001';
        end if;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger trg_guard_action_authorization
    before insert or update on actions
    for each row
    execute function guard_action_authorization();
