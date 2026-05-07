# Architecture: Multi-Branch, Multi-Session Development Workflow

## 1. Overview

This architecture is designed for a multi-module project where implementation velocity comes from parallelism, but quality control comes from a single protected integration point. Each functional module gets its own long-lived feature branch created directly from `main`, and each branch is operated by its own Claude or Codex session. That makes the branch the unit of ownership, planning, implementation, and module-local verification.

The core philosophy is "parallel development, centralized acceptance." Module branches are allowed to move quickly, write implementation code, and evolve their own unit and module-level integration tests without blocking unrelated modules. The `main` branch is intentionally treated as a gate, not a coding lane: it owns review, cross-module validation, project-level documentation, and merge decisions, but it does not originate feature implementation. This separates construction from acceptance in the same way regulated CI/CD pipelines separate build stages from promotion gates.

Inside each module branch, work fans out again. One top-level Claude/Codex session owns the module branch and breaks the module into file- or component-scoped sub-tasks handled by subagents in parallel. This creates a three-level topology: repository orchestrator on `main`, one branch session per module, and multiple file-level subagents under each module session. The pattern resembles fan-out/fan-in execution used by GitLab CI `needs` DAG pipelines and GitHub Actions reusable workflows, but applied to human/agent collaboration.

The `main` branch acts like an integration manager. It receives pull requests from module branches, requests CODEOWNERS review, runs required CI checks, executes cross-module integration tests, updates project-level docs such as the overall architecture and roadmap, and merges only when the full acceptance bar is met. This mirrors branch-protection and gated-merge practices commonly used in GitHub Enterprise, GitLab, and Jenkins-based pipelines where release quality is controlled by a protected branch.

Because the hard constraint is "one branch per functional module, branched from `main`," this workflow is intentionally not trunk-based development in the pure Paul Hammant sense. Instead, it borrows trunk-based ideas selectively: keep branches scoped to one module, keep them rebased or regularly synced with `main`, keep feedback loops short, and avoid using branches as indefinite release lines. The result is a hybrid model optimized for agent parallelism rather than for minimizing branch count.

## 2. Branching Model

The repository uses a hub-and-spoke branching model. `main` is the protected integration branch and the only branch from which module branches are created. Every functional module has exactly one active module branch at a time, for example `module/auth`, `module/payments`, `module/search-index`, or `module/ui-shell`. If the team needs iteration markers, append a work item or milestone suffix, such as `module/auth/m1-token-rotation`, but keep the module name first so branch policies, dashboards, and automation can group work by module.

Branch lifecycle:

1. Create: a maintainer creates `module/<module-name>` from the current `main` HEAD after confirming that module boundaries, ownership, and success criteria are documented.
2. Develop: the module session works only on that branch, adds implementation plus module-scoped tests, and maintains a module-local management document such as `MODULE_PLAN.md`, `MODULE_NOTES.md`, or `docs/modules/<module-name>.md`.
3. Review prep: before requesting merge, the module branch must be updated from `main` by merge or rebase according to repository policy, resolve conflicts locally, and produce a PR description that lists changed files, owned interfaces, test evidence, and unresolved risks.
4. Review: the branch opens a PR into `main`; no direct pushes to `main` are allowed.
5. Merge: `main` accepts the PR only after required approvals and checks pass; prefer squash merge for clean module-level history, or merge queue if the repository is busy.
6. Archive: after merge, delete the remote module branch unless follow-up work is already scheduled. If work remains open, cut a new branch from updated `main` instead of reviving a stale branch.

Naming conventions:

- Branches: `module/<module-name>` or `module/<module-name>/<ticket-or-milestone>`
- Pull requests: `[module:<module-name>] <summary>`
- Module docs: `MODULE_PLAN.md` in the module root, or `docs/modules/<module-name>/MODULE_PLAN.md`
- Test labels: `unit:<module-name>`, `integration:<module-name>`, `cross-module`

Protection rules for `main` should be strict:

- Require pull requests before merge.
- Require at least 2 approvals, with at least 1 from the `main` gate owner or designated integration maintainer.
- Require CODEOWNERS approval for touched paths.
- Require all status checks to pass.
- Require conversation resolution before merge.
- Require linear history if the team wants a clean audit trail; otherwise allow squash-only merges.
- Disable force-push and branch deletion.
- Restrict direct push access to a small maintainer group.
- Enable merge queue when available to avoid last-minute integration races on busy repositories.

