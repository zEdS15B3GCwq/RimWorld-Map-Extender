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


def cmd_grids(args: argparse.Namespace) -> int:
    """Check that every map grid decodes and re-encodes losslessly; optionally render PNGs."""
    from .grids import decode_blob, encode_blob, find_grids, map_size

    tree = load_save(args.input)
    failures = 0
    for m in tree.getroot().find("game/maps"):
        mid = f"Map_{m.findtext('uniqueID')}"
        sx, sz = map_size(m)
        print(f"== {mid} {sx}x{sz}")
        for g in find_grids(m):
            arr = g.array(sx, sz)
            back = g.layout.to_bytes(arr)
            text, deflated = encode_blob(back)
            ok = back == g.data and decode_blob(text, deflated) == g.data
            failures += not ok
            layout = "bits" if g.layout.bits else f"{g.layout.itemsize}B"
            print(f"  {'ok ' if ok else 'BAD'} {g.path:<62} {layout:>4} {arr.shape} "
                  f"text {len(g.element.text.strip()):>6} -> {len(text.strip()):>6}")
        if args.render:
            from .render import render_map

            out = Path(args.render) / mid
            for line in render_map(m, out):
                pass
            print(f"  rendered -> {out}")
    print("all grids round-trip" if not failures else f"{failures} grid(s) FAILED")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="mapext")
    sub = p.add_subparsers(required=True)
    cs = sub.add_parser("census", help="report every coordinate-like value and grid, grouped by XML path")
    cs.add_argument("input")
    cs.add_argument("output", help="markdown report path")
    cs.set_defaults(func=cmd_census)
    gr = sub.add_parser("grids", help="verify grid decode/encode round trip, optionally render PNGs")
    gr.add_argument("input")
    gr.add_argument("--render", metavar="DIR", help="write one PNG per grid per map into DIR/<map>/")
    gr.set_defaults(func=cmd_grids)
    rt = sub.add_parser("roundtrip", help="load and re-write a save unchanged, then compare bytes")
    rt.add_argument("input")
    rt.add_argument("output")
    rt.set_defaults(func=cmd_roundtrip)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
