"""
Context packet builder with hard token budget.

The goal is to distribute the *minimum* context necessary for a module agent to
implement its module safely:
- a subset of the contract relevant to that module
- a spec excerpt: module responsibilities + acceptance criteria
- conventions: testing/rollback/flags rules
- dependency lock excerpt (optional; best-effort)

Token estimation is intentionally simple and deterministic to keep the tool
dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


def estimate_tokens(text: str) -> int:
    """
    Estimate tokens for English/Chinese mixed text.

    This uses a conservative heuristic: 1 token ~= 4 characters.
    """

    return max(1, (len(text) + 3) // 4)


def hard_truncate(text: str, max_tokens: int) -> str:
    """Truncate text to a maximum estimated token count."""

    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 20)].rstrip() + "\n...\n"


def section_excerpt(text: str, max_tokens: int) -> str:
    """Truncate by whole lines first so summaries stay structurally readable."""

    if max_tokens <= 0:
        return ""
    lines = [line.rstrip() for line in text.splitlines()]
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join(kept + [line]).strip()
        if candidate and estimate_tokens(candidate) > max_tokens:
            break
        kept.append(line)
    excerpt = "\n".join(kept).strip()
    if excerpt:
        return excerpt
    return hard_truncate(text, max_tokens)


@dataclass(slots=True)
class ContextPacket:
    """A rendered context packet."""

    module: str
    content: str
    token_estimate: int


class ContextPacketBuilder:
    """Build context packets under a hard token budget."""

    def __init__(
        self,
        *,
        max_tokens: int,
        section_budgets: Dict[str, int],
    ) -> None:
        self.max_tokens = max_tokens
        self.section_budgets = section_budgets

    def build(
        self,
        *,
        module: str,
        contract_excerpt: str,
        spec_excerpt: str,
        conventions_excerpt: str,
        lock_excerpt: str = "",
        state_snapshot: str = "",
        git_snapshot: str = "",
    ) -> ContextPacket:
        """Build a single markdown context packet."""

        contract_excerpt = section_excerpt(
            contract_excerpt, self.section_budgets.get("contract", 0)
        )
        spec_excerpt = section_excerpt(spec_excerpt, self.section_budgets.get("spec", 0))
        conventions_excerpt = section_excerpt(
            conventions_excerpt, self.section_budgets.get("conventions", 0)
        )
        lock_excerpt = section_excerpt(lock_excerpt, self.section_budgets.get("lock", 0))

        content = (
            f"# Context Packet - {module}\n\n"
            "## Goal\n"
            "Implement this module according to the frozen spec and versioned contract.\n\n"
            "## Contract Summary\n"
            f"{contract_excerpt}\n\n"
            "## Spec Summary\n"
            f"{spec_excerpt}\n\n"
            "## Conventions Summary\n"
            f"{conventions_excerpt}\n\n"
            "## State Snapshot\n"
            f"{state_snapshot or '- Module state not yet recorded.'}\n\n"
            "## Git Snapshot\n"
            f"{git_snapshot or '- Git state not yet recorded.'}\n\n"
            "## Dependency Lock Excerpt (Best-effort)\n"
            f"{lock_excerpt}\n\n"
            "## Execution Checklist (Module Agent)\n"
            "- Implement the module with minimal surface area.\n"
            "- Use selective feature flags for risky paths (if enabled).\n"
            "- Do micro-batch unit tests: 2-5 functions per batch.\n"
            "- Update ledger status: coding → unit_tests → module_review.\n"
        )

        token_est = estimate_tokens(content)
        if token_est > self.max_tokens:
            overflow = token_est - self.max_tokens
            content = (
                content
                + "\n"
                + f"> NOTE: Packet exceeds budget by ~{overflow} tokens; "
                "consider reducing excerpts.\n"
            )
            token_est = estimate_tokens(content)

        return ContextPacket(module=module, content=content, token_estimate=token_est)
