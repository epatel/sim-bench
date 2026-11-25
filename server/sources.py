"""Registered source trees the agent may read (milestone M8).

The user registers other projects' directories by name. The agent can read
them (Read/Grep/Glob, enforced by a PreToolUse hook) and nothing else, and it
never runs their code. Paths are machine-specific, so the registry lives in a
gitignored local file. A provenance reference looks like ``name:path/file.py:120``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Files the agent must not read even inside a registered source.
SECRET_PATTERNS = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|[^/]*\.(pem|key|p12|pfx|keystore|jks)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
    r"|\.netrc|\.pypirc|\.npmrc|credentials(\.json)?|[^/]*secret[^/]*)$",
    re.IGNORECASE,
)
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
REF = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9_-]*):(?P<path>[^:]+?)(?::(?P<line>\d+)(?:-(?P<end>\d+))?)?$"
)


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    path: Path


class SourceRegistry:
    def __init__(self, file: Path) -> None:
        self.file = file

    def all(self) -> list[Source]:
        if not self.file.exists():
            return []
        data = json.loads(self.file.read_text() or "{}")
        return [Source(n, Path(p)) for n, p in sorted(data.items())]

    def add(self, name: str, path: str) -> Source:
        if not NAME.match(name or ""):
            raise SourceError("a source name is lowercase letters, digits, '-' or '_'")
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise SourceError("give an absolute path")
        p = p.resolve()
        if not p.is_dir():
            raise SourceError(f"{p} is not a directory")
        if p in (Path("/"), Path.home()) or len(p.parts) < 3:
            raise SourceError(f"{p} is too broad; register a project directory")
        data = {s.name: str(s.path) for s in self.all()}
        data[name] = str(p)
        self._write(data)
        return Source(name, p)

    def remove(self, name: str) -> None:
        data = {s.name: str(s.path) for s in self.all() if s.name != name}
        self._write(data)

    def _write(self, data: dict[str, str]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2) + "\n")

    # --- access checks ------------------------------------------------------------

    def check_readable(self, path: str | None) -> Path:
        """The resolved path if it is inside a registered source and not a secret."""
        if not path:
            raise SourceError("pass a path inside a registered source")
        p = Path(path).expanduser().resolve()
        if not any(p == s.path or p.is_relative_to(s.path) for s in self.all()):
            names = ", ".join(f"{s.name} ({s.path})" for s in self.all()) or "none registered"
            raise SourceError(f"{p} is outside the registered sources: {names}")
        if SECRET_PATTERNS.search(p.as_posix()):
            raise SourceError(f"{p.name} looks like a secret; it is not readable")
        return p

    def snippet(self, ref: str, context: int = 8) -> dict[str, Any]:
        """Lines around a provenance reference ``name:path[:line[-end]]``."""
        m = REF.match(ref or "")
        if not m:
            raise SourceError(f"bad source reference {ref!r} (expected name:path/file:line)")
        src = next((s for s in self.all() if s.name == m["name"]), None)
        if src is None:
            raise SourceError(f"no registered source {m['name']!r}")
        path = self.check_readable(str(src.path / m["path"]))
        if not path.is_file():
            raise SourceError(f"{m['path']} is not a file in {m['name']}")
        lines = path.read_text(errors="replace").splitlines()
        first = int(m["line"] or 1)
        last = int(m["end"] or first)
        lo, hi = max(1, first - context), min(len(lines), last + context)
        return {
            "ref": ref,
            "file": m["path"],
            "first": first,
            "last": last,
            "start": lo,
            "lines": lines[lo - 1 : hi],
        }
