#!/bin/sh
# PreToolUse(Bash): block git force pushes however they are spelled.
command -v python3 >/dev/null || exit 0      # tool missing: do nothing, never block
exec python3 -I -c '
import json, re, shlex, sys
cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
def forced(args):
    return any(a == "--force" or a.startswith(("--force-", "+"))
               or re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", a) for a in args)
for part in re.split(r"&&|\|\||[;|\n]", cmd):
    try:
        words = shlex.split(part)
    except ValueError:
        words = part.split()
    while words and (words[0] in ("sudo", "env", "command", "rtk") or re.fullmatch(r"\w+=.*", words[0])):
        words = words[1:]
    if words[:1] == ["git"] and "push" in words and forced(words[words.index("push") + 1:]):
        print("Force push is blocked in this project. Push without --force, or ask the user.", file=sys.stderr)
        sys.exit(2)
'
