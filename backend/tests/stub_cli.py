"""A real command-line program for connector tests.

Behaves like a CLI that signs in with its own browser login: `status` fails
until `login` has run (it leaves a marker file next to itself, the way a real
CLI keeps its credential in its own config), `--help` prints real help text, and
`greet`/`echo-env` report exactly what they were given. On Windows a `.cmd`
wrapper around it stands in for the npm-installed CLIs (`vercel`, `npm`...),
which are scripts rather than executables.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = Path(os.environ.get("STUB_CLI_HOME") or HERE) / "stubcli-signed-in"

HELP = """stubcli - a tiny test program

Usage:
  stubcli <command> [flags]

Commands:
  status     Show whether you are signed in
  greet      Greet someone by name
  echo-env   Report whether STUBCLI_TOKEN was given
  login      Sign in

Flags:
  --json     print JSON
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("--help", "-h", "help"):
        print(HELP)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "--version":
        print("stubcli 1.0.0")
        return 0
    if command == "login":
        MARKER.write_text("yes", encoding="utf-8")
        print("Signed in.")
        return 0
    if command == "status":
        if not MARKER.exists():
            print("Error: not signed in. Run: stubcli login", file=sys.stderr)
            return 4
        print(json.dumps({"signedIn": True, "credits": 12}))
        return 0
    if command == "greet":
        print(json.dumps({"greeting": f"Hello, {rest[0] if rest else 'nobody'}!",
                          "args": rest}))
        return 0
    if command == "echo-env":
        print(json.dumps({"hasToken": bool(os.environ.get("STUBCLI_TOKEN")),
                          "sawGeminiKey": bool(os.environ.get("GEMINI_API_KEY"))}))
        return 0
    print(f"Error: unknown command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
