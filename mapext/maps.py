"""List a save's maps and how they connect, using the links the save records (not shared tiles).

- type: the world object named by `mapInfo/parent` (Settlement, DeepRim.UndergroundMapParent,
  PocketMap, ...). A settlement is a colony when its faction has the scenario's player faction def.
- MultiFloors: each level's `MF_LevelMapComp/groundMap`; level is absent on the ground map (0).
- DeepRim: the `DeepRim.UndergroundManager/layers` of the map the shaft was dug on (any map type).
"""
from dataclasses import dataclass, field

from .census import map_index
from .pad import linked_maps


@dataclass
class MapInfo:
    id: str
    size: tuple
    kind: str
    tile: str
    label: str = ""
    level: int = 0
    above: str | None = None  # the map this one hangs off in the listing
    below: list = field(default_factory=list)
    pad_group: list = field(default_factory=list)


def _component(map_el, cls: str):
    return next((c for c in map_el.find("components") if c.get("Class") == cls), None)


def _pairs(el) -> dict:
    """A scribed dictionary (`keys/li`, `values/li`) as a dict; {} if `el` is None."""
    if el is None:
        return {}
    return dict(zip((li.text for li in el.iterfind("keys/li")), (li.text for li in el.iterfind("values/li"))))


def _player_faction(game) -> str | None:
    """Id of the faction whose def is the scenario's player faction, e.g. Faction_45."""
    def_name = game.findtext("scenario/playerFaction/factionDef")
    for f in game.find("world/factionManager/allFactions"):
        if f.findtext("def") == def_name:
            return f"Faction_{f.findtext('loadID')}"
    return None


def list_maps(root) -> list[MapInfo]:
    game = root.find("game")
    map_ids, sizes = map_index(root)
    by_id = {mid: el for el, mid in map_ids.items()}
    world_objects = {f"WorldObject_{wo.findtext('ID')}": wo
                     for section in ("worldObjects/worldObjects", "pocketMaps") for wo in game.find(f"world/{section}")}
    player = _player_faction(game)
    infos = {}
    for mid, el in by_id.items():
        wo = world_objects.get(el.findtext("mapInfo/parent"))
        cls, def_name = (wo.get("Class"), wo.findtext("def")) if wo is not None else (None, None)
        comp = _component(el, "MultiFloors.MF_LevelMapComp")
        level = int(comp.findtext("level") or 0) if comp is not None else 0
        ground = comp.findtext("groundMap") if comp is not None else None
        info = MapInfo(mid, sizes[mid], "unknown", wo.findtext("tile") if wo is not None else "?", level=level)
        if cls == "Settlement":
            info.kind = "colony" if wo.findtext("faction") == player else f"settlement of {wo.findtext('faction')}"
            info.label = wo.findtext("nameInt") or ""
        elif cls == "DeepRim.UndergroundMapParent":
            info.kind = f"DeepRim depth {wo.findtext('depth')}"
        elif level != 0:
            info.kind = f"MultiFloors level {level}"
        elif wo is not None:
            info.kind = cls or def_name
        if level != 0 and ground in by_id:
            info.above = ground
        info.pad_group = sorted(set([mid] + linked_maps(el, by_id)), key=list(by_id).index)
        infos[mid] = info
    # DeepRim layers name the world object of each underground map; the manager's map is its surface.
    parent_to_map = {el.findtext("mapInfo/parent"): mid for mid, el in by_id.items()}
    for mid, el in by_id.items():
        manager = _component(el, "DeepRim.UndergroundManager")
        if manager is None:
            continue
        names = _pairs(manager.find("layerNames"))
        for depth, wo_id in _pairs(manager.find("layers")).items():
            child = parent_to_map.get(wo_id)
            if child:
                infos[child].above = mid
                infos[child].label = names.get(depth, "")
    for info in infos.values():
        if info.above:
            infos[info.above].below.append(info.id)
    return list(infos.values())


def format_maps(infos: list[MapInfo]) -> str:
    by_id = {i.id: i for i in infos}
    lines = []

    def emit(info, depth):
        label = f' "{info.label}"' if info.label else ""
        group = [m for m in info.pad_group if m != info.id]
        pads = f"  [pads with {', '.join(group)}]" if group else ""
        size = f"{info.size[0]}x{info.size[1]}"
        lines.append(f"{'  ' * (depth + 1)}{info.id:<8} {size:>9}  {info.kind}{label}{pads}")
        for child in info.below:
            emit(by_id[child], depth + 1)

    tiles = list(dict.fromkeys(i.tile for i in infos))
    for tile in tiles:
        lines.append(f"Tile {tile}")
        for info in infos:
            if info.tile == tile and not info.above:
                emit(info, 0)
    return "\n".join(lines)
