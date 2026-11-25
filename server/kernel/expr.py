"""A small, safe expression language shared by templates and state machines.

Expressions are Python syntax, parsed with ``ast`` and interpreted here; nothing
is ever passed to ``eval``. Allowed:

- literals: numbers, strings, True/False/None, lists and dicts of those
- names from the environment, ``a.b`` and ``a["b"]`` lookups into dicts/lists
- arithmetic ``+ - * / // %``, unary ``-`` and ``not``
- comparisons ``== != < <= > >= in``, ``and`` / ``or``, ``x if c else y``
- calls to the functions the caller passes in (none by default)

Anything else (attribute access on objects, lambdas, comprehensions, imports,
calls to unknown functions) is rejected with ``ExprError``.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from functools import lru_cache
from typing import Any


class ExprError(ValueError):
    """An expression is malformed, unsupported, or failed to evaluate."""


_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


@lru_cache(maxsize=4096)
def parse(expr: str) -> ast.AST:
    """Parse once and check every node is allowed; cached by source text."""
    try:
        tree = ast.parse(str(expr).strip(), mode="eval").body
    except SyntaxError as e:
        raise ExprError(f"bad expression {expr!r}: {e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"unsupported expression {expr!r} ({type(node).__name__})")
    return tree


_ALLOWED = (
    ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.Attribute, ast.Subscript, ast.Call, ast.List, ast.Tuple, ast.Dict,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    *_BIN, *_CMP,
)  # fmt: skip


def evaluate(
    expr: str | int | float | bool | None,
    env: dict[str, Any],
    funcs: dict[str, Callable[..., Any]] | None = None,
) -> Any:
    """Evaluate ``expr`` against ``env``. Non-string values are returned as-is."""
    if not isinstance(expr, str):
        return expr
    return _Eval(env, funcs or {}, expr).run(parse(expr))


class _Eval:
    def __init__(self, env: dict[str, Any], funcs: dict[str, Callable], src: str) -> None:
        self.env, self.funcs, self.src = env, funcs, src

    def fail(self, why: str) -> ExprError:
        return ExprError(f"{why} in {self.src!r}")

    def run(self, n: ast.AST) -> Any:
        try:
            return getattr(self, f"_{type(n).__name__}")(n)
        except ExprError:
            raise
        except (TypeError, ValueError, ZeroDivisionError, KeyError, IndexError) as e:
            raise self.fail(f"{type(e).__name__}: {e}") from e

    def _Constant(self, n):
        return n.value

    def _Name(self, n):
        if n.id not in self.env:
            raise self.fail(f"unknown name {n.id!r}")
        return self.env[n.id]

    def _Attribute(self, n):
        return self._lookup(self.run(n.value), n.attr)

    def _Subscript(self, n):
        return self._lookup(self.run(n.value), self.run(n.slice))

    def _lookup(self, base, key):
        if isinstance(base, dict):
            if key not in base:
                raise self.fail(f"no field {key!r}")
            return base[key]
        if isinstance(base, (list, tuple)) and isinstance(key, int):
            return base[key]
        raise self.fail(f"cannot look up {key!r} on {type(base).__name__}")

    def _BinOp(self, n):
        return _BIN[type(n.op)](self.run(n.left), self.run(n.right))

    def _UnaryOp(self, n):
        v = self.run(n.operand)
        if isinstance(n.op, ast.Not):
            return not v
        return -v if isinstance(n.op, ast.USub) else +v

    def _BoolOp(self, n):
        is_and = isinstance(n.op, ast.And)
        v = None
        for part in n.values:
            v = self.run(part)
            if bool(v) != is_and:  # short-circuit
                return v
        return v

    def _Compare(self, n):
        left = self.run(n.left)
        for op, comp in zip(n.ops, n.comparators, strict=True):
            right = self.run(comp)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True

    def _IfExp(self, n):
        return self.run(n.body) if self.run(n.test) else self.run(n.orelse)

    def _Call(self, n):
        name = n.func.id if isinstance(n.func, ast.Name) else None
        if name not in self.funcs or n.keywords:
            raise self.fail("unsupported expression (call)")
        return self.funcs[name](*[self.run(a) for a in n.args])

    def _List(self, n):
        return [self.run(e) for e in n.elts]

    _Tuple = _List

    def _Dict(self, n):
        return {self.run(k): self.run(v) for k, v in zip(n.keys, n.values, strict=True)}
