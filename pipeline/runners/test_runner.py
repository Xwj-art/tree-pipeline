"""
Test runner hooks.

This module provides a small abstraction around running unit/integration/CDC
commands defined in configuration. It intentionally does not assume any single
test framework.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of a command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""

        return self.returncode == 0


class TestRunner:
    """Run test commands defined by configuration templates."""

    def __init__(self, *, env: Optional[Dict[str, str]] = None) -> None:
        self.env = env

    def run(
        self,
        *,
        command: str,
        cwd: str,
        variables: Dict[str, str],
        timeout_s: int = 1800,
    ) -> CommandResult:
        """
        Run a configured command.

        Args:
            command: Command template with `{var}` placeholders.
            cwd: Working directory (template supported).
            variables: Template variables.
            timeout_s: Timeout seconds.
        """

        rendered_cmd = command.format(**variables)
        rendered_cwd = cwd.format(**variables)
        try:
            proc = subprocess.run(
                shlex.split(rendered_cmd),
                cwd=rendered_cwd,
                env=self.env,
                text=True,
                capture_output=True,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            return CommandResult(
                returncode=127,
                stdout="",
                stderr=f"Command not found: {shlex.split(rendered_cmd)[0]}. "
                "Install the required tool or update pipeline.config.yaml commands.",
            )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