Protection rules for module branches should be lighter. They can allow the assigned module session owner to push freely, because quality is enforced at the `main` gate. If the hosting platform supports branch pattern rules, use a rule like `module/*` for lighter protections and `main` for the hard gate.

## 3. Session & Agent Topology

The recommended topology is three-tier:

1. `main` gate session: one Claude/Codex session attached to `main`, responsible for overall planning, architecture decisions, cross-cutting documentation, PR review, and merge coordination. This session must not write feature implementation code.
2. Module sessions: one Claude/Codex session per module branch, each owning implementation, module-local test authoring, and module-local documentation for exactly one module.
3. Subagents within a module session: the module session splits work by file, component, or tightly related file cluster and runs those subagents in parallel.

Session-to-branch mapping is one-to-one for module work. A session should never write to more than one module branch, because branch hopping destroys auditability and increases accidental cross-module edits. The `main` session can inspect all branches but only commits to `main` for project-level docs, review annotations, release notes, integration harnesses, and merge-adjacent changes such as cross-module tests.

Subagent fan-out rules:

- Divide work by file ownership first, then by coherent component slices if a file is too large.
- Give each subagent an explicit write set, such as `src/auth/token.py` and `tests/auth/test_token.py`.
- Avoid overlapping write sets unless the module session is prepared to serialize integration manually.
- Fan back in through the parent module session, which reviews subagent changes before pushing the module branch.

Concurrency limits should be explicit to avoid agent thrash:

- Repository level: no more than 3 to 7 active module branches per integration window unless CI capacity is unusually high.
- Module level: no more than 2 to 5 subagents writing concurrently within one module branch.
- Shared interfaces: when two modules modify the same public contract, nominate one "interface steward" branch and require the other module branch to consume the contract rather than redefining it independently.

A practical topology chart is:

- `main` session -> plans, reviews, docs, merge decisions
- `module/<name>` session -> implementation, unit tests, module integration tests, `MODULE_PLAN.md`
- file subagents -> parallel edits for files within that module

This pattern resembles code ownership partitions in large monorepos, but with branch boundaries used to keep session state and git history isolated.

## 4. Review & Merge Gate

`main` is the sole acceptance gate. No module branch self-merges. A merge can be performed only by a maintainer or automation identity that represents the `main` gate policy, such as a repository admin, release manager, or GitHub merge queue after policy checks pass.

The review flow should be:

1. Module session opens a PR from `module/<module-name>` into `main`.
2. PR template requires: scope summary, changed interfaces, module doc link, unit test evidence, module integration test evidence, and declared cross-module risks.
3. CODEOWNERS automatically requests reviewers for touched paths.
4. CI runs branch checks: lint, typecheck if applicable, security scan if configured, unit tests, module integration tests, and coverage threshold checks.
5. The `main` gate session performs review-only analysis. It checks architectural fit, contract compatibility, documentation sufficiency, and whether cross-module follow-up is required. It does not fix implementation inside the PR branch; it either requests changes or approves.
6. Once approvals and checks pass, the PR enters merge queue or is merged by the gate maintainer.
7. After merge, `main` runs post-merge cross-module integration and project-level documentation updates if those are configured as separate workflows.

Accept criteria before merge:

- Required CI checks green.
- Required review approvals present.
- CODEOWNERS approval present for affected paths.
- Coverage does not regress below the branch protection threshold.
- Module-local docs updated.
- No unresolved architectural concerns from the `main` gate session.
- No open "cross-module blocker" label on the PR.

Reject or return-for-change criteria:

- The branch writes outside its assigned module without prior approval.
- Unit or module integration tests are missing for behavior changes.
- Public interfaces changed without documented migration notes.
- Cross-module integration risk is identified but not addressed by tests, stubs, or compatibility notes.
- The PR mixes feature work with unrelated refactors that make review ambiguous.

Recommended automation stack:

- GitHub: branch protection rules, CODEOWNERS, required status checks, merge queue, and GitHub Actions.
- GitLab alternative: protected branches, CODEOWNERS approval rules, merge request approvals, and DAG pipelines with `needs`.
- Jenkins alternative: multibranch pipelines plus a protected Git hosting layer such as GitHub or Bitbucket for approval policy.

## 5. Cross-Module Testing Strategy

Testing is intentionally split by ownership boundary. Module branches own fast feedback for local correctness. `main` owns confidence that separately developed modules still compose into a coherent system.

Test placement rules:

