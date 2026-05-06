"""
tree-pipeline package.

This package provides a minimal, type-safe implementation of a two-layer
orchestration workflow for parallel module development driven by a DAG and a
versioned contract.
"""

__all__ = ["Orchestrator"]


def __getattr__(name: str):
    if name == "Orchestrator":
        from .orchestrator import Orchestrator

        return Orchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

