"""
main.py
-------
FastAPI app: browse a Rekordbox library/playlists, tag tracks into phase
groups, and (in a later milestone) build sets. Run from the anomaly/
project root:

    python3 -m uvicorn webapp.main:app --reload

Read-only endpoints only so far (see docs/PLAN.md for the full milestone
list) - tags are always resolved from tag_store, but nothing writes yet.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from webapp import config
from webapp.rekordbox_reader import RBNode, flatten_playlists, parse_library, playlist_tracks
from webapp.tag_store import get_tags

app = FastAPI(title="anomaly")


def _load_library() -> tuple[dict, RBNode]:
    return parse_library(config.REKORDBOX_XML_PATH)


def _find_playlist(root: RBNode, playlist_path: str) -> RBNode:
    playlists = dict(flatten_playlists(root))
    node = playlists.get(playlist_path)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Playlist '{playlist_path}' not found")
    return node


@app.get("/api/playlists")
def list_playlists():
    _, root = _load_library()
    return [
        {"path": path, "name": node.name, "count": len(node.track_ids)}
        for path, node in flatten_playlists(root)
    ]


@app.get("/api/playlists/{playlist_path:path}/tracks")
def list_tracks(playlist_path: str):
    collection, root = _load_library()
    node = _find_playlist(root, playlist_path)
    tracks = playlist_tracks(node, collection)
    tag_map = get_tags(config.TAGS_DB_PATH, playlist_path, [t.track_id for t in tracks])

    return [
        {
            "track_id": t.track_id,
            "title": t.name,
            "artist": t.artist,
            "key": f"{t.key[0]}{t.key[1]}" if t.key else None,
            "energy": t.energy,
            "bpm": t.bpm,
            "duration_s": t.total_time_s,
            "genre": t.genre,
            "phase": tag_map.get(t.track_id),
        }
        for t in tracks
    ]
