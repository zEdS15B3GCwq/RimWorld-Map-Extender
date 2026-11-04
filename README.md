# RimWorld Map Extender

A command-line tool that makes an existing RimWorld map bigger by rewriting the save file offline. You choose a map and how many cells to add on each side. The tool pads the map, shifts everything on it so it stays where it was, fills the new border and checks the result before you load it.

It works on RimWorld 1.6 saves and supports maps linked by the [MultiFloors](https://steamcommunity.com/sharedfiles/filedetails/?id=3384660931) and [DeepRim](https://steamcommunity.com/sharedfiles/filedetails/?id=2164179860) mods.

## Disclaimer (human-written section)

All the code and documentation in this project was created and managed by Claude Opus 5.5 (see Generative AI use section below). One of my main goals was to see how Opus would perform in a hands-off project. Therefore, while I have run tests in RimWorld to confirm that the tool is, indeed, working, I did not check the source code or the documentation carefully. Be aware that there may be undiscovered bugs in the code or errors in the documentation. Pull requests are welcome.

## What it does

- **Pads one or more maps** by any number of cells on the west, east, south and north sides.
- **Shifts every reference to a cell** so it stays in place: buildings, pawns, items, blueprints, plans, zones, areas, jobs, quest locations, the camera, DeepRim shaft positions and more.
- **Keeps linked maps consistent.** All MultiFloors levels of a building share coordinates, so choosing any level pads the whole group. DeepRim layers are separate maps and are only padded when you choose them.
- **Fills the new border** in one of two ways (see [Fill modes](#fill-modes)).
- **Verifies the output** by reading it back and checking that:
  - maps you didn't choose are unchanged;
  - every grid crops back to the original;
  - the border follows the fill rules;
  - every coordinate moved exactly once.
- **Never changes the input file.** It writes a new save.

## Requirements

- Python 3.10 or later (developed and tested on 3.15 only)
- `lxml`, `numpy` and `pillow` (`pillow` is only used for the `grids --render` diagnostic)
- The same mod list in the game as in the save

Install the dependencies into a virtual environment:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

On Linux and macOS the interpreter is `.venv/bin/python`.

## Quick start

1. **Back up your save.** On Windows, saves are in `%USERPROFILE%\AppData\LocalLow\Ludeon Studios\RimWorld by Ludeon Studios\Saves`.
2. **List the maps in the save** and note the id of the map you want to extend:

   ```bash
   python -m rwmapext maps "Green Valley.rws"
   ```

   ```
   World "Nair": seed "planetary", coverage 0.5, rainfall Normal, temperature Normal, pollution 0.05
   Tile 127024,0
     Map_0      275x275  colony "Green Valley"  seed -1123258666  [pads with Map_25, Map_29]
       Map_23     100x100  DeepRim depth 7 "COL-DR7"  seed -1123258666  [pads with Map_27]
         Map_27     100x100  MultiFloors level -1  seed -1173494294  [pads with Map_23]
       Map_25     275x275  MultiFloors level -1  seed -1173494295  [pads with Map_0, Map_29]
         Map_26     150x150  DeepRim depth 1 "MF-DR1"  seed -1123258666
       Map_29     275x275  MultiFloors level 1  seed -1173494290  [pads with Map_0, Map_25]
   Tile 61273,0
     Map_28     275x275  colony "Brío Tarpit"  seed -1147478915
   ```

3. **Extend it** into a new file:

   ```bash
   python -m rwmapext extend "Green Valley.rws" "Green Valley 315.rws" --map Map_0 --all 20
   ```

4. **Put the new file in the saves folder and load it** in RimWorld with the same mods.

## Commands

### `maps <save>`

Lists the save's maps per world tile as a tree of how they connect: DeepRim layers under the map their shaft was dug on, and MultiFloors levels under their ground map. For each map it shows:
- its id, size and type: colony, other faction's settlement, DeepRim layer or MultiFloors level;
- its name, if it has one;
- its generation seed;
- the other maps that are always padded with it.

The first line shows the world settings, which you need to recreate the world in a new game. Map seeds are worked out from the game's code; the save doesn't store them.

### `extend <input> <output> --map ID [options]`

| Option | Meaning |
|---|---|
| `--map ID` | Map to extend, as listed by `maps`, e.g. `Map_0`. Repeat to extend several maps in one run. Required. |
| `--all N` | Cells to add on every side not given separately. Default 0. |
| `--west N`, `--east N`, `--south N`, `--north N` | Cells to add on one side. These override `--all` for that side. |
| `--fill edge` \| `--fill solid` | How to fill the new border. Default `edge`. |
| `--surface DEF` | With `--fill solid`: terrain for surface maps, as a def name, e.g. `Gravel`. |
| `--rock DEF` | With `--fill solid`: rock for underground maps, as a def name, e.g. `Granite`. |
| `--no-verify` | Skip the check of the written file. |

One run applies the same padding to every chosen map. For different sizes per map, run the tool several times, using each output as the next input.

The tool prints each group of maps and its new size, what it filled each map's border with, and a summary of what it changed. Exit code 0 means the output was written and verified, 1 means verification found a problem and 2 means the input was rejected before anything was written.

### Diagnostics

| Command | Use |
|---|---|
| `roundtrip <input> <output>` | Re-writes a save unchanged and checks it's byte-identical to the input. |
| `census <input> <report.md>` | Lists every coordinate-like value and grid in the save by XML path, with the rule the tool applies to it. |
| `grids <input> [--render DIR]` | Checks every map grid decodes and re-encodes losslessly. With `--render`, also writes one PNG per grid per map. |

## Fill modes

### `--fill edge` (default)

Every border cell copies the nearest cell on the old edge of the map, so features at the edge continue in straight lines.

| Copied | Not copied |
|---|---|
| Natural terrain (for a built floor, the terrain under it) | Built floors and bridges |
| Natural rock roofs | Constructed roofs |
| Everything in the map's compressed thing grid: rock, ore, chunks | Plants, animals, items and buildings |
| Fog, river flow, temperature | Snow, sand, gas, pollution, deep resources |

Smoothed stone floors are copied as they are. Mined tunnels that reach the edge continue into the border.

### `--fill solid`

The border gets one material, chosen by the type of map:

| Map type | Border | Default material |
|---|---|---|
| Surface (colonies, other settlements) | `--surface` terrain, no roof, no fog | The map's most common natural terrain on cells without rock |
| Underground (DeepRim layers, MultiFloors levels below ground) | `--rock`, with thick rock roof, fogged | The map's most common rock |
| MultiFloors levels above ground | Unsupported area (`MF_SurfaceVoid`) | — |

- **Terrain under rock:** the tool uses whatever terrain lies under that rock elsewhere in the save, e.g. rough granite under `Granite`.
- **Rivers:** border cells next to an edge cell with flowing water keep that water and its flow, so rivers and anything floating on them keep moving. Still water, such as lakes and ocean, gets the fill material.

**Material names** are def names, not the labels shown in game. The dev mode inspector shows the def name of whatever is under the cursor. The save stores a short hash for each def, and that hash depends on the mod list. So the tool accepts a name only if it finds that material somewhere in the save, and stops with an error otherwise. Examples:

- terrain: `Soil`, `SoilRich`, `Gravel`, `Sand`, `MossyTerrain`, `MarshyTerrain`, `Mud`, `Ice`, `Granite_Rough`, `Slate_RoughHewn`
- rock: `Sandstone`, `Granite`, `Limestone`, `Slate`, `Marble`

Any compressed thing in the save is accepted as rock, so an ore such as `MineableSteel` also works.

## Examples

Add 20 cells on every side of the colony map and its MultiFloors levels:

```bash
python -m rwmapext extend in.rws out.rws --map Map_0 --all 20
```

Add 10 cells to the east and 20 to the north of two separate groups in one run:

```bash
python -m rwmapext extend in.rws out.rws --map Map_0 --map Map_23 --east 10 --north 20
```

Extend a DeepRim layer only:

```bash
python -m rwmapext extend in.rws out.rws --map Map_26 --west 5 --south 4
```

Fill the border with gravel on the surface and granite underground:

```bash
python -m rwmapext extend in.rws out.rws --map Map_0 --all 20 --fill solid --surface Gravel --rock Granite
```

## Limitations

- **Simple border content.** Edge fill gives straight streaks, solid fill gives a uniform material. Both are a base you can reshape in game with dev mode tools. Filling the border from a map generated by the game isn't supported. The game generates a larger map from the same south-west corner, so it can't line up along every edge.
- **Mod coverage.** The tool knows the save data of RimWorld 1.6, its DLCs and the mods it was developed with. If a save holds coordinate data the tool has no rule for, `extend` stops before changing anything and lists the unknown paths. Supporting them means adding rules to `rwmapext/census.py`.
- **Tested cases.** Tested in game:
  - colony maps;
  - MultiFloors levels above and below ground;
  - DeepRim layers, including ones built off MultiFloors levels;
  - two colonies in one save.

  Not tested: maps of other factions' settlements and quest maps.
- **Empty border at first:** new border cells hold no plants, animals or deep resources. Plants grow back over time where the soil allows.
- **DeepRim lifts:** DeepRim places a new lift at the shaft's coordinates on the layer below without checking that layer's size. After padding a surface map but not the layer under it, a shaft in the border could point outside that layer. This is DeepRim's own behaviour, and untested.

## How it works

A RimWorld save is XML. Most per-cell map data, such as terrain, roofs, fog and the rock grid, is stored as base64-encoded, deflate-compressed byte arrays. The tool:
1. loads the save with `lxml`;
2. decodes each grid, pads it and fills the border;
3. shifts every coordinate value on the chosen maps, and world-level values that point into them;
4. writes the save back in the exact format the game's .NET XML writer uses.

An unchanged save re-written by the tool is byte-identical to the original. The verifier then compares the new save against the original.

## Development environment

You only need this to change the tool, for example to add rules for other mods. Running it needs nothing beyond [Requirements](#requirements). The game and mod files are copyrighted, so they aren't in the repository: `include/` is git-ignored, and you fill it from your own installation.

| Folder | Content | Copy from |
|---|---|---|
| `include/Managed/` | Game assemblies (`Assembly-CSharp.dll`, Unity modules, ...) | `RimWorldWin64_Data/Managed/` in the game folder |
| `include/Data/` | Core and DLC defs (`Core`, `Royalty`, `Ideology`, `Biotech`, `Anomaly`, `Odyssey`) | `Data/` in the game folder |
| `include/Mods/<packageId>-<steamId>/` | Mods whose code matters for map data (see [Mods](#mods)) | Steam Workshop folder `steamapps/workshop/content/294100/<steamId>/` |
| `include/*.rws` | Sample saves | Your saves folder |

The game and mod code was decompiled with [ILSpy](https://github.com/icsharpcode/ILSpy)'s command line tool `ilspycmd`, version 11.2.0.10050. The version is pinned in `dotnet-tools.json` as a project-local .NET tool, so nothing is installed globally. It needs the .NET SDK.

```bash
dotnet tool restore
dotnet tool run ilspycmd -p -o include/decompiled include/Managed/Assembly-CSharp.dll
```

This writes a C# project with one folder per namespace (`Verse/`, `RimWorld/`, `RimWorld.Planet/`, ...), about 9,000 files.

Each mod assembly goes into its own folder under `include/Mods-decompiled/`. The game assemblies, the mod's own assembly folder and Vanilla Expanded Framework's assemblies are passed as references, because many mods depend on it:

```bash
M=include/Mods
O=include/Mods-decompiled
VEF=$M/OskarPotocki.VanillaFactionsExpanded.Core-2023507013/1.6/Assemblies
mkdir -p $O
for dll in \
  $M/Mlie.DeepRim-2164179860/1.6/Assemblies/DeepRim.dll \
  $M/telardo.MultiFloors-3384660931/1.6/Assemblies/MultiFloors.dll \
  $M/telardo.MultiFloors-3384660931/1.6/ModCompat/VEF/Assemblies/MultiFloorsVEFCompat.dll; do
  n=$(basename $dll .dll)
  dotnet tool run ilspycmd -p -o $O/$n -r include/Managed -r $(dirname $dll) -r $VEF $dll > /dev/null 2> $O/$n.err
done
```

Add each mod assembly you need to the list. Mods that ship a `Source/` folder don't need decompiling.

The Python environment used during development:

```bash
uv venv .venv --python 3.15
uv pip --python .venv install -r requirements.txt
```

## Mods

These mods were present during development and found to matter for extending maps. Each was checked against its code and the sample saves:

| Mod | Steam id | Why it matters |
|---|---|---|
| [MultiFloors](https://steamcommunity.com/sharedfiles/filedetails/?id=3384660931) | 3384660931 | Each building level is a separate map sharing the ground map's coordinates, linked by stairs. All levels of a group are padded together; upper levels are filled with unsupported area (`MF_SurfaceVoid`). |
| [DeepRim](https://steamcommunity.com/sharedfiles/filedetails/?id=2164179860) | 2164179860 | Underground layers are separate maps linked by shafts and lifts. Its world objects store the shaft position on the layer below (`holeLocation`), which shifts with that layer. |
| [Vanilla Expanded Framework](https://steamcommunity.com/sharedfiles/filedetails/?id=2023507013) | 2023507013 | Its pipe networks (PipeSystem) and other map components are saved on each map. Their cell coordinates shift with the map like the game's own. |
| [Some Things Float](https://steamcommunity.com/sharedfiles/filedetails/?id=2940072380) | 2940072380 | Floats items down rivers using river flow, which is why both fill modes keep flowing water and its flow in the border. |
| [ReGrowth: Core](https://steamcommunity.com/sharedfiles/filedetails/?id=2260097569) | 2260097569 | Patches how fertility is calculated from terrain. Fertility isn't stored, so copying terrain is enough. Its farming data is keyed by zone ID, not by cell. |
| [Adaptive Storage Framework](https://steamcommunity.com/sharedfiles/filedetails/?id=3033901359) | 3033901359 | Saves lists of integer positions; these are slots inside a storage building, not map cells, so they stay unchanged. |
| [RimLogic](https://steamcommunity.com/sharedfiles/filedetails/?id=3750951495) | 3750951495 | Saves cell coordinates on its buildings, which shift with the map. |
| [Rimatomics](https://steamcommunity.com/sharedfiles/filedetails/?id=1127530465) | 1127530465 | Saves cell coordinates on its buildings, which shift with the map. Its missiles' world-level target cells have no rule yet, so a save with a missile in flight stops `extend` with an unknown path. |
| Soil Relocation Framework (Continued) | — | Makes soil removable and movable. It was active in the sample saves and adds no extra map data, so it needs no special handling. |

## Generative AI use

This tool was designed and built with Claude Opus 5.5 (Anthropic) as the main engineer. Claude did most of the work:
- **Reverse-engineering:** worked out the save format and map logic from the decompiled game and mod code, including:
  - how grids are encoded;
  - how coordinates, cells and areas are stored;
  - how def hashes are assigned;
  - how map seeds are derived;
  - how MultiFloors and DeepRim link their maps.
- **Data survey:** catalogued every coordinate-bearing value in the sample saves.
- **Planning:** drew up the implementation plan and kept it up to date.
- **Implementation:** wrote the code for the serializer, grid codecs, padding, fill modes, verification and command line.
- **Testing:** tested each step offline.
- **Documentation:** wrote this README and the project's plan and notes.
- **Version control:** managed the git commits.

The project owner set the goals and constraints, made the design decisions, supplied the game and mod files and test saves, and tested every change in game.

No artwork was generated by AI.
