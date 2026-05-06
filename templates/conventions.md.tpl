# Engineering Conventions (Tree Pipeline)

## Branching & Integration

- Prefer small, incremental PRs.
- Use selective feature flags for risky paths.

## Micro-batch Testing (2-5 functions)

- Group related functions into batches of 2-5.
- After each batch, run unit tests focused on that batch.

## Quality Strategy

- Risk-driven line coverage target: 80-90%
- Maintain a scenario checklist for high-risk flows.
- CDC tests required when contracts expose stable APIs.

## Refactor Cap

- Only 1 refactor round is allowed after review feedback.
- If still failing, escalate to a human decision.

## Rollback Strategy

1. Disable the feature flag (fast rollback)
2. Revert the merge commit
3. Fix forward via a dedicated PR
4. Freeze downstream until compatibility is restored

