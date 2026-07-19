from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from quickprice.instrument_policy import BUILTIN_OKX_YIELD_SYMBOLS, BUILTIN_PROVIDER_ROUTES

PROVIDER_ROOT = Path(__file__).parents[2] / "src" / "quickprice" / "providers"
CANONICAL_PAIR = re.compile(
    r"(?<![A-Z0-9._-])([A-Z][A-Z0-9._-]*:[A-Z][A-Z0-9._-]*)(?![A-Z0-9._-])",
    re.IGNORECASE,
)
ABSTRACT_EXAMPLES = frozenset({"BASE:QUOTE"})


def test_provider_modules_do_not_embed_instrument_pairs() -> None:
    violations: list[str] = []
    for path in sorted(PROVIDER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for match in CANONICAL_PAIR.finditer(node.value):
                pair = match.group(1)
                if pair.upper() not in ABSTRACT_EXAMPLES:
                    relative = path.relative_to(PROVIDER_ROOT)
                    violations.append(f"{relative}:{node.lineno}: {pair}")

    assert violations == [], (
        "provider modules must receive instrument bindings and policies at runtime; "
        "move these pair literals to the managed instrument policy:\n" + "\n".join(violations)
    )


def test_builtin_provider_policy_is_deeply_immutable() -> None:
    route = next(iter(BUILTIN_PROVIDER_ROUTES.values()))
    yield_policy = next(iter(BUILTIN_OKX_YIELD_SYMBOLS.values()))

    with pytest.raises(TypeError):
        route["quote"] = ()
    with pytest.raises(TypeError):
        yield_policy["method"] = "mutated"
