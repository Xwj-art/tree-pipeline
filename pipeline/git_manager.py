"""
Git automation layer for tree-pipeline.

Wraps `git` and `gh` CLI via subprocess only — no third-party dependencies.
Every public method is safe to re-run: inspect state first, mutate only if needed.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class BranchInfo:
    name: str
    exists_local: bool
    exists_remote: bool
    current: bool
    tracking: Optional[str]
    base_branch: str


@dataclass(slots=True)
class PullRequestInfo:
    number: Optional[int]
    url: Optional[str]
    state: str  # "OPEN", "MERGED", "CLOSED", "NONE"
    base_ref: Optional[str]
    head_ref: Optional[str]
    mergeable: Optional[str]  # MERGEABLE, CONFLICTING, UNKNOWN


@dataclass(slots=True)
class GitOperationResult:
    changed: bool
    message: str
    branch: Optional[BranchInfo] = None
    pr: Optional[PullRequestInfo] = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _run(cmd: list[str], *, cwd: str, timeout: int = 30) -> CommandResult:
    """Run a subprocess command and return structured result."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        command=cmd,
        returncode=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )


class GitManager:
    """Git and GitHub CLI automation bound to one repository root."""

    def __init__(
        self,
        *,
        repo_root: str,
        base_branch: str = "main",
        remote_name: str = "origin",
    ) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.base_branch = base_branch
        self.remote = remote_name

    # ── Read-only inspections ──────────────────────────────────────────

    def ensure_cli_available(self) -> GitOperationResult:
        """Verify `git` and `gh` are installed and callable."""
        errors: list[str] = []
        for cmd in (["git", "--version"], ["gh", "--version"]):
            r = _run(cmd, cwd=self.repo_root)
            if r.returncode != 0:
                errors.append(f"{cmd[0]} not available: {r.stderr}")
        if errors:
            return GitOperationResult(changed=False, message="CLI check failed", errors=errors)
        return GitOperationResult(changed=False, message="git and gh available")

    def ensure_gh_auth(self) -> GitOperationResult:
        """Verify `gh auth status` succeeds."""
        r = _run(["gh", "auth", "status"], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False,
                message="gh not authenticated",
                errors=[f"gh auth status failed: {r.stderr}"],
            )
        return GitOperationResult(changed=False, message="gh authenticated")

    def ensure_clean_for_checkout(self) -> GitOperationResult:
        """Fail if tracked/untracked changes make branch switches unsafe."""
        r = _run(["git", "status", "--porcelain"], cwd=self.repo_root)
        if r.stdout:
            return GitOperationResult(
                changed=False,
                message="Working tree is dirty",
                errors=["Uncommitted changes present. Commit or stash before switching branches."],
            )
        return GitOperationResult(changed=False, message="Working tree clean")

    def current_branch(self) -> str:
        """Return the current branch name."""
        r = _run(["git", "branch", "--show-current"], cwd=self.repo_root)
        return r.stdout

    def branch_info(self, branch: str) -> BranchInfo:
        """Inspect local/remote branch existence and tracking state."""
        exists_local = (
            _run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=self.repo_root,
            ).returncode
            == 0
        )
        exists_remote = (
            _run(
                ["git", "ls-remote", "--exit-code", "--heads", self.remote, branch],
                cwd=self.repo_root,
            ).returncode
            == 0
        )
        current = self.current_branch() == branch
        tracking: Optional[str] = None
        if exists_local:
            r = _run(
                ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
                cwd=self.repo_root,
            )
            if r.returncode == 0:
                tracking = r.stdout
        return BranchInfo(
            name=branch,
            exists_local=exists_local,
            exists_remote=exists_remote,
            current=current,
            tracking=tracking,
            base_branch=self.base_branch,
        )

    def pr_info(self, *, head: str, base: Optional[str] = None) -> PullRequestInfo:
        """Inspect whether a PR exists for head->base."""
        base = base or self.base_branch
        r = _run(
            [
                "gh", "pr", "list",
                "--state", "open",
                "--head", head,
                "--base", base,
                "--json", "number,url,state,headRefName,baseRefName,mergeable",
            ],
            cwd=self.repo_root,
        )
        if r.returncode != 0:
            return PullRequestInfo(
                number=None, url=None, state="NONE",
                base_ref=None, head_ref=None, mergeable=None,
            )
        try:
            items = _json.loads(r.stdout)
        except _json.JSONDecodeError:
            return PullRequestInfo(
                number=None, url=None, state="NONE",
                base_ref=None, head_ref=None, mergeable=None,
            )
        if not items:
            return PullRequestInfo(
                number=None, url=None, state="NONE",
                base_ref=None, head_ref=None, mergeable=None,
            )
        pr = items[0]
        return PullRequestInfo(
            number=pr.get("number"),
            url=pr.get("url"),
            state=pr.get("state", "OPEN"),
            base_ref=pr.get("baseRefName"),
            head_ref=pr.get("headRefName"),
            mergeable=pr.get("mergeable"),
        )

    def pr_info_by_number(self, pr_num: int) -> PullRequestInfo:
        """Look up a PR by number."""
        r = _run(
            [
                "gh", "pr", "view", str(pr_num),
                "--json", "number,url,state,headRefName,baseRefName,mergeable,statusCheckRollup",
            ],
            cwd=self.repo_root,
        )
        if r.returncode != 0:
            return PullRequestInfo(
                number=None, url=None, state="NONE",
                base_ref=None, head_ref=None, mergeable=None,
            )
        try:
            data = _json.loads(r.stdout)
        except _json.JSONDecodeError:
            return PullRequestInfo(
                number=None, url=None, state="NONE",
                base_ref=None, head_ref=None, mergeable=None,
            )
        return PullRequestInfo(
            number=data.get("number"),
            url=data.get("url"),
            state=data.get("state", "OPEN"),
            base_ref=data.get("baseRefName"),
            head_ref=data.get("headRefName"),
            mergeable=data.get("mergeable"),
        )

    def working_tree_status(self, *, pathspecs: Optional[list[str]] = None) -> GitOperationResult:
        """Return porcelain status for whole repo or scoped paths."""
        cmd = ["git", "status", "--porcelain"]
        if pathspecs:
            cmd.extend(["--"] + pathspecs)
        r = _run(cmd, cwd=self.repo_root)
        return GitOperationResult(changed=bool(r.stdout), message=r.stdout or "clean")

    def has_uncommitted_changes(self, *, pathspecs: Optional[list[str]] = None) -> bool:
        """Check if target scope has tracked or untracked changes."""
        return self.working_tree_status(pathspecs=pathspecs).changed

    # ── Branch lifecycle ───────────────────────────────────────────────

    def checkout_base_and_pull(self, *, base_branch: Optional[str] = None) -> GitOperationResult:
        """Ensure repo is on base branch and fast-forwarded from remote."""
        base = base_branch or self.base_branch

        clean = self.ensure_clean_for_checkout()
        if not clean.ok:
            return clean

        _run(["git", "fetch", self.remote, "--prune"], cwd=self.repo_root)

        current = self.current_branch()
        if current == base:
            r = _run(["git", "pull", "--ff-only", self.remote, base], cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False,
                    message=f"Failed to fast-forward {base}",
                    errors=[r.stderr],
                )
            return GitOperationResult(
                changed="Already" not in r.stdout, message=r.stdout or "up to date"
            )

        r = _run(["git", "switch", base], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message=f"Failed to switch to {base}", errors=[r.stderr]
            )
        r = _run(["git", "pull", "--ff-only", self.remote, base], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False,
                message=f"Failed to fast-forward {base}",
                errors=[r.stderr],
            )
        return GitOperationResult(changed=True, message=f"Switched to {base} and pulled")

    def ensure_module_branch(self, *, module: str, branch: str) -> GitOperationResult:
        """Create or reuse a module branch from base, ensure upstream exists.

        Idempotent: never resets an existing branch.
        """
        info = self.branch_info(branch)

        if info.exists_local and info.exists_remote:
            return GitOperationResult(
                changed=False,
                message=f"Branch {branch} already exists locally and on {self.remote}",
                branch=info,
            )

        if info.exists_local and not info.exists_remote:
            r = _run(["git", "push", "-u", self.remote, branch], cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False, message=f"Failed to push {branch}", errors=[r.stderr]
                )
            info.exists_remote = True
            return GitOperationResult(
                changed=True, message=f"Pushed {branch} to {self.remote}", branch=info
            )

        base_result = self.checkout_base_and_pull()
        if not base_result.ok:
            return base_result

        r = _run(["git", "switch", "-c", branch], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message=f"Failed to create branch {branch}", errors=[r.stderr]
            )
        r = _run(["git", "push", "-u", self.remote, branch], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message=f"Failed to push {branch}", errors=[r.stderr]
            )

        info = self.branch_info(branch)
        return GitOperationResult(
            changed=True, message=f"Created and pushed {branch}", branch=info
        )

    def checkout_branch(self, branch: str) -> GitOperationResult:
        """Switch to a branch safely, reusing the current branch when possible."""

        current = self.current_branch()
        if current == branch:
            return GitOperationResult(changed=False, message=f"Already on {branch}")

        clean = self.ensure_clean_for_checkout()
        if not clean.ok:
            return clean

        r = _run(["git", "switch", branch], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False,
                message=f"Failed to switch to {branch}",
                errors=[r.stderr],
            )
        return GitOperationResult(changed=True, message=f"Switched to {branch}")

    def push_branch(self, *, branch: Optional[str] = None, set_upstream: bool = True) -> GitOperationResult:
        """Push current or named branch to remote."""
        target = branch or self.current_branch()
        info = self.branch_info(target)

        if set_upstream and not info.tracking:
            r = _run(["git", "push", "-u", self.remote, target], cwd=self.repo_root)
        else:
            r = _run(["git", "push"], cwd=self.repo_root)

        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message=f"Push failed for {target}", errors=[r.stderr]
            )
        changed = "Everything up-to-date" not in r.stdout
        return GitOperationResult(changed=changed, message=r.stdout or "pushed")

    def delete_branch(self, *, branch: str, delete_remote: bool = True) -> GitOperationResult:
        """Delete local and optional remote branch when safe."""
        info = self.branch_info(branch)

        if branch == self.base_branch:
            return GitOperationResult(
                changed=False,
                message="Refusing to delete base branch",
                errors=["Cannot delete base branch"],
            )

        if not info.exists_local and not info.exists_remote:
            return GitOperationResult(changed=False, message=f"Branch {branch} already absent")

        if delete_remote and info.exists_remote:
            r = _run(["git", "push", self.remote, "--delete", branch], cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False, message=f"Failed to delete remote {branch}", errors=[r.stderr]
                )

        if info.exists_local:
            r = _run(["git", "branch", "-d", branch], cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False, message=f"Failed to delete local {branch}", errors=[r.stderr]
                )

        return GitOperationResult(changed=True, message=f"Deleted {branch}")

    # ── Commit and PR ──────────────────────────────────────────────────

    def commit_paths(
        self,
        *,
        message: str,
        pathspecs: Optional[list[str]] = None,
    ) -> GitOperationResult:
        """Stage and commit changes only if there is something new."""
        if pathspecs:
            r = _run(["git", "add", "--"] + pathspecs, cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False, message="Stage failed", errors=[r.stderr]
                )

        r = _run(["git", "diff", "--cached", "--quiet"], cwd=self.repo_root)
        if r.returncode == 0:
            return GitOperationResult(changed=False, message="No staged changes to commit")

        r = _run(["git", "commit", "-m", message], cwd=self.repo_root)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message="Commit failed", errors=[r.stderr]
            )
        return GitOperationResult(changed=True, message=r.stdout or "committed")

    def ensure_pr(
        self,
        *,
        branch: str,
        title: str,
        body: str,
        base_branch: Optional[str] = None,
    ) -> GitOperationResult:
        """Create a PR if none exists, otherwise return existing PR."""
        base = base_branch or self.base_branch

        existing = self.pr_info(head=branch, base=base)
        if existing.state == "OPEN":
            return GitOperationResult(
                changed=False,
                message=f"PR #{existing.number} already exists: {existing.url}",
                pr=existing,
            )
        if existing.state == "MERGED":
            return GitOperationResult(
                changed=False,
                message=f"PR #{existing.number} already merged",
                pr=existing,
            )

        r = _run(
            [
                "gh", "pr", "create",
                "--base", base,
                "--head", branch,
                "--title", title,
                "--body", body,
            ],
            cwd=self.repo_root,
        )
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message="PR creation failed", errors=[r.stderr]
            )

        new_pr = self.pr_info(head=branch, base=base)
        return GitOperationResult(
            changed=True,
            message=f"PR created: {new_pr.url}",
            pr=new_pr,
        )

    # ── Merge gate ─────────────────────────────────────────────────────

    def check_merge_conflicts(self, *, branch: str, base_branch: Optional[str] = None) -> GitOperationResult:
        """Detect merge conflicts before merge."""
        base = base_branch or self.base_branch
        _run(["git", "fetch", self.remote, "--prune"], cwd=self.repo_root)

        pr = self.pr_info(head=branch, base=base)
        if pr.mergeable == "CONFLICTING":
            return GitOperationResult(
                changed=False,
                message=f"PR #{pr.number} has merge conflicts",
                pr=pr,
                errors=["Merge conflicts detected"],
            )
        if pr.mergeable == "UNKNOWN":
            return GitOperationResult(
                changed=False,
                message=f"PR #{pr.number} mergeability unknown — CI may still be running",
                pr=pr,
            )
        return GitOperationResult(changed=False, message="No merge conflicts", pr=pr)

    def merge_pr(
        self,
        *,
        pr: str,
        merge_strategy: str = "squash",
        delete_branch: bool = True,
        require_ci_pass: bool = True,
    ) -> GitOperationResult:
        """Merge a PR through gh CLI, then refresh local base branch.

        Idempotent: if already merged, skip and refresh local main.
        """
        pr_info = self._resolve_pr(pr)
        if pr_info is None:
            return GitOperationResult(
                changed=False, message=f"PR not found: {pr}", errors=["PR lookup failed"]
            )
        if pr_info.state == "MERGED":
            return self._refresh_base(f"PR #{pr_info.number} already merged")

        if require_ci_pass and pr_info.mergeable != "MERGEABLE":
            return GitOperationResult(
                changed=False,
                message=f"PR #{pr_info.number} not mergeable (CI may not be green)",
                pr=pr_info,
                errors=["CI checks not passing or mergeability unknown"],
            )

        strategy_flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(
            merge_strategy, "--squash"
        )
        cmd = ["gh", "pr", "merge", str(pr_info.number), strategy_flag]
        if delete_branch:
            cmd.append("--delete-branch")

        r = _run(cmd, cwd=self.repo_root, timeout=120)
        if r.returncode != 0:
            return GitOperationResult(
                changed=False, message=f"Merge failed for PR #{pr_info.number}", errors=[r.stderr]
            )

        return self._refresh_base(r.stdout or f"PR #{pr_info.number} merged")

    def _resolve_pr(self, pr: str) -> Optional[PullRequestInfo]:
        """Resolve a PR spec (number or branch name) to PullRequestInfo."""
        if pr.isdigit():
            return self.pr_info_by_number(int(pr))
        return self.pr_info(head=pr)

    def _refresh_base(self, reason: str) -> GitOperationResult:
        """Switch to base branch and fast-forward from remote."""
        base = self.base_branch
        current = self.current_branch()
        if current != base:
            r = _run(["git", "switch", base], cwd=self.repo_root)
            if r.returncode != 0:
                return GitOperationResult(
                    changed=False,
                    message=f"{reason}. Failed to switch to {base}: {r.stderr}",
                    errors=[r.stderr],
                )
        r = _run(["git", "pull", "--ff-only", self.remote, base], cwd=self.repo_root)
        return GitOperationResult(
            changed="Already" not in r.stdout,
            message=f"{reason}. {r.stdout.strip() or 'base refreshed'}",
        )

    # ── Aggregate status ───────────────────────────────────────────────

    def aggregate_module_status(self, *, branches: list[str]) -> list[dict]:
        """Return branch/PR/mergeability snapshots for status dashboards."""
        results: list[dict] = []
        for branch in branches:
            info = self.branch_info(branch)
            pr = self.pr_info(head=branch)
            results.append({
                "branch": branch,
                "exists_local": info.exists_local,
                "exists_remote": info.exists_remote,
                "current": info.current,
                "tracking": info.tracking,
                "pr_number": pr.number,
                "pr_url": pr.url,
                "pr_state": pr.state,
                "mergeable": pr.mergeable,
            })
        return results
