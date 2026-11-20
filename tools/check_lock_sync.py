"""Check that requirements-serve.lock still pins every version listed in requirements-serve.txt.

The lock (pip-compile --generate-hashes) is what the Docker images install with --require-hashes.
Editing a pin in requirements-serve.txt without regenerating the lock would silently keep the old
version in production; this check fails in that case. Re-running pip-compile in CI is avoided on
purpose: PyPI can add wheels to an existing release, which would change hashes and make CI flaky.

Regenerate:  pip-compile --allow-unsafe --generate-hashes --strip-extras \
                 --output-file=requirements-serve.lock requirements-serve.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;#\\]+)")


def pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PIN.match(line.strip())
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def main() -> int:
    wanted = pins(ROOT / "requirements-serve.txt")
    locked = pins(ROOT / "requirements-serve.lock")
    bad = [f"{n}=={v} (lock: {locked.get(n, 'missing')})" for n, v in wanted.items() if locked.get(n) != v]
    for b in bad:
        print(f"requirements-serve.lock out of sync: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
