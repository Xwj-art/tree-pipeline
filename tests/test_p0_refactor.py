from __future__ import annotations

import json
from pathlib import Path

from pipeline.module_discovery import discover_modules
from pipeline.orchestrator import (
    Orchestrator,
    load_yaml_or_json,
    main,
    resolve_config,
)
from pipeline.task_ledger import TaskLedger


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "templates"
CONFIG_PATH = REPO_ROOT / "pipeline.config.yaml"


def build_orchestrator() -> Orchestrator:
    config = resolve_config(load_yaml_or_json(str(CONFIG_PATH)))
    return Orchestrator(config=config)


def test_resolve_config_parses_quality_gates() -> None:
    config = resolve_config(load_yaml_or_json(str(CONFIG_PATH)))

    assert config.micro_batch_tests_enabled is True
    assert config.functions_per_batch_min == 2
    assert config.functions_per_batch_max == 5
    assert config.coverage_target_min == 0.80
    assert config.coverage_target_max == 0.90
    assert config.require_cdc is False
    assert config.feature_flags_enabled is True
    assert config.feature_flag_prefix == "FF_"


def test_discover_modules_includes_structured_handoff_fields() -> None:
    suggestions = discover_modules("build a CLI tool with configuration and diagnostics")

    assert suggestions["approved"] is False
    assert suggestions["edits"] == []
    assert suggestions["decision_log"] == []
    assert suggestions["module_candidates"]
    assert "primary_kind" in suggestions["module_candidates"][0]
    assert "owned_paths" in suggestions["module_candidates"][0]


def test_start_accepts_approved_suggestions_file(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    run_dir = tmp_path / "run"
    project_root.mkdir()

    suggestions = discover_modules("build a CLI tool with configuration and diagnostics")
    suggestions["approved"] = True
    suggestions["decision_log"] = [{"decision": "accept", "rationale": "test"}]
    suggestions_path = tmp_path / "suggestions.json"
    suggestions_path.write_text(
        json.dumps(suggestions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "start",
            "--project-root",
            str(project_root),
            "--run-dir",
            str(run_dir),
            "--templates-dir",
            str(TEMPLATES_DIR),
            "--suggestions",
            str(suggestions_path),
        ]
    )

    assert exit_code == 0
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["suggestions_path"] == str(suggestions_path)
    assert manifest["module_metadata"]

    ledger = TaskLedger(str(run_dir / "tasks.jsonl"))
    latest = ledger.load_latest()
    assert latest
    first_task = next(iter(latest.values()))
    assert "description" in first_task.meta or "primary_kind" in first_task.meta


def test_file_done_blocks_module_on_failed_unit_tests(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    run_dir = tmp_path / "run"
    project_root.mkdir()

    orch = build_orchestrator()
    orch.start(
        project_root=str(project_root),
        run_dir=str(run_dir),
        templates_dir=str(TEMPLATES_DIR),
        modules=["core"],
        edges=[],
    )

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"files": [{"id": "src/core.py", "title": "core impl"}]}),
        encoding="utf-8",
    )
    orch.dispatch_files(run_dir=str(run_dir), module="core", plan_path=str(plan_path))
    orch.mark_file_done(
        run_dir=str(run_dir),
        task_id="core::src/core.py",
        project_root=str(project_root),
        test_cmd='python3 -c "import sys; sys.exit(1)"',
    )

    ledger = TaskLedger(str(run_dir / "tasks.jsonl"), allow_skip_states=True)
    latest = ledger.load_latest()
    module_task = latest["core"]

    assert module_task.status == "coding"
    assert module_task.meta["blocked"] is True
    summary = module_task.meta["test_summary"]
    assert isinstance(summary, dict)
    assert summary["status"] == "failed"


def test_resolve_module_pathspecs_prefers_manifest_owned_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    run_dir = tmp_path / "run"
    project_root.mkdir()

    orch = build_orchestrator()
    orch.start(
        project_root=str(project_root),
        run_dir=str(run_dir),
        templates_dir=str(TEMPLATES_DIR),
        modules=["core"],
        edges=[],
        module_metadata={"core": {"owned_paths": ["src/core", "tests/core"]}},
    )

    assert orch._resolve_module_pathspecs(run_dir=str(run_dir), module="core") == [
        "src/core",
        "tests/core",
    ]
