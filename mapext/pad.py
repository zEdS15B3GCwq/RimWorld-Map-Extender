"""Pad a map (and the maps linked to it) with empty border cells, shifting every cell reference.

What to change is decided by the census rules (`census.RULES`), so this module only implements
the actions:
- size: `mapInfo/size` and BoolGrid `mapSizeX`/`mapSizeZ`;
- grid: every byte-array grid and one-<li>-per-cell list, padded with a neutral value;
- shift: values under a padded map's subtree;
- shift-if-map: world-level values whose target map is padded (history look targets, quest part
  cells, the camera).
Any `unknown` path aborts before the tree is changed.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

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


def _grid_pad_mode(path: str, label: str) -> tuple[str, object]:
    """(np.pad mode, constant) for one grid. Values are def shortHashes or amounts, so 0 = none,
    except where an empty cell would be invalid (terrain) or misleading (room temperatures)."""
    if path.startswith("terrainGrid/") and label in ("topGrid", "underGrid"):
        return "edge", None  # every cell needs a terrain; copy the edge (blending is for later)
    if path.startswith("temperatureCache/"):
        return "edge", None
    return "constant", 0


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
        if v.path.startswith("game/questManager/"):
            return quest_part_map(v.element)
        if v.path == "game/cameraMap/camRootPos":
            return current_map
        return None

    return resolve


def pad_save(tree, map_id: str, pad: Padding) -> PadReport:
    """Pad `map_id` and its linked maps in place."""
    root = tree.getroot()
    map_ids, map_sizes = map_index(root)
    by_id = {mid: el for el, mid in map_ids.items()}
    if map_id not in by_id:
        raise KeyError(f"no map {map_id}; maps: {', '.join(by_id)}")
    padded = [map_id] + [m for m in linked_maps(by_id[map_id], by_id) if m != map_id]
    for mid in padded:
        if map_sizes[mid] != map_sizes[map_id]:
            raise ValueError(f"linked map {mid} is {map_sizes[mid]}, {map_id} is {map_sizes[map_id]}")
    sx, sz = map_sizes[map_id]
    nx, nz = sx + pad.west + pad.east, sz + pad.south + pad.north
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
    resolve = _world_target_resolver(root, map_ids)

    grid_elements = set()
    for mid in padded:
        m = by_id[mid]
        for g in find_grids(m):
            mode, const = _grid_pad_mode(g.path, g.label)
            arr = pad_array(g.array(sx, sz), pad.west, pad.east, pad.south, pad.north, value=const, mode=mode)
            g.store(arr)
            grid_elements.add(g.element)
            report.actions[f"grid {mode}"] += 1

    for v, action in values:
        el, text = v.element, (v.element.text or "").strip()
        if action == "grid":
            if v.map_id not in padded:
                continue
            if v.kind.startswith("cell-list"):
                # One <li> per cell (MixedBiomeMapComponent/biomeGrid): pad by copying the edge.
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


def verify_pad(orig_tree, padded_tree, map_id: str, pad: Padding) -> list[str]:
    """Check a padded save against its original; return a list of problems (empty = ok).

    - every grid of a padded map, cropped back to the old area, equals the original;
    - every coordinate value either equals the original or the original shifted by the padding,
      and the shifted ones are exactly the values `pad_save` should shift;
    - mapInfo/size is the new size.
    """
    problems = []
    oroot, proot = orig_tree.getroot(), padded_tree.getroot()
    omaps, osizes = map_index(oroot)
    pmaps, psizes = map_index(proot)
    oby = {mid: el for el, mid in omaps.items()}
    pby = {mid: el for el, mid in pmaps.items()}
    padded = [map_id] + [m for m in linked_maps(oby[map_id], oby) if m != map_id]
    sx, sz = osizes[map_id]
    want = (sx + pad.west + pad.east, sz + pad.south + pad.north)
    for mid in osizes:
        expect = want if mid in padded else osizes[mid]
        if psizes.get(mid) != expect:
            problems.append(f"{mid}: size {psizes.get(mid)}, expected {expect}")
    if problems:
        return problems

    crop = (slice(pad.south, pad.south + sz), slice(pad.west, pad.west + sx))
    for mid in padded:
        og, pg = find_grids(oby[mid]), find_grids(pby[mid])
        if [g.path for g in og] != [g.path for g in pg]:
            problems.append(f"{mid}: grid list differs")
            continue
        for a, b in zip(og, pg):
            if not np.array_equal(a.array(sx, sz), b.array(*want)[crop]):
                problems.append(f"{mid}: grid {a.path} doesn't crop back to the original")

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
