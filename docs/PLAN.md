# Phase-Group Set Building + Local Web App

## Context

`set_builder.py` suggests a DJ set order balancing an energy shape (slow start → rise → plateau → rise) against harmonic key compatibility (rules reused from `plot_set_energy.py`). It currently optimizes purely by matching each track's own intrinsic `Energy N` tag to a target curve, using Rekordbox TSV exports as input and either a Rekordbox-XML or a symlink-folder as output (the XML path has a confirmed bug — Rekordbox silently drops tracks whose file location is already in the live library under a different internal TrackID than the one we generate, so the symlink-folder-drag method is the reliable export path).

The next want: group tracks into 5 named phases — **opening, first boost, plateau, second boost, closing** — and have the algorithm pick candidates for each segment of the set *only* from the matching phase group, rather than picking freely by energy value alone. This requires tracks to be tagged into these groups somehow. Rekordbox's `Genre`/`Message` columns already have sparse, informal versions of this (`Peak`/`Upper`/`Outro`/`Downer`) on only a handful of tracks — not complete enough to build on.

Rather than hand-maintaining a text-file mapping, the plan is a small local web app: browse the Rekordbox library/playlists, tag tracks into the 5 phase groups visually, and hit a "Build Set" button that runs the same algorithm with all the flags `set_builder.py` already exposes (num_tracks/target_minutes, pins, shape, key_strict, key_weight, key_energy_blend) plus the new phase-group constraint. Intended outcome: replace the CLI-flag-and-hand-edited-file workflow with a visual one, without discarding or duplicating the algorithm already built and verified.

## Guiding decisions

- **Phase-tag storage: per-playlist, with a global-default fallback.** A track's role is set-dependent (an "opener" in one set might be "plateau" filler in another). Store tags keyed by `(playlist_path, track_id)`; if no playlist-specific tag exists, fall back to a global-default tag for that track; otherwise untagged.
- **v1 data source: static `rekordbox.xml` export, not Rekordbox's live database.** Rekordbox 6/7's live library is an encrypted SQLCipher DB; the community `pyrekordbox` project can read it but is version-fragile and unevaluated here. v1 reads the same kind of XML export the user already manually re-triggers today. Live-DB access is an explicit future phase, not v1 scope.
- **Backend: FastAPI**, not Flask — the build endpoint has ~10 typed numeric/bool parameters mirroring the CLI flags; Pydantic gives validation and self-documenting `/docs` for free at no real dependency-weight cost over Flask.
- **No Node/build step.** Server-rendered Jinja2 + vanilla JS (optionally htmx via CDN tag) for interactivity. This stays a local, single-user, no-auth tool.
- **New `webapp/` directory** inside this project, alongside the existing flat scripts (`sc_sync.py`, `plot_set_energy.py`, `set_builder.py`), which stay where they are; `webapp/` imports them directly (run via `python3 -m uvicorn webapp.main:app --reload` from the project root).

## Algorithm change to `set_builder.py` (needed regardless of UI — do this first, CLI-verifiable)

**Extract a reusable `build_set()` function.** Today `main()` is monolithic (argparse setup, then all pipeline logic, then output-writer calls inline). Refactor the pipeline into:

```python
@dataclass
class BuildResult:
    order: List[Track]
    targets: List[float]
    pins: Dict[int, Track]
    key_energy_blend: float
    bad_transitions: List[Tuple[int, int, Track, Track]]
    excluded: List[Track]       # untagged pool tracks (missing key/energy, or missing phase tag in phased mode)
    not_selected: List[Track]   # eligible but not chosen by selection

def build_set(pool, *, num_tracks=None, target_minutes=None, pins_spec=[], shape=DEFAULT_SHAPE,
              key_strict=False, key_weight=1.0, key_energy_blend=0.5, iterations=20000,
              phase_groups: Optional[Dict[int, str]] = None,
              phase_segments: Optional[List[float]] = None) -> BuildResult: ...
```

Internal `SystemExit`s (in `parse_pins`, and the pool-shrink-if-not-enough-candidates branch) become a new `BuildError(ValueError)`, caught and converted to `SystemExit` only at the CLI boundary in `main()` — so the same function serves both the CLI and the future web backend with zero duplicated logic, and the web layer can turn `BuildError` into an HTTP 400 instead of killing a process.

