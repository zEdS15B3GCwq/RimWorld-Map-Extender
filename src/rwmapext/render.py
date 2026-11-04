"""Render map grids and thing positions as PNGs (north up) for visual checks."""
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

from .grids import find_cell_lists, find_grids, map_size

SCALE = 2


def _palette(n: int) -> np.ndarray:
    """n distinct-ish colors from the golden-ratio hue walk; index 0 is reserved for black."""
    rng = []
    for i in range(n):
        h = (i * 0.618033988749895) % 1.0
        s, v = 0.65, 0.95 if i % 2 else 0.75
        k = lambda m: v - v * s * max(0, min(1, min((m + h * 6) % 6, 4 - (m + h * 6) % 6)))  # noqa: E731
        rng.append([k(5), k(3), k(1)])
    return (np.array(rng) * 255).astype(np.uint8)


def colorize(arr: np.ndarray) -> np.ndarray:
    """Bool → black/white; few distinct values → categorical colors (0 = black); else grayscale."""
    if arr.ndim == 3:  # raw multi-byte cells: show the first byte
        arr = arr[..., 0]
    if arr.dtype == bool:
        return np.where(arr[..., None], 255, 0).repeat(3, axis=2).astype(np.uint8)
    values = np.unique(arr)
    if len(values) <= 64:
        pal = _palette(len(values) + 1)
        idx = np.searchsorted(values, arr)
        rgb = pal[idx + 1]
        rgb[arr == 0] = 0
        return rgb
    a = arr.astype(np.float64)
    lo, hi = np.percentile(a, 1), np.percentile(a, 99)
    g = np.clip((a - lo) / max(hi - lo, 1e-9), 0, 1)
    return (g[..., None].repeat(3, axis=2) * 255).astype(np.uint8)


def save_png(rgb: np.ndarray, path: Path) -> None:
    img = Image.fromarray(np.flipud(rgb))  # row 0 is south; images are drawn top-down
    if SCALE != 1:
        img = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
    img.save(path)


def thing_layer(map_el, sx: int, sz: int) -> np.ndarray:
    """Top-level things by category: buildings, plants, pawns, other."""
    rgb = np.zeros((sz, sx, 3), dtype=np.uint8)
    colors = {"Plant": (40, 140, 40), "Building": (200, 200, 200), "Pawn": (255, 60, 60)}
    for t in map_el.find("things"):
        pos = t.findtext("pos")
        if not pos:
            continue
        x, _, z = (int(v) for v in pos.strip("()").split(","))
        if not (0 <= x < sx and 0 <= z < sz):
            continue
        cls = t.get("Class") or ""
        key = "Plant" if "Plant" in cls else "Pawn" if cls == "Pawn" else "Building" if "Building" in cls or "Door" in cls else None
        rgb[z, x] = colors.get(key, (120, 120, 255))
    return rgb


def render_map(map_el, out_dir: Path) -> list[str]:
    sx, sz = map_size(map_el)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines, seen = [], Counter()
    for g in find_grids(map_el):
        arr = g.array(sx, sz)
        name = g.path.replace("/", "_").replace("[", "-").replace("]", "")
        seen[name] += 1
        if seen[name] > 1:
            name += f"_{seen[name]}"
        save_png(colorize(arr), out_dir / f"{name}.png")
        layout = "bits" if g.layout.bits else f"{g.layout.itemsize}B"
        lines.append(f"{g.path}: {layout}, {len(np.unique(arr))} distinct values")
    for el, path in find_cell_lists(map_el):
        arr = np.array([c.text == "True" for c in el]).reshape(sz, sx)
        save_png(colorize(arr), out_dir / (path.replace("/", "_").replace("[", "-").replace("]", "") + ".png"))
        lines.append(f"{path}: <li> list, {int(arr.sum())} True")
    save_png(thing_layer(map_el, sx, sz), out_dir / "things.png")
    return lines
