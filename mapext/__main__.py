"""Command line entry point: python -m mapext <command> ..."""
import argparse
import sys
import time
from pathlib import Path

from .savefile import load_save, write_save


def cmd_roundtrip(args: argparse.Namespace) -> int:
    src, dst = Path(args.input), Path(args.output)
    if src.resolve() == dst.resolve():
        print("Refusing to overwrite the input file.", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    tree = load_save(src)
    t1 = time.perf_counter()
    write_save(tree, dst)
    t2 = time.perf_counter()
    print(f"loaded in {t1 - t0:.1f}s, written in {t2 - t1:.1f}s -> {dst}")

    a, b = src.read_bytes(), dst.read_bytes()
    if a == b:
        print(f"identical: {len(a):,} bytes")
        return 0
    i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    print(f"DIFFERENT: sizes {len(a):,} vs {len(b):,}, first difference at byte {i:,}")
    print("  input: ", a[max(0, i - 80):i + 80])
    print("  output:", b[max(0, i - 80):i + 80])
    return 1


def cmd_census(args: argparse.Namespace) -> int:
    from .census import format_report, run_census

    groups, map_sizes = run_census(load_save(args.input))
    report = format_report(groups, map_sizes)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(report, encoding="utf-8")
    print(f"{len(groups)} path groups -> {args.output}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="mapext")
    sub = p.add_subparsers(required=True)
    cs = sub.add_parser("census", help="report every coordinate-like value and grid, grouped by XML path")
    cs.add_argument("input")
    cs.add_argument("output", help="markdown report path")
    cs.set_defaults(func=cmd_census)
    rt = sub.add_parser("roundtrip", help="load and re-write a save unchanged, then compare bytes")
    rt.add_argument("input")
    rt.add_argument("output")
    rt.set_defaults(func=cmd_roundtrip)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
