# datasets/evaluation

The held-out evaluation split. Must remain untouched during model
tuning once it exists (architecture contract §20).

## rule_based_engine_cases.json

### What this measures

Whether `RuleBasedEngine.evaluate()`'s `decision_type` output agrees,
case by case, with a set of 20 independently authored expectations for
the intelligence decision layer. `decision_type` is the sole primary,
hard-fail signal. `reason_category` and `expected.confidence` are
weaker, per-case-optional secondary signals, evaluated only where they
are independently grounded and never silently folded into the primary
match/mismatch result.

### What this does NOT measure

- **Real-world production accuracy.** This is a small (20-case),
  hand-curated synthetic dataset, not a sample of production traffic.
  It has no base rate, so no accuracy/precision/recall/F1/calibration
  figure is computed anywhere in the harness or reported anywhere in
  this dataset -- those words would claim statistical properties this
  artifact cannot support.
- **Policy, Action, Verification, or business-outcome correctness.**
  This evaluation never invokes any of those modules.
- **That the engine is "correct."** See the numerator/denominator
  distinction below -- this is the single most important thing to get
  right when reading this dataset's results.

### The 20/20 result is NOT accuracy, and is not one number

Two counts must never be collapsed into each other:

1. **Dataset agreement: 20/20.** Every case's `decision_type`
   currently matches what `RuleBasedEngine.evaluate()` actually
   returns. This says only that the engine agrees with *this specific,
   small, hand-picked set of cases* -- it is not a percentage of
   anything, and it is not "accuracy."
2. **Grounding: 13/20 independently grounded, 7/20 assumption-based.**
   Of the 20 cases' `decision_type` claims, 8 are `project_defined`
   (traced to a specific section of the master handoff document) and 5
   are `engineering_authored` (traced to the actual git commit message
   of the commit that implemented that rule -- e.g. `git show d10ac06`
   or `git show 34c6762` -- verifiable independently of this session).
   The remaining 7 are `inference_assumption`: honestly disclosed
   judgment calls with no prior project document or commit backing them.

A reader must never say "the engine is 100% correct" from the 20/20
figure. The most that can honestly be said is: **the primary
decision_type evaluation is substantially independently authored --
13 of 20 cases have project-defined or previously-approved-engineering
provenance, and the engine currently agrees with all 20, including the
7 that are explicitly assumption-based.** Do not say "fully
independent" -- 7 of 20 cases remain assumptions, and the 5
`engineering_authored` cases trace to commit messages this assistant
itself wrote (a step more independent than the code's own docstring,
since it is separated, timestamped, immutable project history that
anyone can check with `git show <hash>` -- but not an external or
domain-independent source, and not a verbatim transcript of the
original chat approval).

### Provenance tiers (per field, not per case)

Each of `expected.decision_type` (required), `expected.reason_category`
(optional), and `expected.confidence` (optional) carries its **own**
`{value, tier, reference}` object. A `project_defined` decision_type
never implies its paired confidence claim is also `project_defined` --
confidence and reason_category must independently earn whatever tier
they carry, or the field is simply omitted from that case.

- `project_defined` -- a specific section of
  `razorpay_master_claude_code_handoff_v1.md` or the architecture
  contract.
- `engineering_authored` -- the actual git commit message of the
  commit that implemented the rule, quoting the specific line that
  documents it as a reviewed/locked decision (not a description of
  what the resulting code does).
- `inference_assumption` -- an explicitly unsettled judgment call,
  honestly flagged, never disguised as a stronger claim.
- `observed_production` -- **never a valid tier.** Policy can allow or
  block the identical Decision (Scenario A vs B), so a final
  Action/Verification/Policy outcome is never proof the underlying
  recommendation was correct. This is enforced structurally (the tier
  does not exist in the schema's accepted vocabulary) and by a
  dedicated test.

### Confidence provenance, broken out

Of the 11 cases that evaluate `expected.confidence`: 8 are
`engineering_authored` (RECOMMEND_CAPTURE's fixed 1.0, and
RECOMMEND_RETRY_PROMPT's exact pass-through of `expected_recovery_rate`
-- both traced to commit messages) and 3 are `inference_assumption`
(NO_ACTION's confidence=1.0, for which no project document or commit
message states a numeric value -- treating a definitively-matched
terminal rule as maximally confident is a reasonable but
independently-unverified inference). The remaining 9 cases omit
`expected.confidence` entirely rather than assert an ungrounded claim.
No calibration metric is computed anywhere.

### Reason-category provenance

Only 2 of 20 cases assert `expected.reason_category` (the two
customer-cancellation cases) -- the only place a sub-classification of
`NO_ACTION` adds independently-grounded diagnostic value beyond
decision_type itself. Both are `engineering_authored`, citing the same
commit as their decision_type claim. Every other case omits this field
rather than assert a category that doesn't add independent
information.

### Behavior areas with no independent ground truth

Two of the 7 conceptual behavior areas this dataset covers have **zero**
independently grounded cases -- every case in them is
`inference_assumption`:

- `fallback_unrecognized_state` (refunded status observed directly,
  an unrecognized error_source, and a documented-but-never-observed
  `created` status)
- `order_level_no_payment_attempt` (the single order-level case)

This is disclosed mechanically, not just in prose: `EvaluationReport`
carries a `behavior_areas_inference_only` field computed directly from
the dataset, so this cannot silently go stale.

### Why the dataset is small

20 cases were kept, down from an original 27, after a provenance audit
removed or downgraded every case whose only real evidence was "the
current implementation behaves this way" (citing rule_based.py's
docstring, comments, or branch order). Quality and honest grounding
took priority over hitting a target count -- several boundary-precision
retry-ceiling cases were removed entirely because the exact numeric
ceiling is documented in its own commit message as an unwired
"placeholder," not an independently specified value.

### Scope

This is a first, small, hand-curated regression/behavioral-coverage
artifact for one deterministic rule engine -- not the 50+-merchant
synthetic universe with a real train/validation/held-out split that
the master handoff (§19) and architecture contract (§20) describe.
That remains a substantially larger, future gate, appropriate once an
actual tunable model exists to hold out data for; a deterministic rule
set with no trainable parameters has nothing to tune against a held-out
set today.

See `apps/api/src/evaluation/harness.py` for the loader/comparator
(module docstring has the full methodology) and
`apps/api/tests/test_evaluation_harness.py` for the tests that run it,
including the anti-circularity suite that scans this dataset's raw
text for the specific citation patterns a prior audit found circular.

Status: partial — RuleBasedEngine decision-type coverage only, 13/20
cases independently grounded, 7/20 explicit assumptions.