- Unit tests live next to the module implementation on the module branch.
- Module-scoped integration tests also live on the module branch when they validate interactions wholly inside that module's responsibility boundary.
- Cross-module integration tests live on `main` if they validate coordination between two or more modules, shared contracts, or end-to-end user flows.
- End-to-end smoke tests should run on `main` after merge, or on a synthetic integration branch generated by CI if the repository chooses a pre-merge environment.

Recommended pipeline layers:

1. Module branch PR checks:
   - lint and formatting
   - static analysis and type checking
   - unit tests for the module
   - module integration tests for the module
   - optional contract tests for published interfaces
2. `main` pre-merge or merge-queue checks:
   - changed-module integration matrix
   - consumer/provider contract verification for affected modules
   - cross-module regression suite
   - optional ephemeral environment smoke test
3. `main` post-merge checks:
   - full integration suite
   - E2E smoke suite
   - docs validation and release-note generation

To handle cross-module test gaps, use a contract-first rule. If module A changes an interface consumed by module B, module A must either:

- update or add contract tests that encode the new expectation,
- supply a compatibility shim and document the deprecation path, or
- coordinate a paired merge window where both branches are reviewed together by the `main` gate.

For monorepos, use tooling that can map changed files to affected test scopes. Concrete options include Bazel test targets, Nx affected commands, or Turborepo task graphs. Those tools are not required for this architecture, but they significantly reduce the cost of running the `main` gate by narrowing cross-module checks to impacted dependency paths.

The default policy should be conservative: if affected-module detection is uncertain, run the broader suite. False positives cost time; false negatives ship broken integrations.

## 6. Decision Framework: Candidate Architectures

### Candidate A: Recommended hybrid module-branch model with `main` gate

Approach:
Each functional module gets a dedicated branch from `main` and a dedicated Claude/Codex session. Subagents parallelize work inside each module branch. `main` remains protected, review-centric, and integration-centric, with no feature implementation authored there.

Pros:

- Fits every hard constraint directly, with no reinterpretation.
- Clean ownership boundary: branch, session, tests, and module docs all align.
- Easier auditability because every module change is isolated in one branch and one PR stream.
- Supports heavy parallelism at both module level and file level.
- Makes review quality higher because `main` reviewers evaluate already-cohesive module units instead of mixed partial work.

Cons:

- Long-lived module branches increase merge-drift risk versus pure trunk-based development.
- Cross-module dependencies require explicit coordination and can stall if contracts are unstable.
- CI cost may increase because many module branches run overlapping checks.
- Human process discipline is mandatory; without strict boundaries, module branches can leak into each other.

Fit assessment:
Best fit. It satisfies the branch-per-module requirement while keeping `main` as a pure gate, which is the central architectural goal.

### Candidate B: Pure trunk-based development with feature flags

Approach:
All sessions commit to short-lived branches or directly to `main` with feature flags, branch by abstraction, and very frequent integration as described in trunk-based development.

Pros:

- Minimizes merge pain because branches are extremely short-lived or absent.
- Gives the fastest integration feedback.
- Aligns strongly with continuous integration and continuous delivery practices.
- Reduces stale branch inventory and duplicated CI runs.

Cons:

- Violates the hard constraint that each functional module lives on its own branch from `main`.
- Makes "one session per module branch" impossible as the default operating model.
- Puts more implementation traffic onto `main`, conflicting with the "main does not write implementation code" rule.
- Requires stronger feature-flag discipline and runtime gating maturity.

Fit assessment:
Good near-miss for teams optimizing purely for integration frequency, but not acceptable here because it breaks constraints 1, 2, and 5.

### Candidate C: GitFlow with `develop` as the integration branch

Approach:
Use Vincent Driessen's GitFlow: feature branches merge into `develop`, then release branches merge into `main`. Module branches could exist, but `develop` becomes the practical gate.

Pros:

- Well-known branching pattern with clear release staging.
- Keeps `main` stable for production releases.
- Supports multiple concurrent feature branches and formal release preparation.
- Familiar to teams from enterprise release environments.

Cons:

- Conflicts with the requirement that `main` be the sole review and merge gate.
- Adds an extra integration lane (`develop`) that duplicates responsibility and increases coordination cost.
- Encourages longer-lived branches and deferred integration, which increases merge complexity.
- Project-level docs and cross-module test ownership become split between `develop` and `main`.

Fit assessment:
Structurally close, but not ideal. It introduces a second gate where the requirements explicitly want one gate.

### Candidate D: Single branch or single session monorepo with parallel subagents only

