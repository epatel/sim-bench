"""Model globals: constants and functions that every state machine in a model can use.

The model's ``globals`` is a short script:

    meadow = 2000                      # a constant
    hunt = 0.02 * scale(meadow)        # ...can use earlier constants and functions

    def logistic(n, cap):              # a function: one return expression
        return n * (1 - n / cap)

    def scale(k): return k / 1000      # one-liners too

Like machine scripts, this is parsed into data and interpreted by ``expr`` (no
``eval``). Constants are computed once, from earlier constants, functions and the pure
built-ins (``min``, ``max``, ``abs``, ``round``, ``int``, ``float``, ``len``). A
function sees only its arguments and the constants, not a machine's variables. It may
call other functions (recursion included, up to ``MAX_DEPTH`` deep) and every function
the calling machine has, so ``random()`` in a function draws from that entity's stream.

In a machine, a name is looked up in its variables, then its params, then the globals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Any

from .expr import ExprError, evaluate
from .machine_script import NAME, Line, blocks, check_expr, header, name_list

MAX_DEPTH = 50
PURE = {"min": min, "max": max, "abs": abs, "round": round, "int": int, "float": float,
        "len": len}  # fmt: skip

_CONST = re.compile(rf"^({NAME})\s*=(?!=)\s*(.+)$")
_DEF = re.compile(rf"^def\s+({NAME})\s*\((.*)\)$")


@dataclass(frozen=True)
class Function:
    params: tuple[str, ...]
    body: str  # an expression


@dataclass
class Globals:
    consts: dict[str, Any] = field(default_factory=dict)
    funcs: dict[str, Function] = field(default_factory=dict)

    def bind(self, builtins: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        """``builtins`` plus the model's functions, ready for ``expr.evaluate``."""
        if not self.funcs:
            return builtins
        user = {name: partial(self._call, name, builtins, depth) for name in self.funcs}
        return {**builtins, **user}

    def _call(self, name: str, builtins: dict[str, Any], depth: int, *args: Any) -> Any:
        if depth >= MAX_DEPTH:
            raise ExprError(f"{name}(): calls nested more than {MAX_DEPTH} deep")
        fn = self.funcs[name]
        if len(args) != len(fn.params):
            raise ExprError(f"{name}() takes {len(fn.params)} arguments, got {len(args)}")
        env = {**self.consts, **dict(zip(fn.params, args, strict=True))}
        return evaluate(fn.body, env, self.bind(builtins, depth + 1))


def _function(line: Line, name: str, params: str, body: list[Line]) -> Function:
    if len(body) != 1 or not body[0].text.startswith("return "):
        raise line.fail(f"def {name}: the body is one 'return <expr>' line")
    args = tuple(name_list(line, params, "argument"))
    if len(set(args)) != len(args):
        raise line.fail(f"def {name}: an argument name is repeated")
    return Function(args, check_expr(body[0], body[0].text[len("return ") :]))


def _read(source: str) -> tuple[list[tuple[Line, str, str]], dict[str, Function]]:
    consts, funcs = [], {}
    for line in blocks(source):
        head, body = header(line)
        m = _DEF.match(head)
        if m:
            if m.group(1) in funcs:
                raise line.fail(f"function {m.group(1)!r} is defined twice")
            funcs[m.group(1)] = _function(line, m.group(1), m.group(2), body)
            continue
        m = _CONST.match(line.text)
        if not m or line.body:
            raise line.fail("expected 'name = <expr>' or 'def name(args): return <expr>'")
        consts.append((line, m.group(1), check_expr(line, m.group(2))))
    return consts, funcs


@lru_cache(maxsize=64)
def parse_globals(source: str) -> Globals:
    """Parse and compute a globals script (``ScriptError`` names the line)."""
    consts, funcs = _read(source)
    g = Globals(funcs=funcs)
    for line, name, expr in consts:
        if name in g.consts:
            raise line.fail(f"constant {name!r} is defined twice")
        try:
            g.consts[name] = evaluate(expr, g.consts, g.bind(PURE))
        except (ExprError, TypeError, ZeroDivisionError) as e:
            raise line.fail(f"{name}: {e}") from None
    return g


EMPTY = Globals()
