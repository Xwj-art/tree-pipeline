"""
Dashboard generator.

Reads a JSONL task ledger and produces a human-friendly dashboard.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

from .task_ledger import STATUS_ORDER, TaskEntry, TaskLedger


def utc_now_human() -> str:
    """Return a human-readable UTC timestamp."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


@dataclass(slots=True)
class DashboardStats:
    """Dashboard summary counts."""

    total: int
    done: int
    in_progress: int
    blocked: int


class DashboardGenerator:
    """Generate dashboard markdown from the task ledger."""

    def __init__(self, ledger: TaskLedger) -> None:
        self.ledger = ledger

    def _stats(self, latest: Dict[str, TaskEntry]) -> DashboardStats:
        mod_tasks = [t for t in latest.values() if not t.is_sub_task]
        total = len(mod_tasks)
        done = sum(1 for t in mod_tasks if t.status == "done")
        in_progress = sum(1 for t in mod_tasks if t.status not in ("planned", "done"))
        blocked = sum(
            1
            for t in mod_tasks
            if t.status != "done" and not self.ledger.is_ready(t, latest)
        )
        return DashboardStats(total=total, done=done, in_progress=in_progress, blocked=blocked)

    def render(
        self,
        *,
        run_id: str,
        batches: List[List[str]],
        output_path: str,
    ) -> str:
        """
        Render and write dashboard markdown.

        Args:
            run_id: Run identifier.
            batches: Topological batches.
            output_path: File path to write.

        Returns:
            The written markdown content.
        """

        latest = self.ledger.load_latest()
        stats = self._stats(latest)

        batch_lines: List[str] = []
        for idx, batch in enumerate(batches, start=1):
            batch_lines.append(f"- Batch {idx}: " + ", ".join(batch))
        batches_section = "\n".join(batch_lines) if batch_lines else "- (no batches)"

        # Module-level tasks only (exclude sub-tasks)
        modules = [t for t in latest.values() if not t.is_sub_task]
        modules.sort(key=lambda t: t.module)
        rows: List[str] = []
        for task in modules:
            deps = ", ".join(task.depends_on) if task.depends_on else "-"
            blocked = task.meta.get("blocked")
            if blocked:
                reason = str(task.meta.get("block_reason", "unknown"))[:60]
                blocked_str = f"BLOCKED: {reason}"
            else:
                blocked_str = "-"
            subs = self.ledger.sub_tasks(task.module)
            sub_done = sum(1 for s in subs if s.status == "done")
            sub_info = f" ({sub_done}/{len(subs)} files)" if subs else ""
            rows.append(
                f"| {task.module}{sub_info} | {task.status} | {deps} | {blocked_str} | {task.updated_at} |"
            )
        task_rows = "\n".join(rows) if rows else "| - | - | - | - | - |"

        # File Worker tables
        sub_sections: List[str] = []
        for task in modules:
            subs = self.ledger.sub_tasks(task.module)
            if not subs:
                continue
            sub_rows: List[str] = []
            for s in sorted(subs, key=lambda x: x.id):
                blocked = s.meta.get("blocked")
                blocked_str = "BLOCKED" if blocked else "-"
                sub_rows.append(
                    f"| {s.id} | {s.title} | {s.status} | {blocked_str} |"
                )
            sub_table = (
                f"### {task.module}\n\n"
                "| File | Description | Status | Blocked |\n"
                "|---|---|---|---|\n"
                + "\n".join(sub_rows)
            )
            sub_sections.append(sub_table)
        file_workers_section = "\n\n".join(sub_sections) if sub_sections else "- (no file workers dispatched)"

        # Next actions: module-level
        ready = self.ledger.ready_tasks()
        if ready:
            next_lines: List[str] = []
            for t in ready[:10]:
                blocked_note = ""
                if t.meta.get("blocked"):
                    blocked_note = " **BLOCKED**"
                next_lines.append(f"- Work on `{t.module}` (status: `{t.status}`){blocked_note}")
                criteria = t.meta.get("exit_criteria", {})
                if isinstance(criteria, dict) and t.status in criteria:
                    next_lines.append(f"  - Exit: {criteria[t.status]}")
            next_actions = "\n".join(next_lines)
        else:
            next_actions = "- No ready tasks. Validate dependencies or check for cycles."

        # Next actions: sub-task level
        sub_ready_lines: List[str] = []
        for task in modules:
            ready_subs = self.ledger.ready_sub_tasks(task.module)
            if ready_subs:
                sub_ready_lines.append(f"- **{task.module}** file workers ready:")
                for s in ready_subs[:5]:
                    sub_ready_lines.append(f"  - Dispatch `{s.title}` ({s.id})")
        sub_next = "\n".join(sub_ready_lines) if sub_ready_lines else "- (no file workers ready)"

        md = (
            f"# Tree Pipeline Dashboard - {run_id}\n\n"
            f"Generated at: {utc_now_human()}\n\n"
            "## Summary\n\n"
            f"- Total module tasks: {stats.total}\n"
            f"- Done: {stats.done}\n"
            f"- In progress: {stats.in_progress}\n"
            f"- Blocked: {stats.blocked}\n\n"
            "## Batches\n\n"
            f"{batches_section}\n\n"
            "## Modules\n\n"
            "| Module | Status | Depends On | Blocked | Updated |\n"
            "|---|---|---|---|---|\n"
            f"{task_rows}\n\n"
            "## File Workers\n\n"
            f"{file_workers_section}\n\n"
            "## Next Actions (Modules)\n\n"
            f"{next_actions}\n\n"
            "## Next Actions (File Workers)\n\n"
            f"{sub_next}\n"
        )

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)
        return md

