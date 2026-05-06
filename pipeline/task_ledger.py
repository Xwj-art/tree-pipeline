"""
Task ledger backed by JSON Lines.

The ledger is the single source of truth for pipeline progress, enabling:
- validated state transitions
- checkpointing and resume
- dashboard generation
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Literal, Optional, Sequence

TaskStatus = Literal[
    "planned",
    "spec_ready",
    "coding",
    "unit_tests",
    "module_review",
    "integrated",
    "integration_tests",
    "done",
]

STATUS_ORDER: List[TaskStatus] = [
    "planned",
    "spec_ready",
    "coding",
    "unit_tests",
    "module_review",
    "integrated",
    "integration_tests",
    "done",
]

DEFAULT_EXIT_CRITERIA: Dict[TaskStatus, str] = {
    "planned": "Module registered in tasks.jsonl with dependencies declared",
    "spec_ready": "Spec reviewed, contract frozen with required signatures listed",
    "coding": "Implementation complete, feature flags in place for risky paths",
    "unit_tests": "Micro-batch unit tests pass (2-5 functions per batch, 80%+ line coverage)",
    "module_review": "Code review passed, no CRITICAL or HIGH issues",
    "integrated": "Module integrated with dependencies, no import or interface errors",
    "integration_tests": "Integration tests pass against contract signatures",
    "done": "All gates passed, ready for merge",
}

ALLOWED_TRANSITIONS: Dict[TaskStatus, List[TaskStatus]] = {
    "planned": ["spec_ready"],
    "spec_ready": ["coding"],
    "coding": ["unit_tests"],
    "unit_tests": ["module_review"],
    "module_review": ["integrated"],
    "integrated": ["integration_tests"],
    "integration_tests": ["done"],
    "done": [],
}


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TaskEntry:
    """A single task entry."""

    id: str
    module: str
    title: str
    status: TaskStatus
    depends_on: List[str]
    created_at: str
    updated_at: str
    meta: Dict[str, object]

    def to_json(self) -> str:
        """Serialize task to a JSON string."""

        return json.dumps(
            {
                "id": self.id,
                "module": self.module,
                "title": self.title,
                "status": self.status,
                "depends_on": self.depends_on,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "meta": self.meta,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "TaskEntry":
        """Parse a task entry from a dictionary."""

        status = data.get("status")
        if status not in STATUS_ORDER:
            raise ValueError(f"Invalid task status: {status!r}")

        depends_raw = data.get("depends_on", [])
        depends_on = list(depends_raw) if isinstance(depends_raw, list) else []

        meta_raw = data.get("meta", {})
        meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}

        return TaskEntry(
            id=str(data["id"]),
            module=str(data["module"]),
            title=str(data["title"]),
            status=status,  # type: ignore[assignment]
            depends_on=[str(x) for x in depends_on],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            meta=meta,
        )


class TaskLedger:
    """
    JSONL-backed task ledger with validated state transitions.

    The ledger file contains one JSON object per line (append-only). The latest
    record for a given task id is considered authoritative.
    """

    def __init__(self, path: str, *, allow_skip_states: bool = False) -> None:
        self.path = path
        self.allow_skip_states = allow_skip_states

    def ensure_parent_dir(self) -> None:
        """Ensure the parent directory exists."""

        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)

    def iter_raw_lines(self) -> Iterator[Dict[str, object]]:
        """Yield raw JSON objects from the ledger file."""

        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def load_latest(self) -> Dict[str, TaskEntry]:
        """Load latest entries keyed by task id."""

        latest: Dict[str, TaskEntry] = {}
        for obj in self.iter_raw_lines():
            entry = TaskEntry.from_dict(obj)
            latest[entry.id] = entry
        return latest

    def append(self, entry: TaskEntry) -> None:
        """Append a new entry snapshot to the JSONL file."""

        self.ensure_parent_dir()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

    def create_tasks(
        self,
        *,
        modules: Sequence[str],
        dependencies: Dict[str, List[str]],
        title_template: str = "Implement module {module}",
    ) -> List[TaskEntry]:
        """
        Initialize tasks for all modules.

        Args:
            modules: Module list.
            dependencies: Mapping module -> list of prerequisite modules.
            title_template: Title format.

        Returns:
            Created task entries.
        """

        created: List[TaskEntry] = []
        now = utc_now_iso()
        for module in modules:
            task_id = f"{module}"
            entry = TaskEntry(
                id=task_id,
                module=module,
                title=title_template.format(module=module),
                status="planned",
                depends_on=dependencies.get(module, []),
                created_at=now,
                updated_at=now,
                meta={"exit_criteria": dict(DEFAULT_EXIT_CRITERIA)},
            )
            self.append(entry)
            created.append(entry)
        return created

    def validate_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        """Validate a status transition."""

        if from_status == to_status:
            return
        allowed = ALLOWED_TRANSITIONS[from_status]
        if to_status in allowed:
            return
        if self.allow_skip_states:
            if STATUS_ORDER.index(to_status) > STATUS_ORDER.index(from_status):
                return
        raise ValueError(f"Invalid transition: {from_status} -> {to_status}")

    def update_status(self, task_id: str, new_status: TaskStatus) -> TaskEntry:
        """
        Update a task status, appending a new snapshot.

        Args:
            task_id: Task id (module name by default).
            new_status: New status.

        Returns:
            Updated task entry.
        """

        latest = self.load_latest()
        if task_id not in latest:
            raise KeyError(f"Task id not found: {task_id}")
        current = latest[task_id]
        self.validate_transition(current.status, new_status)
        updated = TaskEntry(
            id=current.id,
            module=current.module,
            title=current.title,
            status=new_status,
            depends_on=current.depends_on,
            created_at=current.created_at,
            updated_at=utc_now_iso(),
            meta=current.meta,
        )
        self.append(updated)
        return updated

    def next_incomplete(self) -> List[TaskEntry]:
        """Return tasks not yet in 'done', sorted by dependency readiness."""

        latest = self.load_latest()
        tasks = list(latest.values())
        tasks.sort(key=lambda t: STATUS_ORDER.index(t.status))
        return [t for t in tasks if t.status != "done"]

    def is_ready(self, task: TaskEntry, latest: Dict[str, TaskEntry]) -> bool:
        """Whether all dependencies are done."""

        for dep in task.depends_on:
            dep_task = latest.get(dep)
            if dep_task is None or dep_task.status != "done":
                return False
        return True

    def block_task(self, task_id: str, reason: str) -> TaskEntry:
        """Mark a task as blocked (adds block metadata without changing status)."""

        latest = self.load_latest()
        if task_id not in latest:
            raise KeyError(f"Task id not found: {task_id}")
        current = latest[task_id]
        now = utc_now_iso()
        updated = TaskEntry(
            id=current.id,
            module=current.module,
            title=current.title,
            status=current.status,
            depends_on=current.depends_on,
            created_at=current.created_at,
            updated_at=now,
            meta={
                **current.meta,
                "blocked": True,
                "block_reason": reason,
                "blocked_at": now,
            },
        )
        self.append(updated)
        return updated

    def unblock_task(self, task_id: str) -> TaskEntry:
        """Remove block metadata from a task."""

        latest = self.load_latest()
        if task_id not in latest:
            raise KeyError(f"Task id not found: {task_id}")
        current = latest[task_id]
        meta = dict(current.meta)
        meta.pop("blocked", None)
        meta.pop("block_reason", None)
        meta.pop("blocked_at", None)
        updated = TaskEntry(
            id=current.id,
            module=current.module,
            title=current.title,
            status=current.status,
            depends_on=current.depends_on,
            created_at=current.created_at,
            updated_at=utc_now_iso(),
            meta=meta,
        )
        self.append(updated)
        return updated

    def ready_tasks(self) -> List[TaskEntry]:
        """Return tasks whose dependencies are satisfied and not done."""

        latest = self.load_latest()
        ready: List[TaskEntry] = []
        for task in latest.values():
            if task.status == "done":
                continue
            if self.is_ready(task, latest):
                ready.append(task)
        ready.sort(key=lambda t: (STATUS_ORDER.index(t.status), t.module))
        return ready

