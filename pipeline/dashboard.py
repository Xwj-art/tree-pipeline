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
        total = len(latest)
        done = sum(1 for t in latest.values() if t.status == "done")
        in_progress = sum(1 for t in latest.values() if t.status not in ("planned", "done"))
        blocked = sum(
            1
            for t in latest.values()
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

        rows: List[str] = []
        for module in sorted(latest.keys()):
            task = latest[module]
            deps = ", ".join(task.depends_on) if task.depends_on else "-"
            blocked = task.meta.get("blocked")
            if blocked:
                reason = str(task.meta.get("block_reason", "unknown"))[:60]
                blocked_str = f"BLOCKED: {reason}"
            else:
                blocked_str = "-"
            rows.append(
                f"| {task.module} | {task.status} | {deps} | {blocked_str} | {task.updated_at} |"
            )
        task_rows = "\n".join(rows) if rows else "| - | - | - | - | - |"

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

        md = (
            f"# Tree Pipeline Dashboard - {run_id}\n\n"
            f"Generated at: {utc_now_human()}\n\n"
            "## Summary\n\n"
            f"- Total tasks: {stats.total}\n"
            f"- Done: {stats.done}\n"
            f"- In progress: {stats.in_progress}\n"
            f"- Blocked: {stats.blocked}\n\n"
            "## Batches\n\n"
            f"{batches_section}\n\n"
            "## Task Table\n\n"
            "| Module | Status | Depends On | Blocked | Updated |\n"
            "|---|---|---|---|---|\n"
            f"{task_rows}\n\n"
            "## Next Actions\n\n"
            f"{next_actions}\n"
        )

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)
        return md