Approach:
Keep one active development branch, perhaps `main` or one broad feature branch, and rely on subagents to divide files in parallel inside the same branch and session context.

Pros:

- Simple git topology with fewer branches to manage.
- Easier whole-repo visibility because all work lands in one place.
- Subagent parallelism still improves throughput on large modules.
- Low branch-administration overhead.

Cons:

- Violates the requirement that each functional module live on its own branch.
- Weak audit boundary between modules; unrelated changes can mix easily.
- Higher risk of subagents colliding on shared files.
- Makes module-scoped accountability and per-module docs harder to enforce.

Fit assessment:
Operationally simpler, but a poor fit for strict module ownership and branch isolation.

### Candidate E: Multi-repo per module with central integration repo

Approach:
Split each module into its own repository, let each session work in a dedicated repo, and integrate via a parent repo, manifest repo, or release train.

Pros:

- Strongest ownership isolation between modules.
- Separate CI pipelines and permissions per module.
- Repository-level autonomy can scale well for independent teams.
- Clear blast-radius containment for secrets, access, and release cadence.

Cons:

- Violates the implied single-repo operating model in this task and complicates "main branch" governance.
- Cross-module integration becomes significantly harder because versioning and dependency publishing become mandatory.
- Architectural changes spanning modules require multi-repo choreography and more tooling.
- Project-level documentation drifts unless aggressively centralized.

Fit assessment:
Useful when modules are independently deployable products, but too heavy for a single multi-module project that wants one repository-level gate.

## 7. Industry References & Rationale

This design is a deliberate hybrid assembled from established practices rather than a copy of one named workflow.

