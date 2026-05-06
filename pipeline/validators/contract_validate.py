"""
Contract validation.

This validator checks:
- required signatures exist in provider modules
- signature strings match exactly (contract-freeze principle)
- version compatibility based on semantic versioning
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


class ContractValidationError(ValueError):
    """Raised when contract validation fails."""


SemVer = Tuple[int, int, int]


def parse_semver(version: str) -> SemVer:
    """Parse a semantic version string into a tuple."""

    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        raise ValueError(f"Invalid semver: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def cmp_semver(a: SemVer, b: SemVer) -> int:
    """Compare two semantic versions."""

    return (a > b) - (a < b)


def parse_constraints(expr: str) -> List[Tuple[str, SemVer]]:
    """
    Parse constraints like '>=1.0.0,<2.0.0' into a list of comparisons.

    Supported operators: >=, <=, >, <, ==.
    """

    parts = [p.strip() for p in expr.split(",") if p.strip()]
    out: List[Tuple[str, SemVer]] = []
    for part in parts:
        m = re.fullmatch(r"(>=|<=|==|>|<)\s*(\d+\.\d+\.\d+)", part)
        if not m:
            raise ValueError(f"Invalid constraint: {part!r}")
        out.append((m.group(1), parse_semver(m.group(2))))
    return out


def satisfies(version: str, constraints: str) -> bool:
    """Check if a version satisfies all constraints."""

    v = parse_semver(version)
    for op, c in parse_constraints(constraints):
        comp = cmp_semver(v, c)
        if op == "==" and comp != 0:
            return False
        if op == ">=" and comp < 0:
            return False
        if op == "<=" and comp > 0:
            return False
        if op == ">" and comp <= 0:
            return False
        if op == "<" and comp >= 0:
            return False
    return True


def load_contract(path: str) -> Dict[str, Any]:
    """
    Load contract from YAML (preferred) or JSON.

    PyYAML is optional but recommended.
    """

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Try JSON first (fast, dependency-free).
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        import yaml  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "PyYAML is required to parse YAML contracts. Install pyyaml or provide JSON."
        ) from e
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("Contract root must be a mapping/object")
    return data


@dataclass(frozen=True, slots=True)
class ProvidedItem:
    """A provided function or endpoint."""

    item_id: str
    signature: str
    since: str


def _iter_function_items(block: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(block, dict):
        return []
    fns = block.get("functions", [])
    if isinstance(fns, list):
        return [x for x in fns if isinstance(x, dict)]
    return []


def index_provides(contract: Mapping[str, Any]) -> Dict[str, ProvidedItem]:
    """Index provided function signatures by id across all modules."""

    modules = contract.get("modules", [])
    if not isinstance(modules, list):
        raise ValueError("contract.modules must be a list")

    index: Dict[str, ProvidedItem] = {}
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        provides = mod.get("provides", {})
        for fn in _iter_function_items(provides):
            item_id = str(fn.get("id", "")).strip()
            signature = str(fn.get("signature", "")).strip()
            since = str(fn.get("since", "1.0.0")).strip()
            if not item_id:
                continue
            if item_id in index:
                raise ContractValidationError(f"Duplicate provided id: {item_id}")
            index[item_id] = ProvidedItem(item_id=item_id, signature=signature, since=since)
    return index


def validate_contract(
    contract: Mapping[str, Any],
    *,
    require_same_major: bool = True,
    require_signatures: bool = True,
) -> None:
    """
    Validate contract consistency across modules.

    Args:
        contract: Parsed contract mapping.
        require_same_major: Fail when consumer constraint allows a different major version.
        require_signatures: Fail when required signature is missing or mismatched.
    """

    contract_version = str(contract.get("version", "1.0.0"))
    contract_major = parse_semver(contract_version)[0]

    provides = index_provides(contract)

    modules = contract.get("modules", [])
    if not isinstance(modules, list):
        raise ValueError("contract.modules must be a list")

    errors: List[str] = []

    for mod in modules:
        if not isinstance(mod, dict):
            continue
        mod_name = str(mod.get("name", "")).strip() or "<unknown>"
        requires = mod.get("requires", {})
        for req in _iter_function_items(requires):
            item_id = str(req.get("id", "")).strip()
            want_sig = str(req.get("signature", "")).strip()
            constraint = str(req.get("constraint", "")).strip()

            if not item_id:
                continue
            if item_id not in provides:
                if require_signatures:
                    errors.append(f"[{mod_name}] requires unknown function id: {item_id}")
                continue

            provided = provides[item_id]
            if require_signatures and want_sig and provided.signature and want_sig != provided.signature:
                errors.append(
                    f"[{mod_name}] signature mismatch for {item_id}: "
                    f"requires '{want_sig}' but provides '{provided.signature}'"
                )

            if constraint:
                try:
                    if not satisfies(provided.since, constraint):
                        errors.append(
                            f"[{mod_name}] version constraint not satisfied for {item_id}: "
                            f"since {provided.since} not in {constraint}"
                        )
                except Exception as e:
                    errors.append(f"[{mod_name}] invalid constraint for {item_id}: {e}")

                if require_same_major:
                    try:
                        since_major = parse_semver(provided.since)[0]
                        if since_major != contract_major:
                            errors.append(
                                f"[{mod_name}] major mismatch for {item_id}: "
                                f"contract major {contract_major} vs since {provided.since}"
                            )
                    except Exception as e:
                        errors.append(f"[{mod_name}] invalid semver for {item_id}: {e}")

    if errors:
        raise ContractValidationError("Contract validation failed:\n- " + "\n- ".join(errors))


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for contract validation."""

    p = argparse.ArgumentParser(description="Validate tree-pipeline contract.yaml")
    p.add_argument("--contract", required=True, help="Path to contract.yaml (or JSON)")
    p.add_argument("--require-same-major", action="store_true", default=False)
    p.add_argument("--require-signatures", action="store_true", default=False)
    args = p.parse_args(argv)

    contract = load_contract(args.contract)
    validate_contract(
        contract,
        require_same_major=args.require_same_major,
        require_signatures=args.require_signatures,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
