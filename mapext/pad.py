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
from .grids import find_grids, map_size, pad_array


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
    notes: list = field(default_factory=list)

    def format(self) -> str:
        lines = [f"padded maps: {', '.join(self.maps)}"] + [f"  {n}" for n in self.notes]
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
    Alone this is a guess; `find_def_value` confirms a name against what the save contains."""
    out = set()
    for name in names:
        h = stable_string_hash(name)
        base = (abs(h) % 65535) * (-1 if h < 0 else 1) & 0xFFFF  # C# % keeps the sign, then cast to ushort
        out.update(range(base, base + 4))
    return out


# Which grids hold each kind of def, for confirming a def name against the save.
DEF_GRIDS = {"terrain": ("topGrid", "underGrid"), "thing": ("compressedThingMap",), "roof": ("roofs",)}


def present_values(root) -> dict[str, set[int]]:
    """Every def value used on any map of the save, per kind."""
    out = {kind: set() for kind in DEF_GRIDS}
    for m in root.find("game/maps"):
        sx, sz = map_size(m)
        for g in find_grids(m):
            for kind, labels in DEF_GRIDS.items():
                if g.label in labels and (kind != "terrain" or g.path.startswith("terrainGrid/")):
                    out[kind].update(np.unique(g.array(sx, sz)).tolist())
    for s in out.values():
        s.discard(0)
    return out


def find_def_value(name: str, kind: str, present: dict) -> int:
    """The save's value for def `name`; it must occur on some map, so the mod list's hash is confirmed."""
    found = sorted(_short_hashes((name,)) & present[kind])
    if not found:
        raise ValueError(f"{kind} {name} isn't on any map in this save; use the def name of something that is")
    if len(found) > 1:
        raise ValueError(f"{kind} {name} matches several values in this save ({found}); choose another")
    return found[0]


@dataclass
class SolidFill:
    """What `--fill solid` puts in one map's border (def values from the save)."""
    role: str           # surface, underground or upper (MultiFloors level above ground)
    terrain: int = 0    # top terrain away from flowing water
    rock: int = 0       # compressedThingMap value (underground)
    roof: int = 0       # roof value (underground)
    note: str = ""


def map_role(root, map_el) -> str:
    comp = next((c for c in map_el.find("components") if c.get("Class") == "MultiFloors.MF_LevelMapComp"), None)
    level = int(comp.findtext("level") or 0) if comp is not None else 0
    if level > 0:
        return "upper"
    if level < 0:
        return "underground"
    parent = map_el.findtext("mapInfo/parent")
    for section in ("worldObjects/worldObjects", "pocketMaps"):
        for wo in root.find(f"game/world/{section}"):
            if f"WorldObject_{wo.findtext('ID')}" == parent:
                return "underground" if wo.get("Class") == "DeepRim.UndergroundMapParent" else "surface"
    return "surface"


def _most_common(values: np.ndarray) -> int:
    values = values[values != 0]
    if values.size == 0:
        return 0
    v, n = np.unique(values, return_counts=True)
    return int(v[np.argmax(n)])


def plan_solid(root, map_ids: list[str], surface: str | None = None, rock: str | None = None) -> dict[str, SolidFill]:
    """Choose each padded map's solid fill: named defs if given (confirmed against the save), else the
    map's most common natural terrain (surface) or compressed thing (underground); MF_SurfaceVoid above."""
    elements, sizes = map_index(root)
    by_id = {mid: el for el, mid in elements.items()}
    present = present_values(root)
    named_terrain = find_def_value(surface, "terrain", present) if surface else None
    named_rock = find_def_value(rock, "thing", present) if rock else None
    plans = {}
    for mid in map_ids:
        sx, sz = sizes[mid]
        arrays = {g.label: g.array(sx, sz) for g in find_grids(by_id[mid]) if not g.path.startswith("areaManager/")}
        natural = np.where(arrays["underGrid"] != 0, arrays["underGrid"], arrays["topGrid"])
        things = arrays["compressedThingMap"]
        role = map_role(root, by_id[mid])
        if role == "upper":
            plans[mid] = SolidFill(role, terrain=find_def_value("MF_SurfaceVoid", "terrain", present), note="MF_SurfaceVoid")
        elif role == "surface":
            t = named_terrain or _most_common(natural[things == 0])
            plans[mid] = SolidFill(role, terrain=t, note=surface or f"most common terrain ({t})")
        else:
            r = named_rock or _most_common(things)
            if not r:
                raise ValueError(f"{mid} has no compressed things to take a rock from; pass --rock")
            # The terrain under that rock, from wherever it lies in the save.
            under_rock = [natural[things == r]] if not named_rock else []
            for m, el in by_id.items():
                if named_rock or m != mid:
                    msx, msz = sizes[m]
                    a = {g.label: g.array(msx, msz) for g in find_grids(el)
                         if g.label in ("topGrid", "underGrid", "compressedThingMap") and not g.path.startswith("areaManager/")}
                    under_rock.append(np.where(a["underGrid"] != 0, a["underGrid"], a["topGrid"])[a["compressedThingMap"] == r])
            t = _most_common(np.concatenate(under_rock))
            plans[mid] = SolidFill(role, terrain=t, rock=r, roof=find_def_value("RoofRockThick", "roof", present),
                                   note=f"{rock or f'most common rock ({r})'} on terrain {t}, thick roof, fogged")
    return plans


