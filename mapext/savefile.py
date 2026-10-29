"""Load and save RimWorld save files (.rws).

RimWorld writes saves with .NET's XmlWriter (UTF-8 with BOM, CRLF, tab indentation,
`<empty />`). `write_save` mimics that format so an unmodified tree round-trips
byte-for-byte, which keeps diffs between input and output meaningful.
"""
from pathlib import Path

from lxml import etree

BOM = b"\xef\xbb\xbf"
DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'
NEWLINE = "\r\n"


def load_save(path: str | Path) -> etree._ElementTree:
    parser = etree.XMLParser(huge_tree=True, remove_blank_text=True)
    return etree.parse(str(path), parser)


def _escape_text(s: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # The parser normalizes CRLF to LF; XmlWriter writes newlines in text back as CRLF.
    return s.replace("\n", NEWLINE)


def _escape_attr(s: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return s.replace("\n", "&#xA;").replace("\r", "&#xD;").replace("\t", "&#x9;")


def _write_element(el: etree._Element, depth: int, out: list[str]) -> None:
    indent = "\t" * depth
    attrs = "".join(f' {k}="{_escape_attr(v)}"' for k, v in el.attrib.items())
    children = [c for c in el if isinstance(c.tag, str)]
    if children:
        if el.text and el.text.strip():
            raise ValueError(f"Mixed content not supported at <{el.tag}>")
        out.append(f"{indent}<{el.tag}{attrs}>")
        for c in children:
            out.append(NEWLINE)
            _write_element(c, depth + 1, out)
        out.append(f"{NEWLINE}{indent}</{el.tag}>")
    elif el.text:
        out.append(f"{indent}<{el.tag}{attrs}>{_escape_text(el.text)}</{el.tag}>")
    else:
        out.append(f"{indent}<{el.tag}{attrs} />")


def write_save(tree: etree._ElementTree, path: str | Path) -> None:
    out: list[str] = [DECLARATION, NEWLINE]
    _write_element(tree.getroot(), 0, out)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(BOM)
        f.write("".join(out).encode("utf-8"))