**Phase-group-constrained selection**, only active when `phase_groups` is passed:

```python
PHASES = ["opening", "first_boost", "plateau", "second_boost", "closing"]

def phase_segment_ranges(n: int, segments: List[float]) -> Dict[str, range]:
    """Split n positions into 5 contiguous ranges by cumulative rounding of the
    given proportions (default even 0.2 each), covering 1..n with no gaps/overlaps."""

def select_and_place_phased(candidates, free_positions, targets, phase_of, segment_ranges) -> Dict[int, Track]:
    """Run select_and_place() once per phase, restricted to that segment's
    positions and only candidates tagged with that phase. Raises BuildError if
    a segment doesn't have enough tagged candidates for its position count."""
```

This reuses `select_and_place()` (the existing Hungarian-assignment function) unchanged — 5 smaller solves instead of one big one, strictly cheaper, correct because positions/candidates partition cleanly by segment.

**Local search must not swap across segment boundaries.** Add one optional parameter to `local_search()`:

```python
def local_search(order, pinned_positions, targets, key_weight, strict, max_iterations,
                  key_energy_blend, segment_of_position: Optional[Dict[int, str]] = None):
    ...
    if segment_of_position is not None and segment_of_position[i + 1] != segment_of_position[j + 1]:
        continue   # skip cross-segment swap candidates
```

Zero behavior change when omitted — today's non-phased CLI calls are unaffected.

**Untagged-track handling**: tracks with no phase tag are excluded from phased builds the same way key/energy-missing tracks already are, added to `BuildResult.excluded` with a distinct reason label so `write_excluded_report`/the UI can show *why* a track didn't make the cut.

**CLI surface for standalone testing** (verify before any UI exists): add `--phase-tags PATH` (a small JSON `{title_substring: phase_name}`, resolved via the same fuzzy substring match `--pin` already uses) and `--phase-shape "0.15,0.2,0.3,0.2,0.15"` (default even split). These are CLI-only conveniences for verification — the web app calls `build_set()` in-process with a `phase_groups` dict built from its own SQLite store, not via this flag.

## Web app

### Data layer

- **`webapp/rekordbox_reader.py`** (new, read-only): parses `rekordbox.xml`'s `COLLECTION` into `{TrackID: RBTrack}` and `PLAYLISTS` into a folder/playlist tree (`NODE Type="0"` = folder, `Type="1"` = playlist containing `<TRACK Key="TrackID"/>` refs) — the structural inverse of `write_rekordbox_xml`. Key/energy parsed via `plot_set_energy.parse_key`/`parse_comment` on the `Comments` field, same convention `load_pool` already relies on. The alternate `rekordbox_mikcues_001.xml` schema (`POSITION_MARK`-based energy) is explicitly out of scope for v1 — documented, not handled.
- **`webapp/tag_store.py`** (new): SQLite (stdlib, no new dependency) table `track_tags(playlist_path, track_id, phase, updated_at)`, primary key `(playlist_path, track_id)`. Lookup order: exact playlist match → global default (`playlist_path=''`) → untagged.
- **`webapp/models.py`**: adapts `RBTrack` + phase tag into `set_builder.Track` instances (`to_builder_track`) so `build_set()` never needs to know about the Rekordbox layer.

### Backend (FastAPI, new `webapp/` package)

```
webapp/{main,config,rekordbox_reader,tag_store,models,build_service}.py
webapp/routes/{playlists,tags,build}.py
webapp/templates/{index,tagging,build}.html
webapp/static/{app.js,style.css}
```

Endpoints:
- `GET /api/playlists` — flattened playlist list with paths/counts
- `GET /api/playlists/{path}/tracks` — tracks + resolved phase tags
- `PUT /api/playlists/{path}/tags/{track_id}` — set a tag (`?scope=global` for the fallback row)
- `POST /api/build` — mirrors the CLI flags 1:1, calls `build_set()` in-process (no subprocess), caches the `BuildResult` server-side keyed by a `build_id` for export
- `POST /api/build/{build_id}/export` — `rekordbox_xml` or `playlist_folder`, reuses `resolve_track_paths`/`write_rekordbox_xml`/`write_playlist_folder` unchanged

### Frontend (Jinja2 + vanilla JS, no Node)

