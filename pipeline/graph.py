"""
Dependency graph utilities.

The graph is represented as a set of modules and dependency edges in the form:
`consumer -> provider` meaning the consumer depends on the provider being ready.

This module can build topological batches (layers) where each batch contains
modules whose dependencies are satisfied by previous batches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple


@dataclass(frozen=True, slots=True)
class GraphBatches:
    """Result of a topological batching run."""

    batches: List[List[str]]
    indegree: Dict[str, int]
    dependents: Dict[str, Set[str]]


class GraphCycleError(ValueError):
    """Raised when a dependency graph contains a cycle."""


def build_topological_batches(
    modules: Sequence[str],
    edges: Sequence[Tuple[str, str]],
) -> GraphBatches:
    """
    Build topological batches from modules and edges.

    Args:
        modules: All module names.
        edges: Dependency edges as tuples (consumer, provider).

    Returns:
        GraphBatches with `batches` ordered from dependency-free to dependent.

    Raises:
        GraphCycleError: If the graph contains a cycle.
        ValueError: If edges reference unknown modules.
    """

    module_set: Set[str] = set(modules)
    unknown = {m for edge in edges for m in edge if m not in module_set}
    if unknown:
        raise ValueError(f"Unknown modules referenced in edges: {sorted(unknown)}")

    indegree: Dict[str, int] = {m: 0 for m in modules}
    dependents: Dict[str, Set[str]] = {m: set() for m in modules}
    prerequisites: Dict[str, Set[str]] = {m: set() for m in modules}

    for consumer, provider in edges:
        if consumer == provider:
            raise ValueError(f"Self-edge is not allowed: {consumer} -> {provider}")
        if consumer not in dependents[provider]:
            dependents[provider].add(consumer)
            prerequisites[consumer].add(provider)
            indegree[consumer] += 1

    remaining: Set[str] = set(modules)
    batches: List[List[str]] = []

    while remaining:
        ready = sorted([m for m in remaining if indegree[m] == 0])
        if not ready:
            cycle_nodes = sorted(remaining)
            raise GraphCycleError(
                "Dependency cycle detected among modules: " + ", ".join(cycle_nodes)
            )

        batches.append(ready)
        for provider in ready:
            remaining.remove(provider)
            for consumer in sorted(dependents[provider]):
                if consumer in remaining:
                    indegree[consumer] -= 1

    return GraphBatches(batches=batches, indegree=indegree, dependents=dependents)


def parse_edge(edge: str) -> Tuple[str, str]:
    """
    Parse an edge in the form 'consumer:provider'.

    Args:
        edge: Edge string.

    Returns:
        A tuple (consumer, provider).
    """

    if ":" not in edge:
        raise ValueError(f"Invalid edge '{edge}', expected consumer:provider")
    consumer, provider = edge.split(":", 1)
    consumer = consumer.strip()
    provider = provider.strip()
    if not consumer or not provider:
        raise ValueError(f"Invalid edge '{edge}', empty endpoint")
    return consumer, provider


def edges_from_strings(edges: Iterable[str]) -> List[Tuple[str, str]]:
    """Convert a list of edge strings into structured tuples."""

    return [parse_edge(e) for e in edges]

