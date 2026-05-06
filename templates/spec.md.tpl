# Spec (Frozen) - {{run_id}}

Generated at: {{generated_at}}

## Overview

Describe the problem, scope, and success metrics.

## Module Breakdown

{{module_breakdown}}

## Dependencies (DAG)

Edges:
{{dependency_edges}}

Batches (topological):
{{topo_batches}}

## Risks (Risk-Driven)

- Identify high-risk behaviors and data paths.
- Decide where to apply selective feature flags.
- Define scenario checklist items for E2E and CDC.

## Acceptance Criteria

- Contract validated (signatures/versions/errors consistent across modules)
- Micro-batch unit tests for each module (2-5 functions per batch)
- Integration tests pass
- CDC checks pass (if enabled)
- Dashboard shows all tasks in `done`

