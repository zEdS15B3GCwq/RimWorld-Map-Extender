"""Coordinate census: find every value in a save that looks like a map cell reference or per-cell grid.

Values are grouped by a normalized XML path:
- the per-map prefix `game/maps/li` becomes `MAP`;
- `li` indices are dropped and each element carries its `Class` attribute in brackets;
- every thing (an element with `<def>` and `<id>` children), at any nesting depth, restarts the
  path at `THING`, so a pawn's fields look the same on the map, in a casket or in a container.

Each group is then labelled by the first matching rule in `RULES` (see there for the reasoning).
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from lxml import etree

INT = r"(-?\d+)"
FLOAT = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
PATTERNS = [
    # (kind, regex). Order matters: the first match wins.
    ("IntVec3+map", re.compile(rf"^\({INT}, {INT}, {INT}\), (\S+)$")),  # TargetInfo / GlobalTargetInfo
    ("IntVec3", re.compile(rf"^\({INT}, {INT}, {INT}\)$")),
    ("CellRect", re.compile(rf"^\({INT},{INT},{INT},{INT}\)$")),
    ("IntVec2", re.compile(rf"^\({INT}, {INT}\)$")),
    ("Vector3", re.compile(rf"^\({FLOAT}, {FLOAT}, {FLOAT}\)$")),
    ("Vector2", re.compile(rf"^\({FLOAT}, {FLOAT}\)$")),
]
BLOB_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
# Plain ints under a suspicious tag name may be cell indices.
SUSPICIOUS_TAG_RE = re.compile(r"(?i)(cell|index|idx|^pos|loc|coord)")
SIZE_TAG_RE = re.compile(r"^mapSize[XZ]$")  # BoolGrid dimensions
INT_RE = re.compile(r"^-?\d+$")
INVALID_IV3 = (-1000, -1000, -1000)

# (kind regex, path regex, action, reason). First match wins; anything unmatched is "unknown".
# Actions: shift = add pad offset to x/z; shift-if-map = shift when the referenced map is the one being
# extended; grid = pad a per-cell array; size = replace with the new map size; keep = leave unchanged.
RULES = [
    # --- per-map data ---
    (r"IntVec3", r"^MAP/mapInfo/size$", "size", "map dimensions"),
    (r"IntVec3", r"/(relativeCells|engineToCenter)(/|$)", "keep", "MultiFloors gravship offsets, relative to an anchor"),
    (r"IntVec3|CellRect|IntVec2|Vector3", r"^MAP/", "shift", "cell or world-space position on this map"),
    (r"blob|cell-list", r"^MAP/", "grid", "per-cell array (base64+deflate, or one <li> per cell)"),
    (r"size-int", r"^MAP/", "size", "BoolGrid width/height"),
    (r"int\?", r"^MAP/", "keep", "reviewed: body-part/toil/stage indices, graphic seeds, cycle counters, cell counts"),
    # --- world-level data that can point into a map ---
    (r"IntVec3\+map", r".", "shift-if-map", "TargetInfo carrying its map id"),
    (r"IntVec3|Vector3", r"THING/", "keep", "stale position of an unspawned thing (world pawn, outpost, trader, quest item)"),
    (r"IntVec3", r"/verbTracker/verbs/li[^/]*/current(Target|Destination)$", "keep", "verb target of an unspawned pawn snapshot"),
    (r"IntVec3", r"^game/questManager/", "shift-if-map", "quest part cell on the quest's map"),
    (r"Vector3", r"^game/cameraMap/camRootPos$", "shift-if-map", "camera on the current map (cosmetic)"),
    (r"IntVec3", r"^game/world/info/initialMapSize$", "keep", "default size for new maps"),
    (r"IntVec3", r"/li\[DeepRim\.UndergroundMapParent\]/holeLocation$", "keep", "cell on the underground map"),
    (r"IntVec3|Vector3", r"^game/world/(grid|features)/", "keep", "world-sphere coordinates"),
    (r"blob", r"^game/(world/grid|history)/", "keep", "world tile grids, history records"),
    (r"int\?", r".", "keep", "reviewed: indices unrelated to map cells"),
]
_RULES = [(re.compile(k), re.compile(p), a, why) for k, p, a, why in RULES]


def classify_rule(path: str, kind: str) -> tuple[str, str]:
    base_kind = kind.split("[")[0]
    for krx, prx, action, why in _RULES:
        if krx.fullmatch(base_kind) and prx.search(path):
            return action, why
    return "unknown", ""


@dataclass
class Group:
    kind: str
    count: int = 0
    maps: set = field(default_factory=set)
    classes: Counter = field(default_factory=Counter)
    samples: list = field(default_factory=list)
    invalid: int = 0
    # Bounds over valid values: x and z (or first/second component).
    lo: list = field(default_factory=lambda: [None, None])
    hi: list = field(default_factory=lambda: [None, None])
    out_of_bounds: int = 0

    def add(self, value, map_id, thing_class, xz, in_bounds):
        self.count += 1
        self.maps.add(map_id)
        if thing_class:
            self.classes[thing_class] += 1
        if len(self.samples) < 3 and value not in self.samples:
            self.samples.append(value if len(value) < 50 else value[:47] + "...")
        if xz is None:
            return
        for i, v in enumerate(xz):
            self.lo[i] = v if self.lo[i] is None else min(self.lo[i], v)
            self.hi[i] = v if self.hi[i] is None else max(self.hi[i], v)
        if in_bounds is False:
            self.out_of_bounds += 1


def _segment(el) -> str:
    cls = el.get("Class")
    return f"{el.tag}[{cls}]" if cls else el.tag


def _is_thing(el) -> bool:
    return el.find("def") is not None and el.find("id") is not None


def run_census(tree) -> tuple[dict, dict]:
    """Return (groups keyed by (path, kind), {map_id: (sizeX, sizeZ)})."""
    root = tree.getroot()
    map_ids, map_sizes = {}, {}
    for m in root.find("game/maps"):
        mid = f"Map_{m.findtext('uniqueID')}"
        sx, _, sz = (int(v) for v in m.findtext("mapInfo/size").strip("()").split(","))
        map_ids[m] = mid
        map_sizes[mid] = (sx, sz)
    cell_counts = {sx * sz for sx, sz in map_sizes.values()}
    groups: dict[tuple[str, str], Group] = {}

    def record(path, kind, value, map_id, thing_class, xz=None):
        g = groups.get((path, kind))
        if g is None:
            g = groups[(path, kind)] = Group(kind)
        in_bounds = None
        if xz is not None and map_id in map_sizes:
            sx, sz = map_sizes[map_id]
            in_bounds = 0 <= xz[0] < sx and 0 <= xz[1] < sz
        g.add(value, map_id, thing_class, xz, in_bounds)
        return g

    def classify_value(path, text, map_id, thing_class):
        for kind, rx in PATTERNS:
            m = rx.match(text)
            if not m:
                continue
            if kind in ("IntVec3+map", "IntVec3"):
                x, y, z = int(m[1]), int(m[2]), int(m[3])
                target = map_id
                if kind == "IntVec3+map" and m[4].startswith("Map_"):
                    target = m[4]
                if (x, y, z) == INVALID_IV3:
                    record(path, kind, text, target, thing_class).invalid += 1
                else:
                    record(path, kind, text, target, thing_class, (x, z))
            elif kind == "CellRect":
                record(path, kind, text, map_id, thing_class, (int(m[1]), int(m[2])))
            elif kind == "IntVec2":
                record(path, kind, text, map_id, thing_class, (int(m[1]), int(m[2])))
            elif kind == "Vector3":
                record(path, kind, text, map_id, thing_class, (float(m[1]), float(m[3])))
            else:
                record(path, kind, text, map_id, thing_class, (float(m[1]), float(m[2])))
            return
        tag = path.rsplit("/", 1)[-1].split("[", 1)[0]
        # LookByteArray writes "<label>Deflate"; all-zero grids compress to just a few dozen chars.
        if BLOB_RE.match(text) and (tag.endswith("Deflate") or len(text) >= 64 and ("\n" in text or len(text) >= 200)):
            record(path, "blob", f"{len(text)} chars", map_id, thing_class)
            return
        if INT_RE.match(text):
            if SIZE_TAG_RE.match(tag):
                record(path, "size-int", text, map_id, thing_class)
            elif SUSPICIOUS_TAG_RE.search(tag):
                record(path, "int?", text, map_id, thing_class)

    def walk(el, path, map_id, thing_class):
        for c in el:
            if not isinstance(c.tag, str):
                continue
            cmap, cclass = map_id, thing_class
            if c in map_ids:
                cmap, cpath = map_ids[c], "MAP"
            elif _is_thing(c):
                cclass = c.get("Class") or c.tag
                cpath = path if path.endswith("THING") else f"{path}/THING"
                # Nested things fold into the outermost THING row.
                if "THING" in path:
                    cpath = path[: path.index("THING") + len("THING")]
            else:
                cpath = f"{path}/{_segment(c)}" if path else _segment(c)
            if len(c):
                n_li = sum(1 for _ in c.iterchildren("li"))
                if n_li in cell_counts:
                    record(cpath, f"cell-list[{n_li}]", f"{n_li} <li>", cmap, cclass)
                    continue
                walk(c, cpath, cmap, cclass)
                continue
            text = (c.text or "").strip()
            if text:
                classify_value(cpath, text, cmap, cclass)

    walk(root, "", "world", None)
    return groups, map_sizes


def format_report(groups: dict, map_sizes: dict) -> str:
    rows = []
    for (path, kind), g in groups.items():
        action, why = classify_rule(path, kind)
        rows.append((action, path, kind, g, why))
    order = {"unknown": 0, "size": 1, "grid": 2, "shift": 3, "shift-if-map": 4, "keep": 5}
    rows.sort(key=lambda r: (order.get(r[0], 9), r[1], r[2]))

    counts = Counter(r[0] for r in rows)
    lines = [
        "# Coordinate census",
        "",
        "Maps: " + ", ".join(f"{k} {v[0]}x{v[1]}" for k, v in map_sizes.items()),
        "",
        "Actions: " + ", ".join(f"{a}: {counts[a]} paths" for a in sorted(counts, key=lambda a: order.get(a, 9))),
        "",
    ]
    current = None
    for action, path, kind, g, why in rows:
        if action != current:
            current = action
            lines += ["", f"## {action}", "", "| path | kind | n | maps | range x / z | inv | OOB | thing classes | samples | reason |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
        rng = "" if g.lo[0] is None else f"{g.lo[0]:g}..{g.hi[0]:g} / {g.lo[1]:g}..{g.hi[1]:g}"
        classes = ", ".join(f"{c} ({n})" for c, n in g.classes.most_common(3))
        if len(g.classes) > 3:
            classes += f", +{len(g.classes) - 3}"
        samples = "; ".join(f"`{s}`" for s in g.samples)
        lines.append(f"| `{path}` | {kind} | {g.count} | {','.join(sorted(g.maps))} | {rng} | {g.invalid or ''} | "
                     f"{g.out_of_bounds or ''} | {classes} | {samples} | {why} |")
    return "\n".join(lines) + "\n"
