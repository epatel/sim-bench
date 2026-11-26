#!/usr/bin/env python3
"""Fetch the pinned mermaid bundle into web/vendor/mermaid/ (it renders ```mermaid in
agent replies; see cards/agent.md). It is not in git: `make vendor` and `make deploy`
run this. A copy that is already there with the pinned version is left alone.

    python3 scripts/vendor_mermaid.py           # fetch if missing or another version
    python3 scripts/vendor_mermaid.py --check   # compare the pin with npm's latest

The file comes from jsDelivr (the npm package's dist/mermaid.min.js) and must match the
SHA-256 pinned here, so a changed or corrupted download is refused. To upgrade: run
--check, set VERSION and SHA256 from its output, try a diagram in the chat, commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

VERSION = "12.3.0"
SHA256 = "M234A9h6OLcKAec25mbI92AdLOQvXkfI+v5dchB0uaM="  # base64, as jsDelivr lists it
FILES = {"dist/mermaid.min.js": "mermaid.min.js", "LICENSE": "LICENSE"}

DEST = Path(__file__).resolve().parent.parent / "web" / "vendor" / "mermaid"
CDN = "https://cdn.jsdelivr.net/npm/mermaid@{version}/{path}"
LISTING = "https://data.jsdelivr.com/v1/packages/npm/mermaid@{version}?structure=flat"
LATEST = "https://registry.npmjs.org/mermaid/latest"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sim-bench-vendor"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def installed() -> str:
    marker = DEST / "VERSION"
    return marker.read_text().strip() if marker.is_file() else ""


def fetch() -> None:
    if installed() == VERSION and (DEST / "mermaid.min.js").is_file():
        print(f"mermaid {VERSION}: already there")
        return
    data = {path: get(CDN.format(version=VERSION, path=path)) for path in FILES}
    got = digest(data["dist/mermaid.min.js"])
    if got != SHA256:
        sys.exit(f"mermaid {VERSION}: checksum mismatch (got {got}); nothing written")
    DEST.mkdir(parents=True, exist_ok=True)
    for path, name in FILES.items():
        tmp = DEST / f"{name}.tmp"
        tmp.write_bytes(data[path])
        tmp.replace(DEST / name)
    (DEST / "VERSION").write_text(VERSION + "\n")
    print(f"mermaid {VERSION}: fetched ({len(data['dist/mermaid.min.js']) // 1024} KiB)")


def check() -> None:
    latest = json.loads(get(LATEST))["version"]
    print(f"mermaid pinned {VERSION}, installed {installed() or 'none'}, latest {latest}")
    if latest == VERSION:
        return
    listing = json.loads(get(LISTING.format(version=latest)))
    sha = next(f["hash"] for f in listing["files"] if f["name"] == "/dist/mermaid.min.js")
    print(f'to upgrade, set in {Path(__file__).name}:\n  VERSION = "{latest}"\n  SHA256 = "{sha}"')


if __name__ == "__main__":
    check() if "--check" in sys.argv[1:] else fetch()