- GitFlow by Vincent Driessen (`A successful Git branching model`, 2010, https://nvie.com/posts/a-successful-git-branching-model/):
  Learned: branches can encode role and lifecycle clearly.
  Adaptation: keep branch naming discipline and explicit lifecycle, but remove `develop` because this project requires `main` to be the sole gate.

- GitHub Flow (`GitHub Flow in the Browser`, GitHub Blog, 2013/2024 update, https://github.blog/developer-skills/github/github-flow-in-the-browser/):
  Learned: small PRs into a protected default branch plus review-centric collaboration scale well.
  Adaptation: keep PR-driven acceptance into `main`, but allow module branches to live longer than GitHub Flow normally prefers because agent-based parallelism is a first-class requirement here.

- Trunk-Based Development by Paul Hammant (https://trunkbaseddevelopment.com/):
  Learned: short feedback loops, frequent integration, branch by abstraction, and avoiding long-lived shared branches reduce merge pain.
  Adaptation: although this architecture cannot be pure trunk-based due to hard branch constraints, it borrows trunk discipline by limiting each module branch to one responsibility, syncing regularly from `main`, and using contract tests plus merge gates to shorten integration cycles.

- GitHub CODEOWNERS (https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners):
  Learned: ownership can be encoded directly in repository metadata and enforced during review.
  Adaptation: assign module owners and cross-cutting owners so PRs touching module boundaries automatically request the right reviewers on `main`.

- GitHub protected branches and required status checks (https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches and https://docs.github.com/articles/about-statuses):
  Learned: a branch can be turned into a policy gate requiring reviews, checks, linear history, merge queue, and restricted pushes.
  Adaptation: `main` becomes the only strict protected branch; module branches stay lighter so implementation can move quickly without weakening the acceptance bar.

- GitHub merge queue (https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request-with-a-merge-queue?tool=webui):
  Learned: high-traffic protected branches benefit from queue-based merges that retest changes against fresh branch state automatically.
  Adaptation: enable merge queue when multiple module PRs compete for `main`, especially if cross-module integration tests are expensive.

- GitHub Actions reusable workflows (https://docs.github.com/en/actions/concepts/workflows-and-actions/reusable-workflows):
  Learned: repeated CI policies should be standardized centrally, then called from multiple workflows.
  Adaptation: encode module PR checks and `main` gate checks as reusable workflows so every module branch gets the same minimum policy.

- GitLab CI DAG pipelines with `needs` (https://docs.gitlab.com/ci/yaml/needs/):
  Learned: fan-out/fan-in dependency graphs allow independent jobs to run in parallel and then converge at an approval point.
  Adaptation: the session topology mirrors this pattern operationally, with subagents fanning out under a module session and converging before the PR is opened.

- Monorepo task-graph tooling such as Bazel, Nx, and Turborepo:
  Bazel build and test concepts (https://bazel.build/):
  Nx affected commands (https://nx.dev/ci/features/affected):
  Turborepo task graph docs (https://turborepo.com/repo/docs/core-concepts/package-and-task-graph):
  Learned: dependency-aware execution lowers CI cost in multi-module repositories.
  Adaptation: use affected-target logic at the `main` gate so cross-module suites run broadly enough to be safe but narrowly enough to remain practical.

The overall rationale is therefore: use GitFlow's explicit branch roles, GitHub Flow's PR-centric review model, trunk-based development's integration discipline, CODEOWNERS and protected branches for governance, and monorepo task graphs for scalable testing. The recommended architecture is not the textbook version of any one of them because the hard constraints are unusually specific to parallel AI-agent development.

## 8. Implementation Roadmap

1. Define module boundaries and branch names.

   - List each functional module and its canonical branch name `module/<module-name>`.
   - Identify cross-cutting directories that remain under `main` gate ownership.
   - Branch creation is now automated via `tree-pipeline start --git-auto`, which runs `git fetch`, `git switch main`, `git pull --ff-only origin main`, `git switch -c module/<module-name>`, and `git push -u origin module/<module-name>` for each module.
   - Module shipping and PR creation: `tree-pipeline module-ship --module <name>`.
   - Main gate merge: `tree-pipeline gate-merge --module <name>` (checks CI, squash merges, deletes branch, refreshes local main).

2. Establish repository governance.

   - Protect `main` with pull-request-only merges, required approvals, required status checks, no force-push, and restricted push access.
   - Add CODEOWNERS entries for each module path and for cross-cutting paths.
   - Choose merge policy: squash merge by default; enable merge queue if PR concurrency is high.
   - Standardize local git behavior for branch hygiene, for example `git config pull.rebase true` or `git config pull.ff only`; choose one team-wide and document it.

3. Standardize session conventions.

   - Reserve one Claude/Codex session per module branch.
   - Reserve one dedicated `main` session for planning, review, docs, and merge coordination only.
   - Define a session naming convention such as `main-gate`, `module-auth`, `module-payments`.

4. Standardize subagent conventions inside module sessions.

   - Require explicit file ownership per subagent before work starts.
   - Cap concurrent subagents per module at a documented limit, such as 3.
   - Require the parent module session to review and reconcile subagent outputs before pushing.

5. Create documentation templates.

   - On `main`: `PROJECT_PLAN.md`, `ARCHITECTURE.md`, `DECISIONS.md`, and `INTEGRATION_TEST_PLAN.md`.
   - On each module branch: `MODULE_PLAN.md`, optional `MODULE_DECISIONS.md`, and a PR checklist section for tests and interface changes.

6. Configure CI for module branches.

   - Add a reusable workflow or pipeline template for lint, static analysis, unit tests, module integration tests, and coverage upload.
   - Trigger it on PRs from `module/*` into `main`.
   - Name jobs uniquely to avoid ambiguous required checks on GitHub.

7. Configure CI for the `main` gate.

   - Add a gate workflow that runs affected cross-module tests on PRs targeting `main`.
   - Add a heavier post-merge workflow that runs the full integration or E2E suite.
   - If available, use merge queue so queued PRs are validated against near-final `main`.

8. Adopt affected-scope test tooling.

   - If the repo is polyglot or large, choose Bazel, Nx, Turborepo, or an equivalent dependency-aware runner.
   - Define how changed files map to module tests and cross-module suites.
   - Default to broader test execution when impact analysis is inconclusive.

9. Define PR and review policy.

   - Require PR templates to include changed module scope, docs updated, tests added, interface changes, and cross-module risks.
   - Require the `main` gate reviewer to either approve, request changes, or mark "needs cross-module test."
   - For shared contracts, require a compatibility note or coordinated merge plan.

10. Define merge and cleanup policy.

- Merge only through the `main` gate.
- Delete module branches after merge unless a follow-up branch is intentionally cut from fresh `main`.
- Record accepted architecture decisions and merged module status in `main` branch documentation.

11. Pilot with 2 to 3 modules before scaling.

- Measure PR lead time, merge-conflict rate, CI duration, and cross-module failure rate.
- Adjust module branch lifetime limits, subagent concurrency, and test breadth before rolling out to all modules.

12. Revisit the workflow quarterly.

- If module branches drift too long, move toward shorter-lived module branches with stronger contract tests.
- If `main` becomes a bottleneck, invest in merge queue, affected-test tooling, and clearer ownership rules rather than relaxing the gate.
