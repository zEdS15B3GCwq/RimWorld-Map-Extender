"""Pad a map (and the maps linked to it), filling the border from the nearest old edge cell and
shifting every cell reference.

What to change is decided by the census rules (`census.RULES`), so this module only implements
the actions:
- size: `mapInfo/size` and BoolGrid `mapSizeX`/`mapSizeZ`;
- grid: every byte-array grid and one-<li>-per-cell list, filled as `_fill_grid` decides;
- shift: values under a padded map's subtree;
- shift-if-map: world-level values whose target map is padded (history look targets, quest part
  cells, the camera, DeepRim `holeLocation`).
Any `unknown` path aborts before the tree is changed.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from lxml import etree

from .census import INVALID_IV3, classify_rule, iter_values, map_index
from .grids import find_grids, pad_array


@dataclass
class Padding:
    west: int = 0
    east: int = 0
    south: int = 0
    north: int = 0

    def __post_init__(self):
        if min(self.west, self.east, self.south, self.north) < 0:
            raise ValueError("padding must be >= 0")


@dataclass
class PadReport:
    maps: list = field(default_factory=list)
    actions: Counter = field(default_factory=Counter)
    unresolved: list = field(default_factory=list)  # world-level values whose map couldn't be found

    def format(self) -> str:
        lines = [f"padded maps: {', '.join(self.maps)}"]
        lines += [f"  {k}: {v}" for k, v in sorted(self.actions.items())]
        for path, text in self.unresolved:
            lines.append(f"  unresolved map, left unchanged: {path} = {text}")
        return "\n".join(lines)


def stable_string_hash(text: str) -> int:
    """GenText.StableStringHash: a wrapped signed 32-bit int."""
    h = 23
    for ch in text:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _short_hashes(names: tuple[str, ...]) -> set[int]:
    """Possible Def.shortHash values. The game moves a hash up when it is taken, which depends on the
    mod list (MineableSteel is one above its plain hash in the sample), so accept a few steps.
    Only used for roofs, which have few defs."""
    out = set()
    for name in names:
        h = stable_string_hash(name)
        base = (abs(h) % 65535) * (-1 if h < 0 else 1) & 0xFFFF  # C# % keeps the sign, then cast to ushort
        out.update(range(base, base + 4))
    return out


NATURAL_ROOFS = _short_hashes(("RoofRockThin", "RoofRockThick"))


def _edge_source(label: str, path: str, arr: np.ndarray, under: np.ndarray | None) -> np.ndarray | None:
    """What the border copies for one grid, as an array of the old size; None = stays empty."""
    if label == "topGrid" and path.startswith("terrainGrid/"):
        return np.where(under != 0, under, arr)  # under is set only below a built floor
    if label == "roofs":
        return np.where(np.isin(arr, list(NATURAL_ROOFS)), arr, 0)
    if label in ("compressedThingMap", "fogGrid", "riverFlowMap") or path.startswith("temperatureCache/"):
        return arr  # compressed things (rock, ore, chunks, ...) are copied whatever they are
    return None


def _fill_grid(g, arr: np.ndarray, under: np.ndarray | None, pad: Padding) -> np.ndarray:
    """Pad one grid: the old area unchanged, the border from the nearest edge cell or empty."""
    source = _edge_source(g.label, g.path, arr, under)
    if source is None:
        return pad_array(arr, pad.west, pad.east, pad.south, pad.north)
    out = pad_array(source, pad.west, pad.east, pad.south, pad.north, mode="edge")
    out[pad.south:pad.south + arr.shape[0], pad.west:pad.west + arr.shape[1]] = arr
    return out


def _fmt_float(v: float) -> str:
    """float32 with 9 significant digits, the way the save writes Vector3 components."""
    return f"{float(np.float32(v)):.9g}"


def shift_text(kind: str, m: re.Match, pad: Padding) -> str | None:
    """New text for a coordinate value shifted by (west, south), or None to leave it unchanged."""
    dx, dz = pad.west, pad.south
    if kind in ("IntVec3", "IntVec3+map"):
        x, y, z = int(m[1]), int(m[2]), int(m[3])
        if (x, y, z) == INVALID_IV3:
            return None
        text = f"({x + dx}, {y}, {z + dz})"
        return f"{text}, {m[4]}" if kind == "IntVec3+map" else text
    if kind == "CellRect":
        a, b, c, d = (int(m[i]) for i in range(1, 5))
        return f"({a + dx},{b + dz},{c + dx},{d + dz})"
    if kind == "IntVec2":
        return f"({int(m[1]) + dx}, {int(m[2]) + dz})"
    if kind == "Vector3":
        return f"({_fmt_float(float(m[1]) + dx)}, {m[2]}, {_fmt_float(float(m[3]) + dz)})"
    raise ValueError(f"don't know how to shift {kind}")


def linked_maps(map_el, by_id: dict) -> list[str]:
    """Map ids that must be padded together with `map_el`: all MultiFloors levels of its ground map,
    which share coordinates. Only the ground map lists the levels, so go through it."""
    def level_comp(el):
        return next((c for c in el.find("components") if c.get("Class") == "MultiFloors.MF_LevelMapComp"), None)

    comp = level_comp(map_el)
    if comp is None:
        return []
    ground = by_id.get(comp.findtext("groundMap"))
    if ground is not None:
        comp = level_comp(ground)
    out = [comp.findtext("groundMap")] + [li.text for li in comp.iterfind("mapByLevel/values/li")]
    return [m for m in dict.fromkeys(out) if m in by_id]


def _world_target_resolver(root, map_ids: dict):
    """Return f(value) -> target map id (or None) for world-level `shift-if-map` values."""
    parent_to_map = {el.findtext("mapInfo/parent"): mid for el, mid in map_ids.items()}
    thing_to_map = {}
    for el, mid in map_ids.items():
        for t in el.find("things").iter():
            idt = t.find("id") if len(t) else None
            if idt is not None and t.find("def") is not None:
                thing_to_map[f"Thing_{idt.text}"] = mid
    maps_el = root.find("game/maps")
    current = root.findtext("game/currentMapIndex")
    current_map = map_ids.get(maps_el[int(current)]) if current and int(current) >= 0 else None

    def quest_part_map(el):
        # The quest part is the ancestor `li` under `parts`; its map is `mapParent` (a world object)
        # or the map holding the pawn named by a `map*` field (mapParentOfPawn, mapOfPawn, mapPawn).
        part = el
        while part is not None and not (part.getparent() is not None and part.getparent().tag == "parts"):
            part = part.getparent()
        if part is None:
            return None
        mp = part.findtext("mapParent")
        if mp in parent_to_map:
            return parent_to_map[mp]
        for c in part:
            if isinstance(c.tag, str) and c.tag.startswith("map") and c.text in thing_to_map:
                return thing_to_map[c.text]
        return None

    def resolve(v):
        if v.kind == "IntVec3+map":
            return v.match[4]
        if v.element.tag == "holeLocation":
            # On the DeepRim world object; "" = that layer has no map now, which is normal.
            return parent_to_map.get(f"WorldObject_{v.element.getparent().findtext('ID')}", "")
        if v.path.startswith("game/questManager/"):
            return quest_part_map(v.element)
        if v.path == "game/cameraMap/camRootPos":
            return current_map
        return None

    return resolve


def select_groups(root, map_ids: list[str]) -> list[list[str]]:
    """Padding groups for the chosen maps: each chosen map plus its MultiFloors levels, without repeats."""
    elements, sizes = map_index(root)
    by_id = {mid: el for el, mid in elements.items()}
    groups, seen = [], set()
    for mid in map_ids:
        if mid not in by_id:
            raise KeyError(f"no map {mid}; maps: {', '.join(by_id)}")
        if mid in seen:
            continue
        group = [mid] + [m for m in linked_maps(by_id[mid], by_id) if m != mid]
        for m in group:
            if sizes[m] != sizes[mid]:
                raise ValueError(f"linked map {m} is {sizes[m]}, {mid} is {sizes[mid]}")
        groups.append(group)
        seen.update(group)
    return groups


def pad_save(tree, map_ids: list[str], pad: Padding) -> PadReport:
    """Pad the chosen maps and their linked maps in place."""
    root = tree.getroot()
    map_els, map_sizes = map_index(root)
    by_id = {mid: el for el, mid in map_els.items()}
    padded = [m for group in select_groups(root, map_ids) for m in group]
    report = PadReport(maps=padded)

    # Classify everything first so an unknown path aborts before anything changes.
    values = []
    unknown = set()
    for v in iter_values(root):
        action, _ = classify_rule(v.path, v.kind)
        if action == "unknown":
            unknown.add((v.path, v.kind))
        values.append((v, action))
    if unknown:
        raise ValueError("unknown census paths, add rules first:\n" + "\n".join(f"  {p} [{k}]" for p, k in sorted(unknown)))
    resolve = _world_target_resolver(root, map_els)

    grid_elements = set()
    for mid in padded:
        sx, sz = map_sizes[mid]
        grids = find_grids(by_id[mid])
        under = next(g.array(sx, sz) for g in grids if g.label == "underGrid")
        for g in grids:
            arr = g.array(sx, sz)
            g.store(_fill_grid(g, arr, under, pad))
            grid_elements.add(g.element)
            report.actions["grid edge" if _edge_source(g.label, g.path, arr, under) is not None else "grid empty"] += 1

    for v, action in values:
        el, text = v.element, (v.element.text or "").strip()
        if action == "grid":
            if v.map_id not in padded:
                continue
            if v.kind.startswith("cell-list"):
                # One <li> per cell (MixedBiomeMapComponent/biomeGrid): pad by copying the edge.
                sx, sz = map_sizes[v.map_id]
                cells = np.array([c.text for c in el], dtype=object).reshape(sz, sx)
                cells = np.pad(cells, ((pad.south, pad.north), (pad.west, pad.east)), mode="edge")
                for c in list(el):
                    el.remove(c)
                for t in cells.reshape(-1):
                    li = el.makeelement("li")
                    li.text = t
                    el.append(li)
                report.actions["grid cell-list"] += 1
            elif el not in grid_elements:
                raise ValueError(f"blob at {v.path} on {v.map_id} isn't a grid of the map's size")
        elif action == "size":
            if v.map_id not in padded:
                continue
            sx, sz = map_sizes[v.map_id]
            nx, nz = sx + pad.west + pad.east, sz + pad.south + pad.north
            if v.kind == "IntVec3":  # mapInfo/size
                el.text = f"({nx}, {v.match[2]}, {nz})"
            elif el.tag == "mapSizeX":
                el.text = str(nx)
            elif el.tag == "mapSizeZ":
                el.text = str(nz)
            else:
                raise ValueError(f"unexpected size field {v.path}")
            report.actions["size"] += 1
        elif action == "shift":
            if v.map_id not in padded:
                continue
            new = shift_text(v.kind, v.match, pad)
            if new is not None:
                el.text = new
                report.actions[f"shift {v.kind}"] += 1
        elif action == "shift-if-map":
            target = resolve(v)
            if target is None:
                if shift_text(v.kind, v.match, pad) is not None:  # valid value we couldn't place
                    report.unresolved.append((v.path, text))
                continue
            if target in padded:
                new = shift_text(v.kind, v.match, pad)
                if new is not None:
                    el.text = new
                    report.actions[f"shift-if-map {v.kind}"] += 1
    return report


def _check_border(mid: str, og: list, pg: list, old: tuple, new: tuple, pad: Padding) -> list[str]:
    """Check border cells without using the fill code: each is its nearest edge cell or allowed empty."""
    (sx, sz), (nx, nz) = old, new
    zz, xx = np.meshgrid(np.arange(nz) - pad.south, np.arange(nx) - pad.west, indexing="ij")
    border = (zz < 0) | (zz >= sz) | (xx < 0) | (xx >= sx)
    nearest = np.clip(zz, 0, sz - 1), np.clip(xx, 0, sx - 1)
    under = next(g.array(sx, sz) for g in og if g.label == "underGrid")
    problems = []
    for a, b in zip(og, pg):
        was, now = a.array(sx, sz), b.array(nx, nz)
        edge = was[nearest][border]
        got = now[border]
        if a.label == "topGrid" and a.path.startswith("terrainGrid/"):
            good = got == np.where(under[nearest][border] != 0, under[nearest][border], edge)
        elif a.label == "roofs":
            good = (got == edge) & np.isin(got, list(NATURAL_ROOFS)) | (got == 0) & ~np.isin(edge, list(NATURAL_ROOFS))
        elif a.label in ("compressedThingMap", "fogGrid", "riverFlowMap") or a.path.startswith("temperatureCache/"):
            good = got == edge
        else:
            good = got == 0
        if not np.all(good):
            problems.append(f"{mid}: grid {a.path} has {int((~good).sum())} wrong border value(s)")
    return problems


def verify_pad(orig_tree, padded_tree, map_ids: list[str], pad: Padding) -> list[str]:
    """Check a padded save against its original; return a list of problems (empty = ok).

    - every padded map has its new size, and every other map's subtree is unchanged;
    - every grid of a padded map, cropped back to the old area, equals the original;
    - every coordinate value either equals the original or the original shifted by the padding,
      and the shifted ones are exactly the values `pad_save` should shift.
    """
    problems = []
    oroot, proot = orig_tree.getroot(), padded_tree.getroot()
    omaps, osizes = map_index(oroot)
    pmaps, psizes = map_index(proot)
    oby = {mid: el for el, mid in omaps.items()}
    pby = {mid: el for el, mid in pmaps.items()}
    padded = [m for group in select_groups(oroot, map_ids) for m in group]
    for mid, (sx, sz) in osizes.items():
        expect = (sx + pad.west + pad.east, sz + pad.south + pad.north) if mid in padded else (sx, sz)
        if psizes.get(mid) != expect:
            problems.append(f"{mid}: size {psizes.get(mid)}, expected {expect}")
        elif mid not in padded and etree.tostring(oby[mid]) != etree.tostring(pby[mid]):
            problems.append(f"{mid}: changed, but it wasn't selected")
    if problems:
        return problems

    for mid in padded:
        sx, sz = osizes[mid]
        crop = (slice(pad.south, pad.south + sz), slice(pad.west, pad.west + sx))
        og, pg = find_grids(oby[mid]), find_grids(pby[mid])
        if [g.path for g in og] != [g.path for g in pg]:
            problems.append(f"{mid}: grid list differs")
            continue
        for a, b in zip(og, pg):
            if not np.array_equal(a.array(sx, sz), b.array(*psizes[mid])[crop]):
                problems.append(f"{mid}: grid {a.path} doesn't crop back to the original")
        problems += _check_border(mid, og, pg, (sx, sz), psizes[mid], pad)

    resolve = _world_target_resolver(oroot, omaps)
    for a, b in zip(iter_values(oroot), iter_values(proot)):
        if a.path != b.path or a.kind.split("[")[0] != b.kind.split("[")[0]:
            problems.append(f"value order differs at {a.path} / {b.path}")
            break
        action, _ = classify_rule(a.path, a.kind)
        if a.match is None or action == "size":
            continue
        should = (action == "shift" and a.map_id in padded) or (action == "shift-if-map" and resolve(a) in padded)
        expected = (should and shift_text(a.kind, a.match, pad)) or (a.element.text or "").strip()
        got = (b.element.text or "").strip()
        if got != expected:
            problems.append(f"{a.path}: {a.element.text!r} -> {got!r}, expected {expected!r}")
            if len(problems) > 20:
                break
    return problems
