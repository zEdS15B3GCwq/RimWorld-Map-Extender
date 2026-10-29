"""Per-cell map grids: decode, pad and re-encode.

RimWorld stores grids through `DataExposeUtility.LookByteArray`:
- bytes are raw-deflated (.NET DeflateStream, no zlib header), base64-encoded, wrapped at 100
  chars with a leading and trailing line break, and saved as `<labelDeflate>`. If deflating
  doesn't shrink the data, the plain base64 is saved as `<label>` instead;
- multi-byte cells are little-endian (u16 terrain/roof/snow/..., u32 gas);
- bool grids (`LookBoolArray`: BoolGrid, fog) are bit-packed, LSB first, ceil(N/8) bytes.
Cells are ordered `index = z * sizeX + x`, so a grid is an array of shape (sizeZ, sizeX) with
row 0 at the south edge.

We don't need to know what each value means to pad a grid, only its cell layout, which is
inferred from the decoded length.
"""
import base64
import re
import zlib
from dataclasses import dataclass

import numpy as np
from lxml import etree

DEFLATE_SUFFIX = "Deflate"
_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_DTYPES = {1: np.uint8, 2: np.dtype("<u2"), 4: np.dtype("<u4")}


def decode_blob(text: str, deflated: bool) -> bytes:
    raw = base64.b64decode("".join(text.split()))
    return zlib.decompress(raw, -15) if deflated else raw


def wrap_base64(s: str) -> str:
    """Mimic AddLineBreaksToLongString (newlines are written back as CRLF by the save writer)."""
    return "\n" + "\n".join(s[i:i + 100] for i in range(0, len(s), 100)) + "\n"


def encode_blob(data: bytes) -> tuple[str, bool]:
    """Return (text, deflated) the way LookByteArray would choose them."""
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = c.compress(data) + c.flush()
    if len(packed) < len(data):
        return wrap_base64(base64.b64encode(packed).decode("ascii")), True
    return wrap_base64(base64.b64encode(data).decode("ascii")), False


@dataclass
class Layout:
    """How one grid stores its cells: bit-packed, or `itemsize` bytes per cell."""
    bits: bool
    itemsize: int = 1

    @staticmethod
    def infer(nbytes: int, ncells: int) -> "Layout | None":
        if nbytes == (ncells + 7) // 8 and ncells % 8 != 0 or nbytes == ncells // 8 and ncells % 8 == 0:
            return Layout(bits=True)
        if nbytes % ncells == 0 and nbytes // ncells >= 1:
            return Layout(bits=False, itemsize=nbytes // ncells)
        return None

    def to_array(self, data: bytes, sx: int, sz: int) -> np.ndarray:
        n = sx * sz
        buf = np.frombuffer(data, dtype=np.uint8)
        if self.bits:
            return np.unpackbits(buf, bitorder="little")[:n].astype(bool).reshape(sz, sx)
        if self.itemsize in _DTYPES:
            return np.frombuffer(data, dtype=_DTYPES[self.itemsize]).reshape(sz, sx)
        return buf.reshape(sz, sx, self.itemsize)  # unusual widths: keep raw bytes per cell

    def to_bytes(self, arr: np.ndarray) -> bytes:
        if self.bits:
            return np.packbits(arr.reshape(-1).astype(np.uint8), bitorder="little").tobytes()
        if self.itemsize in _DTYPES:
            return arr.astype(_DTYPES[self.itemsize]).tobytes()
        return arr.astype(np.uint8).tobytes()


def pad_array(arr: np.ndarray, west: int, east: int, south: int, north: int, value=0, mode: str = "constant") -> np.ndarray:
    """Pad a (sizeZ, sizeX[, k]) grid. Row 0 is south, column 0 is west."""
    widths = [(south, north), (west, east)] + [(0, 0)] * (arr.ndim - 2)
    if mode == "edge":
        return np.pad(arr, widths, mode="edge")
    return np.pad(arr, widths, mode="constant", constant_values=value)


@dataclass
class GridRef:
    """One grid in the XML tree. `label` is the LookByteArray label (tag without `Deflate`)."""
    element: etree._Element
    path: str
    label: str
    deflated: bool
    layout: Layout
    data: bytes

    def array(self, sx: int, sz: int) -> np.ndarray:
        return self.layout.to_array(self.data, sx, sz)

    def store(self, arr: np.ndarray) -> None:
        """Re-encode `arr` into the element, renaming it if the deflate choice changes."""
        data = self.layout.to_bytes(arr)
        text, deflated = encode_blob(data)
        self.element.text = text
        self.element.tag = self.label + DEFLATE_SUFFIX if deflated else self.label
        self.deflated, self.data = deflated, data


def _rel_path(el: etree._Element, stop: etree._Element) -> str:
    parts = []
    while el is not None and el is not stop:
        cls = el.get("Class")
        parts.append(f"{el.tag}[{cls}]" if cls else el.tag)
        el = el.getparent()
    return "/".join(reversed(parts))


def map_size(map_el: etree._Element) -> tuple[int, int]:
    sx, _, sz = (int(v) for v in map_el.findtext("mapInfo/size").strip("()").split(","))
    return sx, sz


def find_grids(map_el: etree._Element) -> list[GridRef]:
    """All byte-array grids in one map element whose decoded size fits the map's cell count.

    `things` is skipped: no thing stores a whole-map grid.
    """
    sx, sz = map_size(map_el)
    ncells = sx * sz
    found = []
    for el in map_el.iter():
        if not isinstance(el.tag, str) or len(el) or not el.text or el.tag == "things":
            continue
        if el.getparent() is not None and el.getparent().tag == "things":
            continue
        text = el.text.strip()
        if not text or not _BLOB_RE.match(text):
            continue
        deflated = el.tag.endswith(DEFLATE_SUFFIX)
        if not deflated and len(text) < 64:
            continue
        try:
            data = decode_blob(text, deflated)
        except (ValueError, zlib.error):
            continue
        layout = Layout.infer(len(data), ncells)
        if layout is None:
            continue
        label = el.tag[: -len(DEFLATE_SUFFIX)] if deflated else el.tag
        found.append(GridRef(el, _rel_path(el, map_el), label, deflated, layout, data))
    return found


def find_cell_lists(map_el: etree._Element) -> list[tuple[etree._Element, str]]:
    """Lists with one `<li>` per cell (e.g. MixedBiomeMapComponent/biomeGrid)."""
    sx, sz = map_size(map_el)
    ncells = sx * sz
    out = []
    for el in map_el.find("components"):
        for sub in el.iter():
            if len(sub) == ncells and all(c.tag == "li" for c in sub[:8]):
                out.append((sub, _rel_path(sub, map_el)))
    return out
