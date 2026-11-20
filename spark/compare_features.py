"""Compare CALL feature tables. Strict completeness is the default CI contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from feature_store import CALL_FEATURES

ATOL, RTOL = 1e-9, 1e-9


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"Missing {path}; generate inputs and features first")
    frame = pd.read_parquet(path)
    if "event_id" not in frame:
        raise SystemExit(f"{path} has no event_id column")
    return frame.set_index("event_id").sort_index()


def compare(ref: pd.DataFrame, got: pd.DataFrame, *, allow_partial: bool = False) -> int:
    """Return nonzero for duplicate/missing/extra IDs, nonfinite values or mismatch.

    allow_partial permits missing feature columns only, for the SQL tutorial.
    Row coverage, uniqueness and numeric correctness are always required.
    """
    failures = []
    for name, frame in (("reference", ref), ("result", got)):
        if frame.index.hasnans or not frame.index.is_unique:
            failures.append(f"{name}: event_id must be non-null and unique")
    if failures:
        print("\n".join(failures))
        return 1
    missing = ref.index.difference(got.index)
    extra = got.index.difference(ref.index)
    print(f"reference={len(ref)} result={len(got)} missing={len(missing)} extra={len(extra)}")
    if len(missing) or len(extra):
        failures.append("event_id sets differ")
    common = ref.index.intersection(got.index)
    if not len(common):
        failures.append("no matching events")
    matched, todo = 0, []
    for col in CALL_FEATURES:
        if col not in ref:
            failures.append(f"reference missing {col}")
            continue
        if col not in got:
            todo.append(col)
            continue
        a = pd.to_numeric(ref.loc[common, col], errors="coerce").to_numpy(dtype=float)
        b = pd.to_numeric(got.loc[common, col], errors="coerce").to_numpy(dtype=float)
        good = np.isfinite(a) & np.isfinite(b) & np.isclose(a, b, atol=ATOL, rtol=RTOL)
        if good.all() and len(common):
            matched += 1
        elif len(common):
            failures.append(f"{col}: {int((~good).sum())} invalid or mismatched values")
    if todo and not allow_partial:
        failures.append(f"missing {len(todo)} feature columns: {', '.join(todo)}")
    print(f"{matched}/{len(CALL_FEATURES)} matched; {len(todo)} unimplemented")
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", type=Path, default=HERE / "reference_call_features.parquet")
    parser.add_argument("--got", type=Path, default=HERE / "spark_call_features.parquet")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Permit missing feature columns, never missing rows or invalid values",
    )
    args = parser.parse_args()
    return compare(load(args.ref), load(args.got), allow_partial=args.allow_partial)


if __name__ == "__main__":
    raise SystemExit(main())
