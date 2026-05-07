"""
Pipeline orchestrator (three-layer architecture).

This file provides a CLI that:
- generates frozen spec + initial contract
- builds DAG batches
- creates/updates a JSONL task ledger
- builds minimal context packets per module and per file under token budgets
- dispatches file-level sub-tasks to Worker Agents for parallel coding
- runs contract validation and optional test hooks

Three layers: Orchestrator → Module Agent (dispatch) → Worker Agent × N (file-level).
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .context_packet import ContextPacketBuilder, hard_truncate
from .dashboard import DashboardGenerator
from .git_manager import GitManager
from .graph import GraphBatches, build_topological_batches, edges_from_strings
from .module_discovery import discover_modules
from .runners.test_runner import TestRunner
from .task_ledger import TaskEntry, TaskLedger
from .validators.contract_validate import ContractValidationError, load_contract, validate_contract


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).isoformat()


def read_text(path: str) -> str:
    """Read a UTF-8 file as text."""

    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path: str, content: str) -> None:
    """Write a UTF-8 text file, creating parent dirs."""

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def simple_template_render(tpl: str, variables: Dict[str, str]) -> str:
    """Render a template replacing `{{key}}` occurrences."""

    out = tpl
    for k, v in variables.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def load_yaml_or_json(path: str) -> Dict[str, Any]:
    """Load YAML/JSON config with optional PyYAML dependency."""

    raw = read_text(path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "PyYAML is required to parse YAML config. Install pyyaml or provide JSON."
        ) from e
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("Config root must be an object/mapping")
    return data


def deep_get(config: Dict[str, Any], path: str, default: Any) -> Any:
    """Get a dotted-path value from nested dicts."""

    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass(slots=True)
class GitConfig:
    """Resolved git automation config."""

    auto_create_pr: bool
    merge_strategy: str  # "squash" | "merge" | "rebase"
    auto_delete_branch: bool
    require_ci_pass: bool
    base_branch: str


@dataclass(slots=True)
class OrchestratorConfig:
    """Resolved orchestrator config used by the runtime."""

    context_packet_max_tokens: int
    spec_excerpt_max_tokens: int
    contract_excerpt_max_tokens: int
    conventions_max_tokens: int
    lock_excerpt_max_tokens: int
    allow_skip_states: bool
    micro_batch_tests_enabled: bool
    functions_per_batch_min: int
    functions_per_batch_max: int
    coverage_target_min: float
    coverage_target_max: float
    require_scenario_checklist: bool
    require_cdc: bool
    feature_flags_enabled: bool
    feature_flag_prefix: str

    contract_file_name: str
    spec_file_name: str
    conventions_file_name: str
    ledger_file_name: str
    dashboard_file_name: str

    require_same_major: bool
    require_signatures: bool

    commands: Dict[str, Dict[str, str]]
    git: GitConfig


def resolve_config(config: Dict[str, Any]) -> OrchestratorConfig:
    """Resolve OrchestratorConfig from raw config dict."""

    return OrchestratorConfig(
        context_packet_max_tokens=int(
            deep_get(config, "orchestrator.token_budget.context_packet_max_tokens", 2200)
        ),
        spec_excerpt_max_tokens=int(
            deep_get(config, "orchestrator.token_budget.spec_excerpt_max_tokens", 900)
        ),
        contract_excerpt_max_tokens=int(
            deep_get(config, "orchestrator.token_budget.contract_excerpt_max_tokens", 700)
        ),
        conventions_max_tokens=int(
            deep_get(config, "orchestrator.token_budget.conventions_max_tokens", 500)
        ),
        lock_excerpt_max_tokens=int(
            deep_get(config, "orchestrator.token_budget.lock_excerpt_max_tokens", 300)
        ),
        allow_skip_states=bool(deep_get(config, "ledger.allow_skip_states", False)),
        micro_batch_tests_enabled=bool(
            deep_get(config, "orchestrator.micro_batch_tests.enabled", True)
        ),
        functions_per_batch_min=int(
            deep_get(config, "orchestrator.micro_batch_tests.functions_per_batch_min", 2)
        ),
        functions_per_batch_max=int(
            deep_get(config, "orchestrator.micro_batch_tests.functions_per_batch_max", 5)
        ),
        coverage_target_min=float(
            deep_get(
                config,
                "orchestrator.quality_gates.coverage.line_coverage_target_min",
                0.80,
            )
        ),
        coverage_target_max=float(
            deep_get(
                config,
                "orchestrator.quality_gates.coverage.line_coverage_target_max",
                0.90,
            )
        ),
        require_scenario_checklist=bool(
            deep_get(
                config,
                "orchestrator.quality_gates.coverage.require_scenario_checklist",
                True,
            )
        ),
        require_cdc=bool(
            deep_get(config, "orchestrator.quality_gates.coverage.require_cdc", True)
        ),
        feature_flags_enabled=bool(
            deep_get(config, "orchestrator.feature_flags.enabled", True)
        ),
        feature_flag_prefix=str(
            deep_get(config, "orchestrator.feature_flags.naming_prefix", "FF_")
        ),
        contract_file_name=str(deep_get(config, "contract.file_name", "contract.yaml")),
        spec_file_name=str(deep_get(config, "spec.file_name", "spec.md")),
        conventions_file_name=str(deep_get(config, "conventions.file_name", "conventions.md")),
        ledger_file_name=str(deep_get(config, "ledger.file_name", "tasks.jsonl")),
        dashboard_file_name=str(deep_get(config, "ledger.dashboard_file_name", "dashboard.md")),
        require_same_major=bool(
            deep_get(config, "contract.compatibility.require_same_major", True)
        ),
        require_signatures=bool(
            deep_get(config, "contract.compatibility.require_signatures", True)
        ),
        commands=dict(deep_get(config, "commands", {}) or {}),
        git=GitConfig(
            auto_create_pr=bool(deep_get(config, "git.auto_create_pr", False)),
            merge_strategy=str(deep_get(config, "git.merge_strategy", "squash")),
            auto_delete_branch=bool(deep_get(config, "git.auto_delete_branch", True)),
            require_ci_pass=bool(deep_get(config, "git.require_ci_pass", True)),
            base_branch=str(deep_get(config, "git.base_branch", "main")),
        ),
    )


def _normalize_module_specs(
    module_items: Sequence[object],
) -> Tuple[List[str], Dict[str, Dict[str, object]]]:
    """Extract module names plus optional metadata from config-like items."""

    modules: List[str] = []
    meta: Dict[str, Dict[str, object]] = {}
    for item in module_items:
        if isinstance(item, str):
            modules.append(item)
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        modules.append(name)
        paths_raw = item.get("paths", [])
        paths = [str(path) for path in paths_raw] if isinstance(paths_raw, list) else []
        description = str(item.get("description", "")).strip()
        module_meta: Dict[str, object] = {}
        if description:
            module_meta["description"] = description
        if paths:
            module_meta["owned_paths"] = paths
        if module_meta:
            meta[name] = module_meta
    return _parse_modules(modules), meta


def _load_suggestions_payload(path: str) -> Dict[str, Any]:
    """Load and validate a suggestions JSON file."""

    payload = load_yaml_or_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Suggestions payload must be an object")
    candidates = payload.get("module_candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Suggestions payload must contain non-empty module_candidates")
    edges = payload.get("dependency_candidates", [])
    if edges is not None and not isinstance(edges, list):
        raise ValueError("Suggestions payload dependency_candidates must be a list")
    if not bool(payload.get("approved", False)):
        raise ValueError("Suggestions payload must be approved before start can consume it")
    return payload


def _modules_and_edges_from_suggestions(
    payload: Dict[str, Any],
) -> Tuple[List[str], List[Tuple[str, str]], Dict[str, Dict[str, object]]]:
    """Extract modules, dependency edges, and metadata from suggestions."""

    modules: List[str] = []
    meta: Dict[str, Dict[str, object]] = {}
    for item in payload.get("module_candidates", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        modules.append(name)
        module_meta: Dict[str, object] = {}
        for key in ("description", "primary_kind"):
            value = item.get(key)
            if value:
                module_meta[key] = value
        paths_raw = item.get("owned_paths", [])
        if isinstance(paths_raw, list):
            paths = [str(path) for path in paths_raw if str(path).strip()]
            if paths:
                module_meta["owned_paths"] = paths
        if module_meta:
            meta[name] = module_meta

    edges: List[Tuple[str, str]] = []
    for edge in payload.get("dependency_candidates", []):
        if not isinstance(edge, dict):
            continue
        consumer = str(edge.get("consumer", "")).strip()
        provider = str(edge.get("provider", "")).strip()
        if consumer and provider:
            edges.append((consumer, provider))
    return _parse_modules(modules), edges, meta


class Orchestrator:
    """Core orchestration API (used by CLI)."""

    def __init__(self, *, config: OrchestratorConfig) -> None:
        self.config = config

    def build_run_id(self) -> str:
        """Build a new run id."""

        return uuid.uuid4().hex[:10]

    def start(
        self,
        *,
        project_root: str,
        run_dir: str,
        templates_dir: str,
        modules: Sequence[str],
        edges: Sequence[Tuple[str, str]],
        module_metadata: Optional[Dict[str, Dict[str, object]]] = None,
        suggestions_path: Optional[str] = None,
        git_auto: bool = False,
    ) -> None:
        """Initialize a new pipeline run directory with specs, ledger, and packets."""

        os.makedirs(run_dir, exist_ok=True)
        run_id = os.path.basename(os.path.abspath(run_dir)) or self.build_run_id()

        batches = build_topological_batches(list(modules), list(edges))
        prereq_map = self._prerequisites_map(modules, edges)

        self._write_spec_and_conventions(
            run_id=run_id,
            run_dir=run_dir,
            templates_dir=templates_dir,
            modules=modules,
            edges=edges,
            batches=batches,
        )
        self._write_initial_contract(
            run_id=run_id,
            run_dir=run_dir,
            modules=modules,
        )

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        ledger.create_tasks(
            modules=list(modules),
            dependencies=prereq_map,
            task_meta_by_module=module_metadata,
        )

        self._write_context_packets(
            project_root=project_root,
            run_dir=run_dir,
            templates_dir=templates_dir,
            modules=modules,
        )

        self.write_run_manifest(
            run_dir=run_dir,
            project_root=project_root,
            modules=modules,
            edges=edges,
            batches=batches.batches,
            module_metadata=module_metadata or {},
            suggestions_path=suggestions_path,
        )

        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches.batches, output_path=dash_path
        )

        if git_auto:
            self._ensure_module_branches(project_root=project_root, modules=list(modules))

    def resume(self, *, run_dir: str, batches: List[List[str]],
               project_root: Optional[str] = None) -> str:
        """Resume a run: auto-unblock stale blocks, then refresh the dashboard.

        Unlike status (read-only), resume actively scans for blocked tasks
        whose dependencies are now satisfied and clears their block, allowing
        the pipeline to progress without manual intervention.
        """

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        latest = ledger.load_latest()

        auto_unblocked: List[str] = []
        for task in latest.values():
            if task.is_sub_task:
                continue
            if task.meta.get("blocked") and ledger.is_ready(task, latest):
                ledger.unblock_task(task.id)
                auto_unblocked.append(task.id)

        if auto_unblocked:
            print(f"[resume] Auto-unblocked: {', '.join(auto_unblocked)}")

        run_id = os.path.basename(os.path.abspath(run_dir))
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        git_status = self._gather_git_status(run_dir=run_dir, project_root=project_root)
        return DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches, output_path=dash_path,
            git_status_by_module=git_status,
        )

    def status(self, *, run_dir: str, batches: List[List[str]],
               project_root: Optional[str] = None) -> str:
        """Return dashboard markdown for the current run status."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        run_id = os.path.basename(os.path.abspath(run_dir))
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        git_status = self._gather_git_status(run_dir=run_dir, project_root=project_root)
        return DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches, output_path=dash_path,
            git_status_by_module=git_status,
        )

    def validate(self, *, run_dir: str, project_root: str, test_cmd: str | None = None) -> List[str]:
        """Run contract validation and optional test hooks.

        Returns a list of module names that failed validation (empty = all good).
        If test_cmd is provided, runs it, parses results, and auto-updates task states.
        """

        contract_path = os.path.join(os.path.abspath(run_dir), self.config.contract_file_name)
        failed_modules: List[str] = []

        # Internal contract validation (captures structured module-level failures)
        if os.path.exists(contract_path):
            contract = load_contract(contract_path)
            try:
                validate_contract(
                    contract,
                    require_same_major=self.config.require_same_major,
                    require_signatures=self.config.require_signatures,
                )
            except ContractValidationError as e:
                failed_modules = self._parse_failed_modules_from_error(str(e))
                ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
                ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
                for mod in failed_modules:
                    ledger.block_task(mod, str(e))
                raise

        variables = {
            "project_root": os.path.abspath(project_root),
            "run_dir": os.path.abspath(run_dir),
            "contract_path": contract_path,
        }
        runner = TestRunner()
        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)

        unit_summary = self._run_unit_test_hook(
            project_root=project_root,
            run_dir=run_dir,
            runner=runner,
            variables=variables,
            test_cmd=test_cmd,
        )
        self._write_test_summary_to_modules(
            ledger=ledger,
            modules=self._module_scope_for_summary(ledger),
            summary=unit_summary,
        )
        if self._summary_failed(unit_summary):
            for module in self._module_scope_for_summary(ledger):
                ledger.block_task(module, f"Unit tests failed for {module}")
            raise RuntimeError(
                "Unit tests failed:\n"
                + unit_summary.get("stdout_tail", "")
                + "\n"
                + unit_summary.get("stderr_tail", "")
            )

        # User-provided test command (e.g. npx vitest run)
        integ_cmd = self.config.commands.get("integration_tests")
        if integ_cmd:
            res = runner.run(
                command=integ_cmd.get("command", ""),
                cwd=integ_cmd.get("cwd", "{project_root}"),
                variables=variables,
            )
            if not res.ok:
                raise RuntimeError(
                    "Integration tests failed:\n" + res.stdout + "\n" + res.stderr
                )

        cdc_cmd = self.config.commands.get("cdc_tests")
        if cdc_cmd:
            res = runner.run(
                command=cdc_cmd.get("command", ""),
                cwd=cdc_cmd.get("cwd", "{project_root}"),
                variables=variables,
            )
            self._write_test_summary_to_modules(
                ledger=ledger,
                modules=self._module_scope_for_summary(ledger),
                summary={
                    "status": "passed" if res.ok else "failed",
                    "command": cdc_cmd.get("command", ""),
                    "passed": 1 if res.ok else 0,
                    "failed": 0 if res.ok else 1,
                    "coverage_pct": None,
                    "cdc_ok": res.ok,
                    "feature_flags_ok": True,
                    "last_run_at": utc_now_iso(),
                },
            )
            if not res.ok:
                raise RuntimeError("CDC tests failed:\n" + res.stdout + "\n" + res.stderr)

        # Update dashboard after validation
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=os.path.basename(os.path.abspath(run_dir)),
            batches=[],
            output_path=dash_path,
        )

        return failed_modules

    def mark_file_done(
        self,
        *,
        run_dir: str,
        task_id: str,
        project_root: Optional[str] = None,
        test_cmd: Optional[str] = None,
    ) -> str:
        """Mark a file sub-task as done with full auto-advance chain.

        1. Transition file: coding→testing→done
        2. Auto-unlock downstream files (planned→ready)
        3. If all files done, auto-advance module to unit_tests
        4. Refresh dashboard
        """

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=True)
        entry = ledger.update_status(task_id, "done")

        module = task_id.split("::", 1)[0]

        unlocked = ledger.unlock_file_deps(module, task_id)
        if unlocked:
            names = ", ".join(u.id for u in unlocked)
            print(f"[file-done] Auto-unlocked: {names}")

        result = f"File marked done: {entry.id} -> {entry.status}"
        if ledger.all_sub_tasks_done(module):
            repo_root = project_root or self._project_root_for_run(run_dir)
            summary = self._run_unit_test_hook(
                project_root=repo_root,
                run_dir=run_dir,
                runner=TestRunner(),
                variables={
                    "project_root": os.path.abspath(repo_root),
                    "run_dir": os.path.abspath(run_dir),
                    "contract_path": os.path.join(os.path.abspath(run_dir), self.config.contract_file_name),
                },
                test_cmd=test_cmd,
            )
            self._write_test_summary_to_modules(ledger=ledger, modules=[module], summary=summary)
            latest = ledger.load_latest()
            mod_task = latest.get(module)
            if self._summary_failed(summary):
                ledger.block_task(module, f"Unit tests failed for {module}")
                result += f"\nModule {module}: blocked by unit test failure"
            elif mod_task and mod_task.status == "coding":
                if mod_task.meta.get("blocked"):
                    ledger.unblock_task(module)
                ledger.update_status(module, "unit_tests")
                result += (
                    f"\nModule {module}: coding -> unit_tests "
                    f"(all {len(ledger.sub_tasks(module))} files done, tests passed)"
                )

        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=os.path.basename(os.path.abspath(run_dir)),
            batches=[],
            output_path=dash_path,
        )
        return result

    def check_module(self, *, run_dir: str, module: str) -> str:
        """Diagnostic: show per-status file counts for a module."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)

        subs = ledger.sub_tasks(module)
        if not subs:
            return f"Module {module}: no file sub-tasks."

        by_status: Dict[str, List[str]] = {}
        for s in subs:
            by_status.setdefault(s.status, []).append(s.id)

        lines = [f"Module {module} file status:"]
        for st in ["planned", "ready", "coding", "testing", "done", "blocked"]:
            ids = by_status.get(st, [])
            if ids:
                lines.append(f"  {st}: {len(ids)} files")
        return "\n".join(lines)

    def _project_root_for_run(self, run_dir: str) -> str:
        """Resolve project root for an existing run."""

        manifest = self.read_run_manifest(run_dir)
        project_root = str(manifest.get("project_root", "")).strip()
        if project_root:
            return project_root
        return os.getcwd()

    def _module_scope_for_summary(self, ledger: TaskLedger) -> List[str]:
        """Return module ids that should receive run-level test summaries."""

        latest = ledger.load_latest()
        modules = [task.id for task in latest.values() if not task.is_sub_task]
        modules.sort()
        return modules

    def _build_test_summary(
        self,
        *,
        command: str,
        returncode: int,
        stdout: str,
        stderr: str,
        cdc_ok: Optional[bool] = None,
    ) -> Dict[str, object]:
        """Convert command output into a structured test summary."""

        import re

        combined = stdout + "\n" + stderr

        def _extract_count(pattern: str) -> int:
            match = re.search(pattern, combined)
            return int(match.group(1)) if match else 0

        coverage_pct: Optional[float] = None
        coverage_match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", combined)
        if not coverage_match:
            coverage_match = re.search(r"coverage[^0-9]*(\d+)%", combined, re.IGNORECASE)
        if coverage_match:
            coverage_pct = float(coverage_match.group(1)) / 100.0

        passed = _extract_count(r"(\d+)\s+passed")
        failed = _extract_count(r"(\d+)\s+failed")
        errors = _extract_count(r"(\d+)\s+error")
        if returncode == 0 and passed == 0 and failed == 0 and errors == 0:
            passed = 1
        if returncode != 0 and failed == 0 and errors == 0:
            failed = 1

        feature_flags_ok = True
        return {
            "status": "passed" if returncode == 0 else "failed",
            "command": command,
            "passed": passed,
            "failed": failed + errors,
            "coverage_pct": coverage_pct,
            "cdc_ok": True if cdc_ok is None else cdc_ok,
            "feature_flags_ok": feature_flags_ok,
            "last_run_at": utc_now_iso(),
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-1000:],
        }

    def _summary_failed(self, summary: Dict[str, object]) -> bool:
        """Whether a structured summary should block module advancement."""

        if summary.get("status") != "passed":
            return True
        coverage_pct = summary.get("coverage_pct")
        if isinstance(coverage_pct, (int, float)) and coverage_pct < self.config.coverage_target_min:
            return True
        cdc_ok = summary.get("cdc_ok")
        if cdc_ok is False:
            return True
        feature_flags_ok = summary.get("feature_flags_ok")
        if feature_flags_ok is False:
            return True
        return False

    def _run_unit_test_hook(
        self,
        *,
        project_root: str,
        run_dir: str,
        runner: TestRunner,
        variables: Dict[str, str],
        test_cmd: Optional[str],
    ) -> Dict[str, object]:
        """Run the configured unit test command and return a structured summary."""

        unit_cmd = test_cmd
        unit_cwd = "{project_root}"
        if not unit_cmd:
            cfg = self.config.commands.get("unit_tests", {})
            unit_cmd = cfg.get("command")
            unit_cwd = cfg.get("cwd", "{project_root}")

        if not unit_cmd:
            return {
                "status": "failed",
                "command": "",
                "passed": 0,
                "failed": 1,
                "coverage_pct": None,
                "cdc_ok": True,
                "feature_flags_ok": True,
                "last_run_at": utc_now_iso(),
                "stdout_tail": "",
                "stderr_tail": "No unit_tests command configured.",
            }

        print(f"[unit-tests] Running: {unit_cmd}")
        res = runner.run(
            command=unit_cmd,
            cwd=unit_cwd,
            variables=variables,
        )
        print(f"[unit-tests] Exit code: {res.returncode}")
        return self._build_test_summary(
            command=unit_cmd,
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
        )

    def _write_test_summary_to_modules(
        self,
        *,
        ledger: TaskLedger,
        modules: Sequence[str],
        summary: Dict[str, object],
    ) -> None:
        """Persist a structured test summary to module task metadata."""

        for module in modules:
            latest = ledger.load_latest()
            task = latest.get(module)
            if task is None or task.is_sub_task:
                continue
            payload = dict(summary)
            payload.pop("stdout_tail", None)
            payload.pop("stderr_tail", None)
            ledger.update_meta(module, {"test_summary": payload})

    def file_start(self, *, run_dir: str, task_id: str) -> TaskEntry:
        """Claim a file sub-task (ready→coding). Called by Worker Agent."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=True)
        return ledger.update_status(task_id, "coding")

    # ── Run manifest ──────────────────────────────────────────────────

    def run_manifest_path(self, run_dir: str) -> str:
        return os.path.join(run_dir, "run_manifest.json")

    def write_run_manifest(
        self, *, run_dir: str,
        project_root: str,
        modules: Sequence[str], edges: Sequence[Tuple[str, str]],
        batches: List[List[str]],
        module_metadata: Optional[Dict[str, Dict[str, object]]] = None,
        suggestions_path: Optional[str] = None,
    ) -> None:
        import json as _json
        manifest = {
            "project_root": os.path.abspath(project_root),
            "modules": list(modules),
            "edges": [f"{c}:{p}" for c, p in edges],
            "batches": batches,
            "module_metadata": module_metadata or {},
            "suggestions_path": suggestions_path,
            "created_at": utc_now_iso(),
        }
        with open(self.run_manifest_path(run_dir), "w", encoding="utf-8") as f:
            _json.dump(manifest, f, indent=2)

    def read_run_manifest(self, run_dir: str) -> Dict[str, Any]:
        import json as _json
        path = self.run_manifest_path(run_dir)
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)

    def dispatch_files(
        self,
        *,
        run_dir: str,
        module: str,
        plan_path: str,
    ) -> List[TaskEntry]:
        """Dispatch file-level sub-tasks for a module to worker agents.

        Reads a JSON plan file: {files: [{id, title, depends_on}]}.
        Creates sub-tasks in the ledger and per-file context packets.
        """

        plan = load_yaml_or_json(plan_path)
        files_raw = plan.get("files", [])
        if not isinstance(files_raw, list) or not files_raw:
            raise ValueError("Plan must contain a non-empty 'files' list")

        files: List[Dict[str, object]] = []
        for f in files_raw:
            if not isinstance(f, dict):
                raise ValueError(f"Invalid file entry: {f!r}")
            files.append(f)

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)

        # Create sub-tasks
        sub_tasks = ledger.create_sub_tasks(module=module, files=files)

        # Generate per-file context packets with module-specific excerpts
        contract_path = os.path.join(run_dir, self.config.contract_file_name)
        spec_path = os.path.join(run_dir, self.config.spec_file_name)
        full_contract = read_text(contract_path) if os.path.exists(contract_path) else ""
        full_spec = read_text(spec_path) if os.path.exists(spec_path) else ""
        module_contract = self._extract_module_contract(full_contract, module) or full_contract
        module_spec = self._extract_module_spec(full_spec, module) or full_spec

        packets_dir = os.path.join(run_dir, "context_packets")
        os.makedirs(packets_dir, exist_ok=True)
        for f in files:
            file_id = str(f["id"])
            content = (
                f"# File Task: {file_id}\n\n"
                f"## Module: {module}\n\n"
                f"## Description\n{str(f.get('title', file_id))}\n\n"
                "## Contract (Relevant)\n"
                f"{hard_truncate(module_contract, self.config.contract_excerpt_max_tokens)}\n\n"
                "## Spec (Relevant)\n"
                f"{hard_truncate(module_spec, self.config.spec_excerpt_max_tokens)}\n\n"
                "## Worker Instructions\n"
                f"- Implement this file: `{file_id}`\n"
                "- Follow the frozen contract signatures exactly\n"
                f"- Mark task as 'done': `python3 -m pipeline.orchestrator file-done --run-dir <dir> --task-id {module}::{file_id}`\n"
            )
            packet_path = os.path.join(packets_dir, f"{module}__{file_id.replace('/', '_')}.md")
            write_text(packet_path, content)

        # Advance module task to 'coding' if not already there
        latest = ledger.load_latest()
        mod_task = latest.get(module)
        if mod_task:
            if mod_task.status == "planned":
                ledger.update_status(module, "spec_ready")
                ledger.update_status(module, "coding")
            elif mod_task.status == "spec_ready":
                ledger.update_status(module, "coding")

        # Update dashboard
        batches = [[module]]
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=os.path.basename(os.path.abspath(run_dir)),
            batches=batches,
            output_path=dash_path,
        )

        return sub_tasks

    @staticmethod
    def _parse_failed_modules_from_error(error_text: str) -> List[str]:
        """Parse module names from a ContractValidationError message."""
        import re

        modules: List[str] = []
        for m in re.finditer(r"\[(\w+)\]", error_text):
            mod = m.group(1)
            if mod not in modules:
                modules.append(mod)
        return modules

    @staticmethod
    def _parse_vitest_modules(stdout: str) -> set[str]:
        """Parse vitest output to find which modules had passing tests.
        
        Looks for patterns like: ✓ tests/core-engine/ecs.test.ts (12 tests)
        Extracts the module directory name from the test path.
        """
        import re
        
        modules: set[str] = set()
        # Match vitest checkmarks: ✓ tests/module-name/file.test.ts
        for m in re.finditer(r'✓\s+tests/([^/]+)/', stdout):
            modules.add(m.group(1))
        return modules

    # ── Git automation ─────────────────────────────────────────────────

    def _gather_git_status(self, *, run_dir: str,
                           project_root: Optional[str] = None) -> Optional[Dict[str, Dict[str, object]]]:
        """Gather git branch/PR status per module for dashboard enrichment."""

        if not project_root or not os.path.isdir(project_root):
            return None
        try:
            git = self._git(project_root)
            avail = git.ensure_cli_available()
            if not avail.ok:
                return None
            manifest = self.read_run_manifest(run_dir)
            modules = manifest.get("modules", [])
            if not modules:
                return None
            branches = [f"module/{m}" for m in modules]
            statuses = git.aggregate_module_status(branches=branches)
            result: Dict[str, Dict[str, object]] = {}
            for i, module in enumerate(modules):
                if i < len(statuses):
                    result[module] = {
                        "branch": statuses[i].get("branch", "-"),
                        "pr_url": str(statuses[i].get("pr_url", "-") or "-"),
                        "pr_state": statuses[i].get("pr_state", "-"),
                        "mergeable": statuses[i].get("mergeable", "-"),
                    }
            return result
        except Exception:
            return None

    def _git(self, project_root: str) -> GitManager:
        return GitManager(
            repo_root=project_root,
            base_branch=self.config.git.base_branch,
        )

    def _ensure_module_branches(self, *, project_root: str, modules: list[str]) -> None:
        """Create module/<name> branches for all modules."""
        git = self._git(project_root)

        avail = git.ensure_cli_available()
        if not avail.ok:
            raise RuntimeError(avail.message)

        auth = git.ensure_gh_auth()
        if not auth.ok:
            raise RuntimeError(auth.message)

        print(f"[git-auto] Creating branches for {len(modules)} modules on base '{self.config.git.base_branch}'")
        for m in modules:
            branch = f"module/{m}"
            result = git.ensure_module_branch(module=m, branch=branch)
            if not result.ok:
                raise RuntimeError(f"Failed to create branch for {m}: {result.errors}")
            print(f"  {m}: {branch} {'(reused)' if not result.changed else '(created)'}")

    def gate_merge(self, *, run_dir: str, project_root: str, module: str) -> str:
        """Merge a module PR into base branch through the main gate.

        Checks CI pass status before merging.
        """
        git = self._git(project_root)
        branch = f"module/{module}"

        avail = git.ensure_cli_available()
        if not avail.ok:
            return f"ERROR: {avail.message}"

        auth = git.ensure_gh_auth()
        if not auth.ok:
            return f"ERROR: {auth.message}"

        # Ensure we're on base branch
        base_result = git.checkout_base_and_pull()
        if not base_result.ok:
            return f"ERROR: {base_result.errors}"

        # Check merge conflicts
        conflict = git.check_merge_conflicts(branch=branch)
        if not conflict.ok:
            return f"ERROR: Merge blocked — {conflict.errors}\n{conflict.message}"

        # Merge
        result = git.merge_pr(
            pr=branch,
            merge_strategy=self.config.git.merge_strategy,
            delete_branch=self.config.git.auto_delete_branch,
            require_ci_pass=self.config.git.require_ci_pass,
        )
        if not result.ok:
            return f"ERROR: Merge failed: {result.errors}"

        # Update dashboard
        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=True)
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=os.path.basename(os.path.abspath(run_dir)),
            batches=[],
            output_path=dash_path,
        )

        return result.message

    @staticmethod
    def _pr_body(*, ledger: TaskLedger, module: str, run_dir: str) -> str:
        """Generate a PR body from module state."""
        subs = ledger.sub_tasks(module)
        done = sum(1 for s in subs if s.status == "done")
        return (
            f"## Module: {module}\n\n"
            f"- Run dir: `{run_dir}`\n"
            f"- Files: {done}/{len(subs)} done\n"
            f"- Status: ready for review\n"
        )

    def _write_git_meta(self, *, ledger: TaskLedger, module: str, branch: str) -> None:
        """Write git metadata into the module task entry."""
        ledger.update_meta(module, {"git": {"branch": branch, "shipped": True}})

    def _resolve_module_pathspecs(self, *, run_dir: str, module: str) -> List[str]:
        """Resolve owned pathspecs from manifest metadata or dispatched files."""

        manifest = self.read_run_manifest(run_dir)
        module_meta = manifest.get("module_metadata", {})
        if isinstance(module_meta, dict):
            meta = module_meta.get(module, {})
            if isinstance(meta, dict):
                paths_raw = meta.get("owned_paths", [])
                if isinstance(paths_raw, list):
                    paths = [str(path) for path in paths_raw if str(path).strip()]
                    if paths:
                        return list(dict.fromkeys(paths))

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=True)
        files = [
            sub.id.split("::", 1)[1]
            for sub in ledger.sub_tasks(module)
            if "::" in sub.id
        ]
        return list(dict.fromkeys(files))

    def module_ship(self, *, run_dir: str, project_root: str, module: str,
                    commit_message: str = "", pr_title: str = "",
                    pr_body: str = "") -> str:
        """Ship a module with scoped commit paths and safe branch checkout."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=True)

        if not ledger.all_sub_tasks_done(module):
            subs = ledger.sub_tasks(module)
            pending = [s.id for s in subs if s.status != "done"]
            return f"ERROR: Cannot ship {module} — {len(pending)} file(s) not done: {', '.join(pending[:10])}"

        git = self._git(project_root)
        branch = f"module/{module}"

        avail = git.ensure_cli_available()
        if not avail.ok:
            return f"ERROR: {avail.message}"

        auth = git.ensure_gh_auth()
        if not auth.ok:
            return f"ERROR: {auth.message}"

        info = git.branch_info(branch)
        if not info.exists_local:
            return f"ERROR: Branch {branch} does not exist. Run 'start --git-auto' first."

        owned_paths = self._resolve_module_pathspecs(run_dir=run_dir, module=module)
        if not owned_paths:
            return (
                f"ERROR: Cannot ship {module} — no owned paths resolved from config, "
                "suggestions, or dispatched files."
            )

        checkout = git.checkout_branch(branch)
        if not checkout.ok:
            return f"ERROR: {checkout.errors}"

        msg = commit_message or f"feat({module}): implement {module} module"
        commit = git.commit_paths(message=msg, pathspecs=owned_paths)
        print(f"[module-ship] Commit: {commit.message}")

        push = git.push_branch(branch=branch)
        if not push.ok:
            return f"ERROR: Push failed: {push.errors}"
        print(f"[module-ship] Push: {push.message}")

        if self.config.git.auto_create_pr:
            title = pr_title or f"[module:{module}] {module} implementation"
            body = pr_body or self._pr_body(ledger=ledger, module=module, run_dir=run_dir)
            pr = git.ensure_pr(branch=branch, title=title, body=body)
            if not pr.ok:
                return f"ERROR: PR failed: {pr.errors}"
            print(f"[module-ship] PR: {pr.message}")

        self._write_git_meta(ledger=ledger, module=module, branch=branch)
        return f"Module {module} shipped: branch={branch}, pushed={push.changed}"

    def _write_spec_and_conventions(
        self,
        *,
        run_id: str,
        run_dir: str,
        templates_dir: str,
        modules: Sequence[str],
        edges: Sequence[Tuple[str, str]],
        batches: GraphBatches,
    ) -> None:
        spec_tpl = read_text(os.path.join(templates_dir, "spec.md.tpl"))
        conv_tpl = read_text(os.path.join(templates_dir, "conventions.md.tpl"))

        module_breakdown = "\n".join([f"- `{m}`: TBD" for m in modules]) or "- (none)"
        dependency_edges = "\n".join([f"- {c} -> {p}" for c, p in edges]) or "- (none)"
        topo_batches = "\n".join(
            [f"- Batch {i}: {', '.join(b)}" for i, b in enumerate(batches.batches, start=1)]
        ) or "- (none)"

        spec_md = simple_template_render(
            spec_tpl,
            {
                "run_id": run_id,
                "generated_at": utc_now_iso(),
                "module_breakdown": module_breakdown,
                "dependency_edges": dependency_edges,
                "topo_batches": topo_batches,
            },
        )
        write_text(os.path.join(run_dir, self.config.spec_file_name), spec_md)

        conv_md = simple_template_render(
            conv_tpl,
            {
                "run_id": run_id,
                "generated_at": utc_now_iso(),
            },
        )
        write_text(os.path.join(run_dir, self.config.conventions_file_name), conv_md)

    def _write_initial_contract(
        self,
        *,
        run_id: str,
        run_dir: str,
        modules: Sequence[str],
    ) -> None:
        contract_content = (
            "# contract.yaml - generated by tree-pipeline\n\n"
            "version: \"1.0.0\"\n"
            f"generated_at: \"{utc_now_iso()}\"\n\n"
            "modules:\n"
        )
        # Minimal initial contract; users will refine provides/requires.
        for m in modules:
            contract_content += (
                f"  - name: \"{m}\"\n"
                f"    description: \"{m} module\"\n"
                "    provides:\n"
                "      functions: []\n"
                "      http_endpoints: []\n"
                "    requires:\n"
                "      functions: []\n"
                "      http_endpoints: []\n"
            )
        write_text(os.path.join(run_dir, self.config.contract_file_name), contract_content)

    def _write_context_packets(
        self,
        *,
        project_root: str,
        run_dir: str,
        templates_dir: str,
        modules: Sequence[str],
    ) -> None:
        contract_text = read_text(os.path.join(run_dir, self.config.contract_file_name))
        spec_text = read_text(os.path.join(run_dir, self.config.spec_file_name))
        conv_text = read_text(os.path.join(run_dir, self.config.conventions_file_name))

        section_budgets = {
            "contract": self.config.contract_excerpt_max_tokens,
            "spec": self.config.spec_excerpt_max_tokens,
            "conventions": self.config.conventions_max_tokens,
            "lock": self.config.lock_excerpt_max_tokens,
        }
        builder = ContextPacketBuilder(
            max_tokens=self.config.context_packet_max_tokens,
            section_budgets=section_budgets,
        )

        packets_dir = os.path.join(run_dir, "context_packets")
        os.makedirs(packets_dir, exist_ok=True)

        lock_excerpt = self._try_read_lock_excerpt(project_root)

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        latest = ledger.load_latest()

        for module in modules:
            module_contract = self._extract_module_contract(contract_text, module)
            module_spec = self._extract_module_spec(spec_text, module)

            state_snapshot = self._build_module_snapshot(module, latest)
            git_snapshot = f"Branch: module/{module} (pending creation)"

            packet = builder.build(
                module=module,
                contract_excerpt=module_contract or contract_text,
                spec_excerpt=module_spec or spec_text,
                conventions_excerpt=conv_text,
                lock_excerpt=lock_excerpt,
                state_snapshot=state_snapshot,
                git_snapshot=git_snapshot,
            )
            write_text(os.path.join(packets_dir, f"{module}.md"), packet.content)

    @staticmethod
    def _build_module_snapshot(module: str,
                               latest: Dict[str, "TaskEntry"]) -> str:
        """Build a brief state snapshot for a module from ledger data."""

        task = latest.get(module)
        if task is None:
            return f"- Module `{module}` not yet recorded in ledger."
        status = task.status
        blocked = "BLOCKED: " + str(task.meta.get("block_reason", ""))[:60] if task.meta.get("blocked") else ""
        deps = ", ".join(task.depends_on) if task.depends_on else "none"
        lines = [
            f"- Module: `{module}`",
            f"- Status: {status}",
        ]
        if blocked:
            lines.append(f"- Blocked: {blocked}")
        lines.append(f"- Depends on: {deps}")
        return "\n".join(lines)

    @staticmethod
    def _extract_module_contract(contract_text: str, module: str) -> str | None:
        """Extract the section of contract.yaml relevant to a specific module."""
        import re
        # Match from "  - name: module" to the next "  - name:" or end of modules
        pattern = rf'(  - name: "{re.escape(module)}".*?)(?=\n  - name: |\nmodules:|\Z)'
        m = re.search(pattern, contract_text, re.DOTALL)
        return m.group(1) if m else None

    @staticmethod
    def _extract_module_spec(spec_text: str, module: str) -> str | None:
        """Extract the module line from spec.md."""
        import re
        pattern = rf'- `{re.escape(module)}`:.*'
        m = re.search(pattern, spec_text)
        return m.group(0) if m else None

    def _try_read_lock_excerpt(self, project_root: str) -> str:
        candidates = [
            "poetry.lock",
            "uv.lock",
            "requirements.txt",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        ]
        max_chars = self.config.lock_excerpt_max_tokens * 4
        for name in candidates:
            path = os.path.join(project_root, name)
            if os.path.exists(path):
                try:
                    return read_text(path)[:max_chars]
                except Exception:
                    return ""
        return ""

    def _prerequisites_map(
        self, modules: Sequence[str], edges: Sequence[Tuple[str, str]]
    ) -> Dict[str, List[str]]:
        prereq: Dict[str, List[str]] = {m: [] for m in modules}
        for consumer, provider in edges:
            prereq.setdefault(consumer, []).append(provider)
        for k in prereq:
            prereq[k] = sorted(list(set(prereq[k])))
        return prereq


def _parse_modules(values: Sequence[str]) -> List[str]:
    modules: List[str] = []
    for v in values:
        m = v.strip()
        if not m:
            continue
        modules.append(m)
    # de-dup preserve order
    seen = set()
    out: List[str] = []
    for m in modules:
        if m in seen:
            continue
        out.append(m)
        seen.add(m)
    return out


def _infer_batches_from_edges(modules: List[str], edge_strs: List[str]) -> List[List[str]]:
    edges = edges_from_strings(edge_strs)
    return build_topological_batches(modules, edges).batches


def _package_root() -> str:
    """Return the tree-pipeline install root (where templates/ and config live)."""

    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _resolve_templates_dir(explicit: Optional[str]) -> str:
    """Resolve templates directory with fallback. Raises FileNotFoundError if not found."""

    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(_package_root(), "templates"))
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isdir(path):
            return path
    raise FileNotFoundError(
        "templates directory not found. Tried: " + ", ".join(candidates)
    )


def _resolve_config_path(explicit: Optional[str]) -> str:
    """Resolve config path with fallback to package root."""

    if explicit:
        return explicit
    return os.path.join(_package_root(), "pipeline.config.yaml")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""

    p = argparse.ArgumentParser(prog="tree-pipeline")
    p.add_argument("--config", default=None, help="Config path (YAML/JSON)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start a new pipeline run")
    p_start.add_argument("--project-root", required=True)
    p_start.add_argument("--run-dir", required=True)
    p_start.add_argument("--templates-dir", default=None)
    p_start.add_argument("--module", action="append", default=[])
    p_start.add_argument(
        "--suggestions",
        default=None,
        help="Approved suggestions JSON produced by suggest-modules",
    )
    p_start.add_argument(
        "--edge", action="append", default=[],
        help="Dependency edge: DEPENDENT:DEPENDENCY (e.g. room-system:core-engine means room-system depends on core-engine)",
    )
    p_start.add_argument(
        "--git-auto", action="store_true", default=False,
        help="Automatically create and push module/<name> branches for each module",
    )

    p_suggest = sub.add_parser(
        "suggest-modules",
        help="Suggest module candidates, contracts, and dependency edges from a requirement",
    )
    p_suggest.add_argument(
        "--requirement",
        required=True,
        help="Natural-language requirement text for any software domain",
    )
    p_suggest.add_argument(
        "--output",
        required=True,
        help="Path to write structured module suggestions JSON",
    )

    p_status = sub.add_parser("status", help="Render dashboard for an existing run")
    p_status.add_argument("--run-dir", required=True)
    p_status.add_argument("--project-root", default=None, help="Optional: enrich dashboard with git status")
    p_status.add_argument("--module", action="append", default=[], help="Optional: override manifest")
    p_status.add_argument("--edge", action="append", default=[], help="Optional: override manifest")

    p_resume = sub.add_parser("resume", help="Resume a run: auto-unblock + refresh dashboard")
    p_resume.add_argument("--run-dir", required=True)
    p_resume.add_argument("--project-root", default=None, help="Optional: enrich dashboard with git status")
    p_resume.add_argument("--module", action="append", default=[], help="Optional: override manifest")
    p_resume.add_argument("--edge", action="append", default=[], help="Optional: override manifest")

    p_validate = sub.add_parser("validate", help="Validate contract + run test hooks")
    p_validate.add_argument("--run-dir", required=True)
    p_validate.add_argument("--project-root", required=True)
    p_validate.add_argument("--test-cmd", default=None, help="Test command to run and parse results from")

    p_dispatch = sub.add_parser("dispatch", help="Dispatch file-level tasks to worker agents")
    p_dispatch.add_argument("--run-dir", required=True)
    p_dispatch.add_argument("--module", required=True)
    p_dispatch.add_argument("--plan", required=True, help="JSON file describing files to create")

    p_file_start = sub.add_parser("file-start", help="Claim a file sub-task (Worker Agent)")
    p_file_start.add_argument("--run-dir", required=True)
    p_file_start.add_argument("--task-id", required=True, help="Sub-task id (e.g. api::src/models.py)")

    p_file_done = sub.add_parser("file-done", help="Mark a file sub-task as done + auto-advance")
    p_file_done.add_argument("--run-dir", required=True)
    p_file_done.add_argument("--task-id", required=True, help="Sub-task id (e.g. api::src/models.py)")

    p_module_ship = sub.add_parser("module-ship", help="Ship a module: commit, push, and create PR")
    p_module_ship.add_argument("--run-dir", required=True)
    p_module_ship.add_argument("--project-root", required=True)
    p_module_ship.add_argument("--module", required=True)
    p_module_ship.add_argument("--commit-message", default="")
    p_module_ship.add_argument("--pr-title", default="")
    p_module_ship.add_argument("--pr-body-file", default=None, help="PR body template file")

    p_gate_merge = sub.add_parser("gate-merge", help="Merge a module PR through the main gate")
    p_gate_merge.add_argument("--run-dir", required=True)
    p_gate_merge.add_argument("--project-root", required=True)
    p_gate_merge.add_argument("--module", required=True)
    p_gate_merge.add_argument("--pr", default=None, help="PR number (alternative to --module)")

    p_module_check = sub.add_parser("module-check", help="Show per-status file counts for a module")
    p_module_check.add_argument("--run-dir", required=True)
    p_module_check.add_argument("--module", required=True)

    p_next = sub.add_parser("next", help="Show the next ready modules and files")
    p_next.add_argument("--run-dir", required=True)

    args = p.parse_args(argv)

    config_path = _resolve_config_path(args.config)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = load_yaml_or_json(config_path)
    resolved = resolve_config(config)
    orch = Orchestrator(config=resolved)

    if args.cmd == "suggest-modules":
        suggestions = discover_modules(args.requirement)
        write_text(args.output, json.dumps(suggestions, indent=2, ensure_ascii=False) + "\n")
        print(f"Module suggestions written to {args.output}")
        return 0

    if args.cmd == "start":
        modules = _parse_modules(args.module)
        edges = edges_from_strings(args.edge)
        module_metadata: Dict[str, Dict[str, object]] = {}

        if getattr(args, "suggestions", None):
            suggestions = _load_suggestions_payload(args.suggestions)
            modules, edges, module_metadata = _modules_and_edges_from_suggestions(suggestions)

        # Auto-read modules/edges from YAML config if not passed via CLI
        if not modules:
            # Support both: modules as a list, and modules.items as a list
            cfg_modules = deep_get(config, "modules", None)
            if isinstance(cfg_modules, dict):
                cfg_modules = cfg_modules.get("items", cfg_modules)
            if cfg_modules and isinstance(cfg_modules, list):
                modules, module_metadata = _normalize_module_specs(cfg_modules)
        if not edges:
            # Support both: dependencies.edges as a list, and modules[].depends_on
            dep_edges = deep_get(config, "dependencies.edges", None)
            if dep_edges and isinstance(dep_edges, list):
                edges = edges_from_strings([str(e) for e in dep_edges])
            else:
                cfg_modules = deep_get(config, "modules", None)
                if isinstance(cfg_modules, dict):
                    cfg_modules = cfg_modules.get("items", cfg_modules)
                if cfg_modules and isinstance(cfg_modules, list):
                    edge_strs = []
                    for m in cfg_modules:
                        if isinstance(m, dict):
                            name = m.get("name", "")
                            deps = m.get("depends_on", [])
                            if name and isinstance(deps, list):
                                for dep in deps:
                                    edge_strs.append(f"{name}:{dep}")
                    edges = edges_from_strings(edge_strs)

        if not modules:
            raise ValueError(
                "No modules specified. Use --module or define 'modules' in YAML config."
            )

        templates_dir = _resolve_templates_dir(args.templates_dir)
        orch.start(
            project_root=args.project_root,
            run_dir=args.run_dir,
            templates_dir=templates_dir,
            modules=modules,
            edges=edges,
            module_metadata=module_metadata,
            suggestions_path=getattr(args, "suggestions", None),
            git_auto=getattr(args, 'git_auto', False),
        )
        return 0

    def _batches_from_manifest_or_args():
        """Resolve batches from run manifest, falling back to CLI args."""
        manifest = orch.read_run_manifest(args.run_dir)
        if manifest and manifest.get("batches"):
            # use cached batches if no CLI override
            if not args.module and not args.edge:
                return manifest["batches"]
        modules = _parse_modules(getattr(args, 'module', []))
        edge_strs = list(getattr(args, 'edge', []))
        return _infer_batches_from_edges(modules, edge_strs) if modules else []

    if args.cmd == "status":
        batches = _batches_from_manifest_or_args()
        project_root = getattr(args, 'project_root', None)
        print(orch.status(run_dir=args.run_dir, batches=batches, project_root=project_root))
        return 0

    if args.cmd == "resume":
        batches = _batches_from_manifest_or_args()
        project_root = getattr(args, 'project_root', None)
        print(orch.resume(run_dir=args.run_dir, batches=batches,
                          project_root=project_root))
        return 0

    if args.cmd == "validate":
        failed = orch.validate(
            run_dir=args.run_dir,
            project_root=args.project_root,
            test_cmd=getattr(args, 'test_cmd', None),
        )
        if failed:
            print(f"Validation failed for modules: {', '.join(failed)}")
        else:
            print("Validation passed")
        return 0

    if args.cmd == "dispatch":
        orch.dispatch_files(
            run_dir=args.run_dir,
            module=args.module,
            plan_path=args.plan,
        )
        return 0

    if args.cmd == "file-start":
        entry = orch.file_start(run_dir=args.run_dir, task_id=args.task_id)
        print(f"File claimed: {entry.id} -> {entry.status}")
        return 0

    if args.cmd == "file-done":
        result = orch.mark_file_done(
            run_dir=args.run_dir,
            task_id=args.task_id,
        )
        print(result)
        return 0

    if args.cmd == "module-check":
        result = orch.check_module(run_dir=args.run_dir, module=args.module)
        print(result)
        return 0

    if args.cmd == "next":
        ledger_path = os.path.join(args.run_dir, resolved.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=resolved.allow_skip_states)
        latest = ledger.load_latest()

        ready_modules = ledger.ready_tasks()
        print("## Ready Modules\n")
        if ready_modules:
            for t in ready_modules:
                blocked = " **BLOCKED**" if t.meta.get("blocked") else ""
                print(f"- {t.module} (status: {t.status}){blocked}")
        else:
            print("- (none)")

        print("\n## Ready File Workers\n")
        any_files = False
        for task in latest.values():
            if task.is_sub_task:
                continue
            ready_subs = ledger.ready_sub_tasks(task.module)
            if ready_subs:
                any_files = True
                print(f"- **{task.module}**:")
                for s in ready_subs[:5]:
                    print(f"  - {s.title} ({s.id})")
        if not any_files:
            print("- (none)")
        return 0

    if args.cmd == "module-ship":
        pr_body = ""
        if getattr(args, 'pr_body_file', None):
            pr_body = read_text(args.pr_body_file)
        result = orch.module_ship(
            run_dir=args.run_dir,
            project_root=args.project_root,
            module=args.module,
            commit_message=getattr(args, 'commit_message', ""),
            pr_title=getattr(args, 'pr_title', ""),
            pr_body=pr_body,
        )
        print(result)
        return 0 if not result.startswith("ERROR") else 1

    if args.cmd == "gate-merge":
        result = orch.gate_merge(
            run_dir=args.run_dir,
            project_root=args.project_root,
            module=args.module,
        )
        print(result)
        return 0 if not result.startswith("ERROR") else 1

    raise AssertionError(f"Unhandled command: {args.cmd}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
