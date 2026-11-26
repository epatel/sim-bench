"""Machine scripts: state machines written like code, parsed into machine data.

A script is another way to write the ``machine`` param. ``parse_script`` turns it into
the same mapping as the YAML form, so the runtime, its checks and its safety are
unchanged: it is still data, and expressions go through ``expr`` (never ``eval``).
``format_script`` goes the other way, for showing and converting YAML machines.

    inputs in, failure             # ports (defaults: in / out)
    outputs out, rejected
    var failures = 0               # a variable and its starting value (a literal)
    initial closed                 # optional: the first state is the initial one

    on failure: pass               # a handler for every state (a fallback)

    state closed:
      enter:                       # runs on entering the state
        failures = 0
        after window -> tick       # a timer: fires event "tick" after `window`
      on in: send out              # a message on port "in" (one-liners are fine)
      on tick:
        failures = 0
        after window -> tick
      on failure:
        failures += 1
        if failures >= threshold:
          goto open

    state open:
      ...

Statements (one per line; a block is indented under a line ending in ``:``):

    x = expr    x += expr          set a variable (also -= *= /=)
    a, b = e1, e2                  set several from the old values at once
    goto state                     leave this state (exit, then enter); ends the handler
    send port                      forward the current message (or a new, empty one)
    emit port {k: expr} size expr  send a new message (payload and size optional)
    drop reason words              drop the current message
    after expr -> event [keep]     fire `event` later; `keep` survives state changes
    if expr: / elif expr: / else:
    count name                     add 1 to a counter metric
    observe name [= expr]          add a sample to a metric (default: the variable)
    push var    pop var            queue the current message / take the oldest back
    release var -> port            send every queued message, oldest first
    pass                           do nothing

``#`` starts a comment. Errors name the line.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .expr import ExprError, parse


class ScriptError(ValueError):
    pass


# --- lines and blocks ------------------------------------------------------------------


@dataclass
class Line:
    no: int
    text: str
    body: list[Line] = field(default_factory=list)

    def fail(self, why: str) -> ScriptError:
        return ScriptError(f"line {self.no}: {why}")


def _strip_comment(text: str) -> str:
    quote = None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            return text[:i]
    return text


def blocks(source: str) -> list[Line]:
    """Lines nested by indentation (the parent of a block ends with ':')."""
    root = Line(0, "")
    stack: list[tuple[int, Line]] = [(-1, root)]
    for no, raw in enumerate(source.splitlines(), 1):
        text = _strip_comment(raw).rstrip()
        if not text.strip():
            continue
        indent = len(text) - len(text.lstrip())
        while indent <= stack[-1][0]:
            stack.pop()
        line = Line(no, text.strip())
        stack[-1][1].body.append(line)
        stack.append((indent, line))
    return root.body


def _top_level_colon(text: str) -> int:
    """Index of the first ':' outside brackets and strings, or -1."""
    depth, quote = 0, None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            return i
    return -1


def header(line: Line) -> tuple[str, list[Line]]:
    """'on tick: x = 1' -> ('on tick', [x = 1]); a trailing ':' opens the indented body."""
    i = _top_level_colon(line.text)
    if i < 0:
        return line.text, line.body
    head, rest = line.text[:i].strip(), line.text[i + 1 :].strip()
    if rest and line.body:
        raise line.fail("put the statement on the next line when the block has more lines")
    return head, ([Line(line.no, rest)] if rest else line.body)


# --- parsing ----------------------------------------------------------------------------

NAME = r"[A-Za-z_][\w-]*"
_ASSIGN = re.compile(rf"^({NAME}(?:\s*,\s*{NAME})*)\s*([-+*/]?)=(?!=)\s*(.+)$")
_ARROW = re.compile(rf"^(.+?)\s*->\s*({NAME})(\s+keep)?$")


def check_expr(line: Line, text: str) -> str:
    try:
        parse(text)
    except ExprError as e:
        raise line.fail(str(e)) from None
    return text.strip()


def _name(line: Line, text: str, what: str) -> str:
    if not re.fullmatch(NAME, text or ""):
        raise line.fail(f"expected a {what} name, got {text!r}")
    return text


def name_list(line: Line, text: str, what: str) -> list[str]:
    return [_name(line, n.strip(), what) for n in text.split(",") if n.strip()]


def _segments(line: Line, text: str, count: int) -> list[str]:
    """Split 'e1, e2' into ``count`` expression sources (a tuple, parsed by ast)."""
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError as e:
        raise line.fail(f"bad expression {text!r}: {e.msg}") from None
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    if len(parts) != count:
        raise line.fail(f"{count} names but {len(parts)} values")
    return [check_expr(line, ast.get_source_segment(text, p) or "") for p in parts]


def _assignment(line: Line, m: re.Match) -> dict[str, Any]:
    names = [n.strip() for n in m.group(1).split(",")]
    op, rhs = m.group(2), m.group(3)
    if op:
        if len(names) > 1:
            raise line.fail(f"{op}= takes one name")
        return {"set": {names[0]: check_expr(line, f"{names[0]} {op} ({rhs})")}}
    return {"set": dict(zip(names, _segments(line, rhs, len(names)), strict=True))}


def _emit(line: Line, rest: str) -> dict[str, Any]:
    port, _, rest = rest.partition(" ")
    spec: dict[str, Any] = {"port": _name(line, port, "port")}
    rest = rest.strip()
    if rest.startswith("{"):
        end = _closing_brace(line, rest)
        spec["payload"] = _payload(line, rest[: end + 1])
        rest = rest[end + 1 :].strip()
    if rest:
        word, _, size = rest.partition(" ")
        if word != "size" or not size.strip():
            raise line.fail(f"after the payload, expected 'size <expr>', got {rest!r}")
        spec["size"] = check_expr(line, size)
    return {"emit": spec}


def _closing_brace(line: Line, text: str) -> int:
    depth, quote = 0, None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise line.fail("the payload's '{' is never closed")


def _payload(line: Line, text: str) -> dict[str, str]:
    """'{rabbits: rabbits, "id": n + 1}' -> {"rabbits": "rabbits", "id": "n + 1"}."""
    # bare keys are names in Python; quote them so the dict parses as literals
    quoted = re.sub(rf"([{{,]\s*)({NAME})\s*:", r'\1"\2":', text)
    try:
        node = ast.parse(quoted, mode="eval").body
    except SyntaxError as e:
        raise line.fail(f"bad payload {text!r}: {e.msg}") from None
    if not isinstance(node, ast.Dict) or None in node.keys:
        raise line.fail(f"a payload is a {{key: expr}} mapping, got {text!r}")
    out = {}
    for k, v in zip(node.keys, node.values, strict=True):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            raise line.fail(f"payload keys are names, got {ast.unparse(k)!r}")
        out[k.value] = check_expr(line, ast.get_source_segment(quoted, v) or "")
    return out


def _after(line: Line, rest: str) -> dict[str, Any]:
    m = _ARROW.match(rest)
    if not m:
        raise line.fail("expected 'after <expr> -> <event>' (optionally followed by 'keep')")
    out = {"after": check_expr(line, m.group(1)), "event": m.group(2)}
    if m.group(3):
        out["keep"] = True
    return out


def _release(line: Line, rest: str) -> dict[str, Any]:
    m = _ARROW.match(rest)
    if not m or m.group(3):
        raise line.fail("expected 'release <list var> -> <port>'")
    return {"release": {"from": _name(line, m.group(1), "variable"), "to": m.group(2)}}


def _observe(line: Line, rest: str) -> dict[str, Any]:
    name, eq, value = rest.partition("=")
    name = _name(line, name.strip(), "metric")
    return {"observe": {name: check_expr(line, value) if eq else name}}


def _drop(line: Line, rest: str) -> dict[str, Any]:
    reason = rest.strip()
    if _quoted(reason):
        reason = reason[1:-1]
    return {"drop": reason or "dropped"}


def _quoted(text: str) -> bool:
    return len(text) > 1 and text[0] == text[-1] and text[0] in "'\""


_SIMPLE = {
    "goto": lambda line, rest: {"goto": _name(line, rest, "state")},
    "send": lambda line, rest: {"send": _name(line, rest, "port")},
    "count": lambda line, rest: {"count": _name(line, rest, "counter")},
    "push": lambda line, rest: {"push": _name(line, rest, "variable")},
    "pop": lambda line, rest: {"pop": _name(line, rest, "variable")},
    "emit": _emit,
    "after": _after,
    "release": _release,
    "observe": _observe,
    "drop": _drop,
}


def _statement(line: Line) -> dict[str, Any] | None:
    if line.body:
        raise line.fail(
            "unexpected indented block (only if/elif/else, on, enter and exit have one)"
        )
    if line.text == "pass":
        return None
    m = _ASSIGN.match(line.text)
    if m:
        return _assignment(line, m)
    word, _, rest = line.text.partition(" ")
    if word in _SIMPLE:
        return _SIMPLE[word](line, rest.strip())
    raise line.fail(f"don't know how to read {line.text!r}")


def _actions(lines: list[Line]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        word = header(lines[i])[0].split(" ", 1)[0]
        if word == "if":
            action, i = _if_chain(lines, i)
            out.append(action)
            continue
        if word in ("elif", "else"):
            raise lines[i].fail(f"'{word}' without an 'if' before it")
        action = _statement(lines[i])
        if action:
            out.append(action)
        i += 1
    return out


def _if_chain(lines: list[Line], i: int) -> tuple[dict[str, Any], int]:
    """An if with its elif/else siblings; elif becomes a nested if in else."""
    head, body = header(lines[i])
    action = {"if": check_expr(lines[i], head[2:]), "then": _actions(body)}
    i += 1
    if i < len(lines):
        nxt, nbody = header(lines[i])
        if nxt.startswith("elif "):
            lines = [Line(lines[i].no, "if " + nxt[5:], nbody), *lines[i + 1 :]]
            nested, used = _if_chain(lines, 0)
            action["else"] = [nested]
            return action, i + used
        if nxt == "else":
            action["else"] = _actions(nbody)
            i += 1
    return action, i


class _Machine:
    """Collects the top-level declarations of a script."""

    def __init__(self) -> None:
        self.m: dict[str, Any] = {"vars": {}, "on": {}, "states": {}}

    def line(self, line: Line) -> None:
        head, body = header(line)
        word, _, rest = head.partition(" ")
        handler = getattr(self, f"_{word}", None)
        if handler is None:
            raise line.fail(f"expected inputs, outputs, var, initial, on or state; got {word!r}")
        handler(line, rest.strip(), body)

    def _inputs(self, line, rest, body):
        self.m["inputs"] = name_list(line, rest, "port")

    def _outputs(self, line, rest, body):
        self.m["outputs"] = name_list(line, rest, "port")

    def _initial(self, line, rest, body):
        self.m["initial"] = _name(line, rest, "state")

    def _var(self, line, rest, body):
        name, eq, value = rest.partition("=")
        name = _name(line, name.strip(), "variable")
        try:
            self.m["vars"][name] = ast.literal_eval(value.strip()) if eq else 0
        except (ValueError, SyntaxError):
            raise line.fail(
                f"a variable starts as a literal (number, string, list), got {value.strip()!r}"
            ) from None

    def _on(self, line, rest, body):
        _handlers(self.m["on"], line, rest, body)

    def _state(self, line, rest, body):
        name = _name(line, rest, "state")
        if name in self.m["states"]:
            raise line.fail(f"state {name!r} is defined twice")
        self.m["states"][name] = _state_body(body)
        self.m.setdefault("initial", name)


def _handlers(on: dict[str, Any], line: Line, rest: str, body: list[Line]) -> None:
    for event in name_list(line, rest, "event"):
        if event in on:
            raise line.fail(f"'on {event}' is defined twice here")
        on[event] = _actions(body)


def _state_body(lines: list[Line]) -> dict[str, Any]:
    st: dict[str, Any] = {}
    for line in lines:
        head, body = header(line)
        word, _, rest = head.partition(" ")
        if word in ("enter", "exit") and not rest:
            st[word] = _actions(body)
        elif word == "on":
            _handlers(st.setdefault("on", {}), line, rest.strip(), body)
        else:
            raise line.fail(f"inside a state, expected enter:, exit: or on <event>:, got {head!r}")
    return st


@lru_cache(maxsize=256)
def _parse_cached(source: str) -> dict[str, Any]:
    builder = _Machine()
    for line in blocks(source):
        builder.line(line)
    m = builder.m
    if not m["on"]:
        del m["on"]
    return m


def parse_script(source: str) -> dict[str, Any]:
    """A machine script -> the machine mapping (``check_machine`` validates it)."""
    return copy.deepcopy(_parse_cached(source))


# --- formatting -------------------------------------------------------------------------

INDENT = "  "


def format_script(m: dict[str, Any]) -> str:
    """A machine mapping -> script text (``parse_script`` reads it back)."""
    out = _declarations(m)
    states = m.get("states") or {}
    for event, acts in (m.get("on") or {}).items():
        out += ["", *_block(f"on {event}", acts, 0)]
    for name, st in states.items():
        out += ["", *_state_lines(name, st or {})]
    return "\n".join(out).lstrip("\n") + "\n"


def _declarations(m: dict[str, Any]) -> list[str]:
    out = [f"{key} {', '.join(m[key])}" for key, default in (("inputs", "in"), ("outputs", "out"))
           if m.get(key, [default]) != [default]]  # fmt: skip
    out += [f"var {k} = {v!r}" for k, v in (m.get("vars") or {}).items()]
    first = next(iter(m.get("states") or {}), None)
    if m.get("initial") and m["initial"] != first:
        out.append(f"initial {m['initial']}")
    return out


def _state_lines(name: str, st: dict[str, Any]) -> list[str]:
    body: list[str] = []
    for part in ("enter", "exit"):
        if st.get(part):
            body += _block(part, st[part], 1)
    for event, acts in (st.get("on") or {}).items():
        body += _block(f"on {event}", acts, 1)
    return [f"state {name}:", *body] if body else [f"state {name}"]


def _block(head: str, acts: list, depth: int) -> list[str]:
    lines = _action_lines(acts, depth + 1)
    pad = INDENT * depth
    if not lines:
        return [f"{pad}{head}: pass"]
    if len(lines) == 1 and not _opens_block(lines[0]):
        return [f"{pad}{head}: {lines[0].strip()}"]
    return [f"{pad}{head}:", *lines]


def _opens_block(line: str) -> bool:
    return line.rstrip().endswith(":")


def _action_lines(acts: list, depth: int) -> list[str]:
    out: list[str] = []
    for a in acts:
        out += _format_action(a, depth)
    return out


def _format_action(a: dict[str, Any], depth: int) -> list[str]:
    pad = INDENT * depth
    if "if" in a:
        return _if_lines(a, depth, "if")
    verb = next(iter(k for k in a if k in _FORMATTERS))
    return [pad + line for line in _FORMATTERS[verb](a)]


def _if_lines(a: dict[str, Any], depth: int, word: str) -> list[str]:
    pad = INDENT * depth
    lines = [f"{pad}{word} {a['if']}:", *(_action_lines(a.get("then") or [], depth + 1)
                                          or [INDENT * (depth + 1) + "pass"])]  # fmt: skip
    rest = a.get("else") or []
    if len(rest) == 1 and "if" in rest[0]:
        return lines + _if_lines(rest[0], depth, "elif")
    if rest:
        lines += [f"{pad}else:", *_action_lines(rest, depth + 1)]
    return lines


def _set_lines(a: dict[str, Any]) -> list[str]:
    items = {k: str(v) for k, v in a["set"].items()}
    if len(items) == 1:
        [(k, v)] = items.items()
        return [f"{k} = {v}"]
    return [f"{', '.join(items)} = {', '.join(items.values())}"]  # all from the old values


def _emit_line(a: dict[str, Any]) -> list[str]:
    spec = a["emit"] if isinstance(a["emit"], dict) else {"port": a["emit"]}
    text = f"emit {spec['port']}"
    if spec.get("payload"):
        text += " {" + ", ".join(f"{k}: {v}" for k, v in spec["payload"].items()) + "}"
    if "size" in spec:
        text += f" size {spec['size']}"
    return [text]


def _after_line(a: dict[str, Any]) -> list[str]:
    return [f"after {a['after']} -> {a['event']}" + (" keep" if a.get("keep") else "")]


def _observe_lines(a: dict[str, Any]) -> list[str]:
    return [
        f"observe {k}" if str(v) == k else f"observe {k} = {v}" for k, v in a["observe"].items()
    ]


_FORMATTERS = {
    "set": _set_lines,
    "emit": _emit_line,
    "after": _after_line,
    "observe": _observe_lines,
    "goto": lambda a: [f"goto {a['goto']}"],
    "send": lambda a: [f"send {a['send']}"],
    "drop": lambda a: [f"drop {a['drop']}"],
    "count": lambda a: [f"count {a['count']}"],
    "push": lambda a: [f"push {a['push']}"],
    "pop": lambda a: [f"pop {a['pop']}"],
    "release": lambda a: [f"release {a['release']['from']} -> {a['release']['to']}"],
}
