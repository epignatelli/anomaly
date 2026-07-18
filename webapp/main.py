"""
main.py
-------
FastAPI app: browse a Rekordbox library/playlists, tag tracks into phase
groups, and (in a later milestone) build sets. Run from the anomaly/
project root:

    python3 -m uvicorn webapp.main:app --reload

See docs/PLAN.md for the full milestone list.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from set_builder import (  # noqa: E402
    DEFAULT_SHAPE,
    BuildError,
    achieved_energy_curve,
    fmt_key,
    resolve_track_paths,
    transition_penalty,
    write_playlist_folder,
    write_rekordbox_xml,
)

from webapp import build_service, config, settings_store
from webapp.rekordbox_reader import RBNode, flatten_playlists, parse_library, playlist_tracks
from webapp.tag_store import get_tags, set_global_default, set_tag

app = FastAPI(title="anomaly")


class TagUpdate(BaseModel):
    phase: Optional[str] = None


class SettingsUpdate(BaseModel):
    rekordbox_xml_path: str


class BuildRequest(BaseModel):
    playlist_path: str
    num_tracks: Optional[int] = None
    target_minutes: Optional[float] = None
    pins: List[str] = []
    shape: str = DEFAULT_SHAPE
    use_phase_tags: bool = False
    phase_shape: Optional[List[float]] = None
    key_strict: bool = False
    key_weight: float = 1.0
    key_energy_blend: float = 0.5
    iterations: int = 20000


class ExportRequest(BaseModel):
    kind: str  # "rekordbox_xml" | "playlist_folder"
    path: str
    playlist_name: Optional[str] = None


def _load_library() -> tuple[dict, RBNode]:
    return parse_library(settings_store.get_rekordbox_xml_path())


def _find_playlist(root: RBNode, playlist_path: str) -> RBNode:
    playlists = dict(flatten_playlists(root))
    node = playlists.get(playlist_path)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Playlist '{playlist_path}' not found")
    return node


@app.get("/api/settings")
def get_settings():
    path = settings_store.get_rekordbox_xml_path()
    return {"rekordbox_xml_path": str(path), "exists": path.is_file()}


@app.put("/api/settings")
def update_settings(body: SettingsUpdate):
    try:
        settings_store.set_rekordbox_xml_path(body.rekordbox_xml_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_settings()


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


@app.put("/api/playlists/{playlist_path:path}/tags/{track_id}", status_code=204)
def set_track_tag(
    playlist_path: str,
    track_id: str,
    body: TagUpdate,
    scope: str = Query("playlist", pattern="^(playlist|global)$"),
):
    try:
        if scope == "global":
            set_global_default(config.TAGS_DB_PATH, track_id, body.phase)
        else:
            set_tag(config.TAGS_DB_PATH, playlist_path, track_id, body.phase)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/build")
def build(req: BuildRequest):
    try:
        build_id, result, phase_groups = build_service.run_build(
            req.playlist_path,
            num_tracks=req.num_tracks,
            target_minutes=req.target_minutes,
            pins=req.pins,
            shape=req.shape,
            use_phase_tags=req.use_phase_tags,
            phase_shape=req.phase_shape,
            key_strict=req.key_strict,
            key_weight=req.key_weight,
            key_energy_blend=req.key_energy_blend,
            iterations=req.iterations,
        )
    except BuildError as e:
        raise HTTPException(status_code=400, detail=str(e))

    curve = achieved_energy_curve(result.order, result.key_energy_blend)
    order_out = []
    for i, t in enumerate(result.order):
        trans = None
        if i > 0:
            cat, _ = transition_penalty(result.order[i - 1].key, t.key)
            trans = cat
        order_out.append(
            {
                "position": i + 1,
                "title": t.title,
                "artist": t.artist,
                "key": fmt_key(t.key),
                "energy": t.energy,
                "achieved_energy": round(curve[i], 2),
                "target_energy": round(result.targets[i], 2),
                "bpm": t.bpm,
                "duration_s": t.duration_s,
                "transition_from_prev": trans,
                "pinned": (i + 1) in result.pins,
                "phase": phase_groups.get(t.idx) if phase_groups else None,
            }
        )

    return {
        "build_id": build_id,
        "order": order_out,
        "total_duration_s": sum(t.duration_s or 0 for t in result.order),
        "bad_transitions": [
            {"from_position": i, "to_position": j, "from_title": a.label, "to_title": b.label}
            for i, j, a, b in result.bad_transitions
        ],
        "excluded": [t.label for t in result.missing_tags + result.phase_untagged],
        "not_selected": [t.label for t in result.not_selected],
        "shrink_warning": result.shrink_warning,
    }


@app.post("/api/build/{build_id}/export")
def export_build(build_id: str, req: ExportRequest):
    try:
        result = build_service.get_cached(build_id)
    except BuildError as e:
        raise HTTPException(status_code=404, detail=str(e))

    base_dir = config.LIBRARY_BASE_DIR
    meta_dir = base_dir / ".meta"
    resolved = resolve_track_paths(result.order, base_dir, meta_dir)

    if req.kind == "rekordbox_xml":
        playlist_name = req.playlist_name or f"anomaly-build-{build_id[:8]}"
        skipped = write_rekordbox_xml(result.order, resolved, playlist_name, req.path)
    elif req.kind == "playlist_folder":
        skipped = write_playlist_folder(result.order, resolved, req.path)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown export kind '{req.kind}'")

    return {
        "written": req.path,
        "resolved": len(result.order) - len(skipped),
        "total": len(result.order),
        "skipped": [t.label for t in skipped],
    }


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


# Mounted last so it never shadows the /api/... routes above - StaticFiles
# only handles requests nothing earlier matched.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
