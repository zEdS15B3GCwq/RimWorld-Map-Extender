# RimWorld Map Extender

Goal: a Python tool that enlarges an existing colony map by rewriting a RimWorld save file (`.rws`, plain XML) offline, instead of resizing a live map in-game.

## Layout

- `include/` (git-ignored; copies, treat as read-only reference unless there is a reason to change them)
  - `2.rws`: sample save, ~48 MB. `2_plans_constructions.rws` adds plans and blueprints; `2_2colonies_MFDRmix.rws` (~71 MB) adds a second colony and mixed MultiFloors/DeepRim chains. Don't read it whole as text; parse with lxml (`huge_tree=True`) or stream with `iterparse`.
  - `Managed/`: game assemblies (`Assembly-CSharp.dll`, Unity modules, etc.).
  - `decompiled/`: ilspycmd output of `Assembly-CSharp.dll` (~9k `.cs` files, namespaces as folders: `Verse/`, `RimWorld/`, ...). Grep this rather than decompiling again.
  - `Data/`: Core + DLC defs (`Core`, `Royalty`, `Ideology`, `Biotech`, `Anomaly`, `Odyssey`), ~490 MB. Grep with narrow globs.
  - `Mods/<packageId>-<steamId>/`: mods whose code may matter for map data. Some ship a `Source/` folder; use it before decompiling.
  - `Mods-decompiled/<Assembly>/`: ilspycmd output of the mods' 1.6 assemblies.
- `mapext/`: the tool. `python -m mapext roundtrip <in> <out>` re-writes a save unchanged and checks it is byte-identical (the serializer mimics .NET XmlWriter: BOM, CRLF, tabs, `<x />`).
  - `python -m mapext census <in> <report.md>` lists every coordinate-like value and grid by path, labelled by the rules in `mapext/census.py`.
  - `python -m mapext grids <in> [--render DIR]` checks every map grid decodes/re-encodes losslessly and renders PNGs.
  - `python -m mapext maps <in>` lists maps per tile, how they connect and which pad together.
  - `python -m mapext pad <in> <out> [--map Map_0] [--all N | --west/--east/--south/--north N]` pads a map and its MultiFloors levels, then verifies the output.
- `plan.md`: plan, findings and progress. Keep it up to date.
- `out/` (git-ignored): generated saves and reports.
- `.venv/`: Python 3.15 with lxml, numpy, pillow. Manage it with `uv pip --python .venv`.
- `dotnet-tools.json`: local tool manifest (ilspycmd). Run as `dotnet tool run ilspycmd ...`.

## Assumptions

- The same mod list is used for the target save and any donor saves.
- Goal is padding the map; decode grid formats only as far as padding needs.

## Rules

- Change things only inside this project folder. No global pip/dotnet installs, and don't read the game install folder; ask the user for any game or mod files you need.
- Never overwrite `include/2.rws`; write modified saves to `out/`.
