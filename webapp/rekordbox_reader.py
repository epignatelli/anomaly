"""
rekordbox_reader.py
--------------------
Read-only parser for a Rekordbox XML library export (the structural inverse
of set_builder.write_rekordbox_xml): COLLECTION -> {TrackID: RBTrack}, and
PLAYLISTS -> a folder/playlist tree.

This is v1 data-source scope: it reads a manually re-exported static XML
file, not Rekordbox's live (encrypted) database. See docs/PLAN.md.

Note: this only handles the "Comments-embedded energy, Tonality-or-Comments
key" schema real rekordbox.xml exports use (confirmed against the project's
actual library export). The alternate rekordbox_mikcues_001.xml schema
(POSITION_MARK-based energy) is a different format and is NOT handled here.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plot_set_energy import parse_comment, parse_key  # noqa: E402

Key = Tuple[int, str]


@dataclass
class RBTrack:
    track_id: str
    name: str
    artist: str
    genre: str
    comments: str
    key: Optional[Key]
    energy: Optional[int]
    bpm: Optional[float]
    total_time_s: int
    location: str

    @property
    def path(self) -> Path:
        """Location as a real local filesystem path."""
        return Path(unquote(self.location.replace("file://localhost", "")))


@dataclass
class RBNode:
    name: str
    node_type: str  # "0" = folder, "1" = playlist
    children: List["RBNode"] = field(default_factory=list)
    track_ids: List[str] = field(default_factory=list)  # only meaningful for playlists


def _parse_track(el: ET.Element) -> RBTrack:
    comments = el.get("Comments", "") or ""

    key = parse_key(el.get("Tonality", "") or "")
    if key is None:
        key = parse_key(comments)

    energy_raw = parse_comment(comments) if comments else -1
    energy = energy_raw if energy_raw >= 0 else None

    try:
        bpm = float(el.get("AverageBpm"))
    except (TypeError, ValueError):
        bpm = None

    try:
        total_time_s = int(el.get("TotalTime", "0") or "0")
    except ValueError:
        total_time_s = 0

    return RBTrack(
        track_id=el.get("TrackID", ""),
        name=el.get("Name", "") or "",
        artist=el.get("Artist", "") or "",
        genre=el.get("Genre", "") or "",
        comments=comments,
        key=key,
        energy=energy,
        bpm=bpm,
        total_time_s=total_time_s,
        location=el.get("Location", "") or "",
    )


def _parse_node(el: ET.Element) -> RBNode:
    node_type = el.get("Type", "0")
    node = RBNode(name=el.get("Name", ""), node_type=node_type)
    if node_type == "1":
        node.track_ids = [t.get("Key", "") for t in el.findall("TRACK")]
    else:
        node.children = [_parse_node(child) for child in el.findall("NODE")]
    return node


def parse_library(xml_path: str | Path) -> Tuple[Dict[str, RBTrack], RBNode]:
    """Parse a Rekordbox XML export into (collection, playlist_tree_root)."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    collection_el = root.find("COLLECTION")
    collection: Dict[str, RBTrack] = {}
    if collection_el is not None:
        for track_el in collection_el.findall("TRACK"):
            track = _parse_track(track_el)
            if track.track_id:
                collection[track.track_id] = track

    playlists_el = root.find("PLAYLISTS")
    root_node_el = playlists_el.find("NODE") if playlists_el is not None else None
    playlist_root = _parse_node(root_node_el) if root_node_el is not None else RBNode(name="ROOT", node_type="0")

    return collection, playlist_root


def flatten_playlists(root: RBNode, prefix: str = "") -> List[Tuple[str, RBNode]]:
    """Depth-first walk yielding (display_path, playlist_node) for every
    playlist (Type=1) node, e.g. ('Techno/bangers', node)."""
    results: List[Tuple[str, RBNode]] = []
    for child in root.children:
        path = f"{prefix}/{child.name}" if prefix else child.name
        if child.node_type == "1":
            results.append((path, child))
        else:
            results.extend(flatten_playlists(child, path))
    return results


def playlist_tracks(playlist_node: RBNode, collection: Dict[str, RBTrack]) -> List[RBTrack]:
    """Resolve a playlist node's TRACK Key refs to full RBTrack objects, in
    playlist order. Track IDs with no matching COLLECTION entry are skipped."""
    return [collection[tid] for tid in playlist_node.track_ids if tid in collection]


if __name__ == "__main__":
    # Quick manual smoke test: python3 webapp/rekordbox_reader.py path/to/rekordbox.xml
    if len(sys.argv) != 2:
        print("Usage: python3 rekordbox_reader.py path/to/rekordbox.xml")
        raise SystemExit(1)
    collection, playlist_root = parse_library(sys.argv[1])
    print(f"Collection: {len(collection)} tracks")
    playlists = flatten_playlists(playlist_root)
    print(f"Playlists: {len(playlists)}")
    for path, node in playlists[:15]:
        tracks = playlist_tracks(node, collection)
        with_key_energy = sum(1 for t in tracks if t.key and t.energy is not None)
        print(f"  {path:40s} entries={len(node.track_ids):4d}  resolved={len(tracks):4d}  key+energy={with_key_energy:4d}")
