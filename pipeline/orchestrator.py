"""
Pipeline orchestrator (two-layer architecture).

This file provides a CLI that:
- generates frozen spec + initial contract
- builds DAG batches
- creates/updates a JSONL task ledger
- builds minimal context packets per module under token budgets
- runs contract validation and optional test hooks

The "module agent" is not an executable subprocess here; instead, the
orchestrator produces context packets and ledger updates that a human/Claude
module agent can follow.
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
from .graph import GraphBatches, build_topological_batches, edges_from_strings
from .runners.test_runner import TestRunner
from .task_ledger import TaskLedger
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
class OrchestratorConfig:
    """Resolved orchestrator config used by the runtime."""

    context_packet_max_tokens: int
    spec_excerpt_max_tokens: int
    contract_excerpt_max_tokens: int
    conventions_max_tokens: int
    lock_excerpt_max_tokens: int
    allow_skip_states: bool

    contract_file_name: str
    spec_file_name: str
    conventions_file_name: str
    ledger_file_name: str
    dashboard_file_name: str

    require_same_major: bool
    require_signatures: bool

    commands: Dict[str, Dict[str, str]]


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
    )


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
            templates_dir=templates_dir,
            modules=modules,
        )

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        ledger.create_tasks(modules=list(modules), dependencies=prereq_map)

        self._write_context_packets(
            project_root=project_root,
            run_dir=run_dir,
            templates_dir=templates_dir,
            modules=modules,
        )

        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches.batches, output_path=dash_path
        )

    def resume(self, *, run_dir: str, batches: List[List[str]]) -> None:
        """Regenerate dashboard for an existing run (safe resume)."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        run_id = os.path.basename(os.path.abspath(run_dir))
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches, output_path=dash_path
        )

    def status(self, *, run_dir: str, batches: List[List[str]]) -> str:
        """Return dashboard markdown for the current run status."""

        ledger_path = os.path.join(run_dir, self.config.ledger_file_name)
        ledger = TaskLedger(ledger_path, allow_skip_states=self.config.allow_skip_states)
        run_id = os.path.basename(os.path.abspath(run_dir))
        dash_path = os.path.join(run_dir, self.config.dashboard_file_name)
        return DashboardGenerator(ledger).render(
            run_id=run_id, batches=batches, output_path=dash_path
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

        # User-provided test command (e.g. npx vitest run)
        if test_cmd:
            print(f"[validate] Running: {test_cmd}")
            res = runner.run(
                command=test_cmd,
                cwd=os.path.abspath(project_root),
                variables=variables,
            )
            print(f"[validate] Exit code: {res.returncode}")

            # Auto-update task states from test results
            passed_modules = self._parse_vitest_modules(res.stdout)
            latest = ledger.load_latest()
            from .task_ledger import STATUS_ORDER
            for mod in latest.values():
                if mod.is_sub_task:
                    continue
                if mod.module in passed_modules and mod.status != "done":
                    current_idx = STATUS_ORDER.index(mod.status)
                    for target in STATUS_ORDER[current_idx + 1:]:
                        try:
                            ledger.update_status(mod.id, target)
                            print(f"[validate] {mod.module}: {mod.status} -> {target}")
                        except ValueError:
                            continue

            if not res.ok:
                raise RuntimeError(
                    "Tests failed:\n" + res.stdout[-2000:] + "\n" + res.stderr[-1000:]
                )

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

        # Generate per-file context packets
        contract_path = os.path.join(run_dir, self.config.contract_file_name)
        spec_path = os.path.join(run_dir, self.config.spec_file_name)
        contract_text = read_text(contract_path) if os.path.exists(contract_path) else ""
        spec_text = read_text(spec_path) if os.path.exists(spec_path) else ""

        packets_dir = os.path.join(run_dir, "context_packets")
        os.makedirs(packets_dir, exist_ok=True)
        for f in files:
            file_id = str(f["id"])
            content = (
                f"# File Task: {file_id}\n\n"
                f"## Module: {module}\n\n"
                f"## Description\n{str(f.get('title', file_id))}\n\n"
                "## Contract (Relevant)\n"
                f"{hard_truncate(contract_text, self.config.contract_excerpt_max_tokens)}\n\n"
                "## Spec (Relevant)\n"
                f"{hard_truncate(spec_text, self.config.spec_excerpt_max_tokens)}\n\n"
                "## Worker Instructions\n"
                f"- Implement this file: `{file_id}`\n"
                "- Follow the frozen contract signatures exactly\n"
                "- Mark task as 'done' when implementation passes unit tests\n"
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
        templates_dir: str,
        modules: Sequence[str],
    ) -> None:
        tpl = read_text(os.path.join(templates_dir, "contract.yaml.tpl"))
        rendered_modules: List[str] = []
        for m in modules:
            rendered_modules.append(
                simple_template_render(
                    tpl,
                    {
                        "contract_version": "1.0.0",
                        "generated_at": utc_now_iso(),
                        "module_name": m,
                        "module_description": f"{m} module",
                    },
                ).strip()
            )
        contract_content = (
            "# contract.yaml - generated by tree-pipeline\n\n"
            "version: \"1.0.0\"\n"
            f"generated_at: \"{utc_now_iso()}\"\n\n"
            "modules:\n"
        )
        # Keep a minimal initial contract; users will refine provides/requires.
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
        for module in modules:
            # Extract module-specific contract section instead of full dump
            module_contract = self._extract_module_contract(contract_text, module)
            module_spec = self._extract_module_spec(spec_text, module)
            packet = builder.build(
                module=module,
                contract_excerpt=module_contract or contract_text,
                spec_excerpt=module_spec or spec_text,
                conventions_excerpt=conv_text,
                lock_excerpt=lock_excerpt,
            )
            write_text(os.path.join(packets_dir, f"{module}.md"), packet.content)

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
        for name in candidates:
            path = os.path.join(project_root, name)
            if os.path.exists(path):
                try:
                    return read_text(path)[:4000]
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
        "--edge", action="append", default=[],
        help="Dependency edge: DEPENDENT:DEPENDENCY (e.g. room-system:core-engine means room-system depends on core-engine)",
    )

    p_status = sub.add_parser("status", help="Render dashboard for an existing run")
    p_status.add_argument("--run-dir", required=True)
    p_status.add_argument("--module", action="append", default=[])
    p_status.add_argument(
        "--edge", action="append", default=[],
        help="Dependency edge: DEPENDENT:DEPENDENCY",
    )

    p_resume = sub.add_parser("resume", help="Alias for status (safe resume)")
    p_resume.add_argument("--run-dir", required=True)
    p_resume.add_argument("--module", action="append", default=[])
    p_resume.add_argument(
        "--edge", action="append", default=[],
        help="Dependency edge: DEPENDENT:DEPENDENCY",
    )

    p_validate = sub.add_parser("validate", help="Validate contract + run test hooks")
    p_validate.add_argument("--run-dir", required=True)
    p_validate.add_argument("--project-root", required=True)
    p_validate.add_argument("--test-cmd", default=None, help="Test command to run and parse results from")

    p_dispatch = sub.add_parser("dispatch", help="Dispatch file-level tasks to worker agents")
    p_dispatch.add_argument("--run-dir", required=True)
    p_dispatch.add_argument("--module", required=True)
    p_dispatch.add_argument("--plan", required=True, help="JSON file describing files to create")

    args = p.parse_args(argv)

    config_path = _resolve_config_path(args.config)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = load_yaml_or_json(config_path)
    resolved = resolve_config(config)
    orch = Orchestrator(config=resolved)

    if args.cmd == "start":
        modules = _parse_modules(args.module)
        edges = edges_from_strings(args.edge)

        # Auto-read modules/edges from YAML config if not passed via CLI
        if not modules:
            cfg_modules = deep_get(config, "modules", None)
            if cfg_modules and isinstance(cfg_modules, list):
                for m in cfg_modules:
                    if isinstance(m, dict):
                        name = m.get("name", "")
                        if name:
                            modules.append(name)
                    elif isinstance(m, str):
                        modules.append(m)
        if not edges:
            # Read edges from config: each module dict has depends_on list
            cfg_modules = deep_get(config, "modules", None)
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
        )
        return 0

    if args.cmd in ("status", "resume"):
        modules = _parse_modules(args.module)
        edge_strs: List[str] = list(args.edge)
        batches = _infer_batches_from_edges(modules, edge_strs) if modules else []
        print(orch.status(run_dir=args.run_dir, batches=batches))
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

    raise AssertionError(f"Unhandled command: {args.cmd}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

