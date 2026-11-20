"""Fail when a secret or personal file is tracked (or staged) in git.

Blocks: anything under secrets/, .env / .env.* (except .env.example), private keys (*.key, *.pem)
and Word documents (*.docx). Files already in history before this guard existed are listed in
ALLOWED_LEGACY so the check can be turned on without rewriting history; remove an entry once the
file is untracked.

Run:  python tools/check_tracked_secrets.py            (all tracked files, used by CI)
      python tools/check_tracked_secrets.py FILE...    (given files, used by pre-commit)
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED = ("secrets/*", "*/secrets/*", "*.key", "*.pem", "*.docx", "*.p12", "*.pfx")
ALLOWED_LEGACY = {
    "docs/PORTFOLIO_THREE_SYSTEMS.docx",
    "docs/Telecom_Fraud_Report_v2_EN.docx",
    "docs/Telecom_Fraud_Report_v2_VI.docx",
}


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