def _river(arrays: dict) -> np.ndarray | None:
    """Cells with river flow; None if the map has no flow grid."""
    flow = arrays.get("riverFlowMap")
    return None if flow is None else np.any(flow != 0, axis=-1)


def _solid_source(label: str, path: str, arr: np.ndarray, under: np.ndarray, river, plan: SolidFill):
    """Like `_edge_source` for `--fill solid`: the material, except next to flowing water."""
    if path.startswith("temperatureCache/"):
        return arr
    if plan.role == "upper":
        return np.full_like(arr, plan.terrain) if label == "topGrid" and path.startswith("terrainGrid/") else None
    flowing = river if river is not None else np.zeros(arr.shape[:2], bool)
    if label == "topGrid" and path.startswith("terrainGrid/"):
        return np.where(flowing, np.where(under != 0, under, arr), plan.terrain).astype(arr.dtype)
    if label == "riverFlowMap":
        return arr
    if plan.role == "underground":
        if label == "compressedThingMap":
            return np.where(flowing, 0, plan.rock).astype(arr.dtype)
        if label == "roofs":
            return np.full_like(arr, plan.roof)
        if label == "fogGrid":
            return np.ones_like(arr)
    return None


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


def _fill_grid(arr: np.ndarray, source: np.ndarray | None, pad: Padding) -> np.ndarray:
    """Pad one grid: the old area unchanged, the border from `source`'s nearest edge cell, or empty."""
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


def pad_save(tree, map_ids: list[str], pad: Padding, fill: str = "edge",
             surface: str | None = None, rock: str | None = None) -> PadReport:
    """Pad the chosen maps and their linked maps in place."""
    root = tree.getroot()
    map_els, map_sizes = map_index(root)
    by_id = {mid: el for el, mid in map_els.items()}
    padded = [m for group in select_groups(root, map_ids) for m in group]
    report = PadReport(maps=padded)
    plans = plan_solid(root, padded, surface, rock) if fill == "solid" else {}
    report.notes += [f"{mid} {p.role}: {p.note}" for mid, p in plans.items()]

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
        arrays = {g.label: g.array(sx, sz) for g in grids if not g.path.startswith("areaManager/")}
        under, river = arrays["underGrid"], _river(arrays)
        for g in grids:
            arr = g.array(sx, sz)
            if fill == "solid":
                source = _solid_source(g.label, g.path, arr, under, river, plans[mid])
            else:
                source = _edge_source(g.label, g.path, arr, under)
            g.store(_fill_grid(arr, source, pad))
            grid_elements.add(g.element)
            report.actions[f"grid {fill}" if source is not None else "grid empty"] += 1

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


def _check_border(mid: str, og: list, pg: list, old: tuple, new: tuple, pad: Padding,
                  plan: SolidFill | None = None) -> list[str]:
    """Check border cells without using the fill code: each is its nearest edge cell, the solid
    material, or allowed empty."""
    (sx, sz), (nx, nz) = old, new
    zz, xx = np.meshgrid(np.arange(nz) - pad.south, np.arange(nx) - pad.west, indexing="ij")
    border = (zz < 0) | (zz >= sz) | (xx < 0) | (xx >= sx)
    nearest = np.clip(zz, 0, sz - 1), np.clip(xx, 0, sx - 1)
    under = next(g.array(sx, sz) for g in og if g.label == "underGrid")
    flow = next((g.array(sx, sz) for g in og if g.label == "riverFlowMap"), None)
    flowing = np.zeros(nearest[0][border].shape, bool) if flow is None else np.any(flow[nearest][border] != 0, axis=-1)
    problems = []
    for a, b in zip(og, pg):
        was, now = a.array(sx, sz), b.array(nx, nz)
        edge = was[nearest][border]
        got = now[border]
        terrain = a.label == "topGrid" and a.path.startswith("terrainGrid/")
        if plan is not None and not a.path.startswith("temperatureCache/"):
            underground = plan.role == "underground"
            if plan.role == "upper":
                good = got == (plan.terrain if terrain else 0)
            elif terrain:
                good = np.where(flowing, got == np.where(under[nearest][border] != 0, under[nearest][border], edge),
                                got == plan.terrain)
            elif a.label == "riverFlowMap":
                good = got == edge
            elif a.label == "compressedThingMap" and underground:
                good = got == np.where(flowing, 0, plan.rock)
            elif a.label == "roofs" and underground:
                good = got == plan.roof
            elif a.label == "fogGrid" and underground:
                good = got == True  # noqa: E712 (numpy bool array)
            else:
                good = got == 0
        elif terrain:
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


def verify_pad(orig_tree, padded_tree, map_ids: list[str], pad: Padding, fill: str = "edge",
               surface: str | None = None, rock: str | None = None) -> list[str]:
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

    plans = plan_solid(oroot, padded, surface, rock) if fill == "solid" else {}
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
        problems += _check_border(mid, og, pg, (sx, sz), psizes[mid], pad, plans.get(mid))

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
