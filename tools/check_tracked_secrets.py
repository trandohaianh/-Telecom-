"""Fail when a secret or personal file is tracked (or staged) in git.

Blocks: anything under secrets/, .env / .env.* (except .env.example), private keys (*.key, *.pem)
and Word documents (*.docx). ALLOWED_LEGACY lists files that may stay tracked despite a blocked
pattern; it is empty (the three report .docx files were untracked, they remain in old history).

Run:  python tools/check_tracked_secrets.py            (all tracked files, used by CI)
      python tools/check_tracked_secrets.py FILE...    (given files, used by pre-commit)
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED = ("secrets/*", "*/secrets/*", "*.key", "*.pem", "*.docx", "*.p12", "*.pfx")
ALLOWED_LEGACY: set[str] = set()


def is_blocked(path: str) -> bool:
    path = path.replace("\\", "/")
    name = PurePosixPath(path).name
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in BLOCKED)


def main(argv: list[str]) -> int:
    paths = argv or subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
    bad = [p for p in paths if p and is_blocked(p) and p.replace("\\", "/") not in ALLOWED_LEGACY]
    for p in bad:
        print(f"blocked file must not be committed: {p}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