1. **Playlist picker** (`index.html`) — list from `GET /api/playlists`, click → tagging view.
2. **Tagging board** (`tagging.html`) — 5-column Kanban (+ "unassigned" tray), native HTML5 drag-and-drop between columns fires `PUT /api/playlists/{path}/tags/{track_id}`; click-to-select dropdown as a faster/keyboard-friendly alternative. Cards show title/artist/key/energy/bpm (energy read straight from the existing tag convention, no new energy-entry UI in v1).
3. **Build panel + results** (`build.html`) — form mirroring every existing CLI flag, `POST /api/build`, results table matching `print_result`'s columns plus a Phase column, export buttons for both output modes showing resolved/skipped counts exactly like the CLI does today.

## Explicit non-goals for v1

- No live Rekordbox DB access (`pyrekordbox`/SQLCipher) — future phase, not now.
- No new Rekordbox write path beyond the two that already exist.
- No auth/multi-user — local-only server, `127.0.0.1`.
- No Node/npm/build step.
- No energy-retagging UI — energy stays sourced from the existing `Comments` convention.
- No automated test suite requirement beyond manual CLI-diff verification at each milestone (though `build_set()`/`phase_segment_ranges()` becoming pure functions makes them cheap to add `pytest` coverage for later).

## Milestones (each independently verifiable before moving on)

1. **Phase-groups algorithm in `set_builder.py`, CLI-only.** Add `BuildError`, `phase_segment_ranges`, `select_and_place_phased`, the `local_search` segment guard, `--phase-tags`/`--phase-shape`. Verify: hand-write a `--phase-tags` JSON against a real TSV, confirm segments only pull from their tagged pool, no cross-segment swaps occur, and output is byte-identical to today's when the new flags are omitted.
2. **Extract `build_set()`**, convert internal `SystemExit`→`BuildError`, `main()` calls it. Verify: diff CLI stdout/CSV output before/after refactor on the same inputs+flags — must match exactly.
3. **`rekordbox_reader.py`, standalone.** Verify against a real `rekordbox.xml`: playlist paths/counts match Rekordbox's own UI, key/energy parsing matches what the TSV-based path already produces for the same tracks.
4. **`tag_store.py`.** Verify get/set/global-fallback lookup order against a scratch DB file.
5. **FastAPI read-only endpoints** (`/api/playlists`, `/api/playlists/{path}/tracks`, tags null). Verify via `/docs` + curl.
6. **Tagging write endpoint.** Verify round-trip via curl before any frontend exists.
7. **Build + export endpoints.** Verify: curl a payload matching a known-good CLI invocation (including a manually-seeded tag_store for phase_groups), confirm the returned order matches CLI output for the same inputs; confirm exports produce correct files.
8. **Frontend: playlist picker + tagging board.** Verify manually in-browser against a real library.
9. **Frontend: build panel + results + export.** Verify end-to-end: tag via browser, build, confirm phase column matches tags just set, export both ways, cross-check against Milestone 7's curl-verified output.
10. *(Optional polish, not required for a working v1)*: visual shape-curve editor, saved build presets, playlist search/filter.

## Verification approach summary

Every milestone has a concrete manual check (diff CLI output, curl round-trip, or in-browser confirmation) before moving to the next — no milestone depends on trusting an earlier one blindly. The core algorithm (Milestone 1) is fully testable from the terminal before a single line of web code exists, so the riskiest new logic gets verified independent of the UI.

## Future phase (v2): standalone portable build

The real deployment target is a USB drive containing the Rekordbox export, the audio files, and this app, plugged into *any* laptop (own machine, borrowed, venue laptop) with **nothing pre-installed** — no system Python, no `pip install`, no internet access required at the venue. That rules out a plain "clone the repo and run `uvicorn`" workflow.

The plan is a standalone executable via PyInstaller (or Nuitka), bundling the FastAPI backend and all Python dependencies, that starts the server and opens the browser automatically, resolving data paths relative to its own location on the USB drive rather than a fixed absolute path. This requires a separate build per target OS (PyInstaller bundles aren't cross-platform) — at minimum macOS (Apple Silicon) and Windows (x64), the latter needing an actual Windows machine or CI runner to build and test.

This is deliberately **out of scope for v1** — tracked as its own milestone ("v2: Standalone portable build") to pick up once the phase-groups algorithm and web app are working, not before. See issue #11.
