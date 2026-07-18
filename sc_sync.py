#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}


@dataclass
class PlaylistResult:
    url: str
    folder: str
    symlinks_created: int = 0
    tracks_downloaded: int = 0
    sidecars_moved: int = 0
    yt_dlp_returncode: Optional[int] = None
    errors: List[str] = field(default_factory=list)


# ------------------------------
# yt-dlp helpers
# ------------------------------
def yt_json_flat(url: str) -> List[dict]:
    """Return playlist entries (with at least 'id') using yt-dlp -J --flat-playlist.

    Raises RuntimeError with a short, readable message on any failure so callers
    can catch it and keep going with the remaining playlists.
    """
    try:
        cp = subprocess.run(
            ["yt-dlp", "-J", "--flat-playlist", url],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else str(e)
        raise RuntimeError(f"yt-dlp listing failed: {detail}") from e
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"yt-dlp returned invalid JSON: {e}") from e
    return data.get("entries", []) or []


def run_yt_dlp_in(
    folder: Path, url: str, archive_path: Path, dry: bool
) -> "tuple[Optional[int], List[str]]":
    """Run yt-dlp in 'folder' to fetch new items only (archive enforces skip).

    Streams yt-dlp's output live (as before) while also keeping the last few
    lines around, so a nonzero exit can be reported with an actual reason
    instead of just a bare return code.

    Returns (returncode, tail_lines). returncode is None in dry-run mode.
    """
    cmd = (
        f"cd {shlex.quote(str(folder))} && "
        f"yt-dlp {shlex.quote(url)} "
        f"--format mp3/m4a "
        f"--download-archive {shlex.quote(str(archive_path))} "
        f"--write-info-json "
        f"--no-part --newline"
    )
    if dry:
        print(f"[DRYRUN] would run: {cmd}")
        return None, []

    tail: deque = deque(maxlen=15)
    proc = subprocess.Popen(
        [cmd],
        shell=True,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        print(line)
        tail.append(line)
    proc.wait()
    return proc.returncode, list(tail)


# ------------------------------
# filesystem helpers
# ------------------------------
def safe_symlink(src: Path, dst_dir: Path, dry: bool) -> Optional[Path]:
    """Create a relative symlink in dst_dir pointing to src, if not already present."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        return None
    rel = os.path.relpath(src, dst_dir)
    if dry:
        print(f"[DRYRUN] would symlink: {dst} -> {rel}")
        return None
    os.symlink(rel, dst)
    return dst


def same_stem_media_in(folder: Path, stem: str) -> Optional[Path]:
    """Find a media file in 'folder' whose stem matches 'stem'."""
    for ext in AUDIO_EXTS:
        p = folder / f"{stem}{ext}"
        if p.exists() and p.is_file():
            return p
    return None


# ------------------------------
# ID <-> path index (persisted to .meta/id_path_map.json)
# ------------------------------
def load_id_map(meta_dir: Path) -> Dict[str, Path]:
    p = meta_dir / "id_path_map.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
        # stored as relative paths from base
        return {k: Path(v) for k, v in raw.items()}
    except Exception:
        return {}


def save_id_map(
    meta_dir: Path, base_dir: Path, id2file: Dict[str, Path], dry: bool
) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    p = meta_dir / "id_path_map.json"
    payload = {
        k: str(f.relative_to(base_dir)) for k, f in id2file.items() if f.exists()
    }
    if dry:
        print(f"[DRYRUN] would write {p} with {len(payload)} entries")
        return
    p.write_text(json.dumps(payload, indent=2))


def prune_missing(id2file: Dict[str, Path]) -> Dict[str, Path]:
    return {k: v for k, v in id2file.items() if v.exists()}


# Bootstrap: read adjacent *.info.json and IDs in filenames (non-destructive)
def build_id_index(base_dir: Path, meta_dir: Path) -> Dict[str, Path]:
    id2file: Dict[str, Path] = load_id_map(meta_dir)

    # Make absolute
    for k, v in list(id2file.items()):
        if not v.is_absolute():
            id2file[k] = (base_dir / v).resolve()

    # 1) Adjacent *.info.json sidecars (except inside .meta)
    for info in base_dir.rglob("*.info.json"):
        if meta_dir in info.parents:
            continue
        try:
            meta = json.loads(info.read_text())
            tid = str(meta.get("id", "")).strip()
        except Exception:
            tid = ""
        if not tid:
            continue
        stem = info.name[: -len(".info.json")]
        media = same_stem_media_in(info.parent, stem)
        if media and tid not in id2file:
            id2file[tid] = media.resolve()

    # 2) IDs inside filenames like [1234567890]
    id_in_name = re.compile(r"\[(\d{6,})\]")
    for f in base_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            m = id_in_name.search(f.name)
            if m:
                id2file.setdefault(m.group(1), f.resolve())

    return prune_missing(id2file)


# After downloads, stash sidecars into .meta and update id map
def replace_with_retry(src: Path, dst: Path, attempts: int = 3, delay_s: float = 0.3) -> None:
    """Move src to dst, working around a FAT32/fskit driver bug.

    The base drive here is FAT32 mounted via macOS's fskit msdos driver.
    Cross-directory rename() reliably raises ENOENT (not a transient race)
    for filenames containing decomposed-Unicode combining-diacritic
    sequences (e.g. NFD "o" + U+0308), even though the file demonstrably
    exists and is readable. A few Path.replace() retries handle any genuine
    transient flakiness; if those still fail, fall back to copy+unlink,
    which sidesteps the driver's buggy rename path entirely.
    """
    last_err: Optional[OSError] = None
    for i in range(attempts):
        try:
            src.replace(dst)
            return
        except FileNotFoundError as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(delay_s)

    try:
        dst.write_bytes(src.read_bytes())
        src.unlink()
    except OSError:
        raise last_err


def ingest_new_sidecars(
    playlist_dir: Path,
    base_dir: Path,
    meta_dir: Path,
    id2file: Dict[str, Path],
    dry: bool,
) -> "tuple[int, List[str]]":
    moved = 0
    errors: List[str] = []
    for info in playlist_dir.glob("*.info.json"):
        try:
            try:
                meta = json.loads(info.read_text())
                tid = str(meta.get("id", "")).strip()
            except Exception:
                tid = ""
            if not tid:
                continue

            stem = info.name[: -len(".info.json")]
            media = same_stem_media_in(playlist_dir, stem)
            if media:
                # Update the map (absolute paths in memory; saved as relative)
                id2file.setdefault(tid, media.resolve())

            # Move the info file to .meta as <id>.info.json (dedup by ID)
            target = meta_dir / f"{tid}.info.json"
            if dry:
                print(f"[DRYRUN] would move {info} -> {target}")
                moved += 1
            else:
                meta_dir.mkdir(parents=True, exist_ok=True)
                # If a file with that ID already exists in .meta, keep the first one
                if not target.exists():
                    replace_with_retry(info, target)
                    moved += 1
                else:
                    # Already stashed; remove duplicate leftover
                    try:
                        info.unlink()
                    except Exception:
                        pass
        except Exception as e:
            # Don't let one bad sidecar abort the rest of the batch.
            errors.append(f"{info.name}: {e}")
    return moved, errors


# ------------------------------
# Symlinks for per-playlist duplicates
# ------------------------------
def ensure_playlist_symlinks(
    url: str, playlist_dir: Path, id2file: Dict[str, Path], dry: bool
) -> int:
    entries = yt_json_flat(url)
    created = 0
    existing_names = {
        p.name for p in playlist_dir.glob("*") if p.is_file() or p.is_symlink()
    }

    for e in entries:
        tid = str(e.get("id") or "").strip()
        if not tid:
            continue
        src = id2file.get(tid)
        if not src:
            continue
        # If already present in this folder by name, skip
        if src.name in existing_names:
            continue
        if safe_symlink(src, playlist_dir, dry):
            created += 1
    return created


# ------------------------------
# reporting
# ------------------------------
def write_report(
    meta_dir: Path,
    started: datetime,
    duration_s: float,
    dry: bool,
    results: List[PlaylistResult],
) -> Path:
    """Write a per-run report (and append a one-line summary to the history log)."""
    total_symlinks = sum(r.symlinks_created for r in results)
    total_downloaded = sum(r.tracks_downloaded for r in results)
    total_errors = sum(len(r.errors) for r in results)
    failed_playlists = [r for r in results if r.errors]

    lines = []
    lines.append(f"sc_sync report - {started.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Mode:               {'DRY RUN' if dry else 'LIVE'}")
    lines.append(f"Duration:           {duration_s:.1f}s")
    lines.append(f"Playlists synced:   {len(results)}")
    lines.append(f"New tracks fetched: {total_downloaded}")
    lines.append(f"Symlinks created:   {total_symlinks}")
    lines.append(f"Errors:             {total_errors}")
    lines.append("")
    lines.append("Per-playlist detail:")
    for r in results:
        name = r.url.rstrip("/").split("/")[-1]
        status = "OK" if not r.errors else "ISSUES"
        lines.append(
            f"  [{status}] {name:35s} downloaded={r.tracks_downloaded:<4d} "
            f"symlinks={r.symlinks_created:<4d} rc={r.yt_dlp_returncode}"
        )
        for err in r.errors:
            lines.append(f"           ! {err}")

    if failed_playlists:
        lines.append("")
        lines.append(f"{len(failed_playlists)} playlist(s) had issues - see above.")

    report_text = "\n".join(lines) + "\n"

    reports_dir = meta_dir / "reports"
    ts_slug = started.strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"report_{ts_slug}.txt"
    history_path = meta_dir / "sync_history.log"
    summary_line = (
        f"{started.strftime('%Y-%m-%d %H:%M:%S')} "
        f"{'DRYRUN' if dry else 'LIVE  '} "
        f"playlists={len(results)} new={total_downloaded} "
        f"symlinks={total_symlinks} errors={total_errors}\n"
    )

    print("\n" + "-" * 80)
    print(report_text.rstrip("\n"))
    print("-" * 80)

    if dry:
        print(f"[DRYRUN] would write report to {report_path}")
        print(f"[DRYRUN] would append summary to {history_path}")
        return report_path

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text)
    with history_path.open("a") as f:
        f.write(summary_line)

    return report_path


# ------------------------------
# main
# ------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="SoundCloud playlist fetch + symlink deduper"
    )
    parser.add_argument(
        "--base-dir",
        default="../SoundCloud-LQ",
        help="Base folder (relative to this script; default sits alongside the anomaly/ project folder)",
    )
    parser.add_argument(
        "--archive", default="archive.txt", help="yt-dlp archive path (inside base-dir)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without changing anything",
    )
    parser.add_argument(
        "--meta-dir", default=".meta", help="Where to stash info.json sidecars"
    )
    args = parser.parse_args()

    # Env override: DRYRUN=1
    dry = args.dry_run or os.environ.get("DRYRUN") == "1"

    playlists = [
        "https://soundcloud.com/therapy13/sets/acid-techno",
        "https://soundcloud.com/therapy13/sets/bangers",
        "https://soundcloud.com/therapy13/sets/chill-techno",
        "https://soundcloud.com/therapy13/sets/cloudy-boiler-gotec-club-1-1-2",
        "https://soundcloud.com/therapy13/sets/emotional-non-techno",
        "https://soundcloud.com/therapy13/sets/emotional-techno",
        "https://soundcloud.com/therapy13/sets/hard-techno-ascending",
        "https://soundcloud.com/therapy13/sets/hard-techno-exit",
        "https://soundcloud.com/therapy13/sets/hard-techno-peak",
        "https://soundcloud.com/therapy13/sets/hard-techno-pop-remixes",
        "https://soundcloud.com/therapy13/sets/high-energy-hard-dance",
        "https://soundcloud.com/therapy13/sets/high-energy-psytrance",
        "https://soundcloud.com/therapy13/sets/intro-tracks",
        "https://soundcloud.com/therapy13/sets/pure-schranz",
        "https://soundcloud.com/therapy13/sets/schellt-schissma-secret-rave",
        "https://soundcloud.com/therapy13/sets/techno-classics",
        "https://soundcloud.com/dean-thielen/sets/bounce-techno",
        "https://soundcloud.com/therapy13/sets/session-6",
        "https://soundcloud.com/therapy13/sets/session-6-selected",
        "https://soundcloud.com/therapy13/sets/session-7",
    ]

    script_dir = Path(__file__).resolve().parent
    base_dir = (script_dir / args.base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = (base_dir / args.meta_dir).resolve()
    archive_path = (base_dir / args.archive).resolve()

    print(f"Base:     {base_dir}")
    print(f"Meta:     {meta_dir}")
    print(f"Archive:  {archive_path}")
    print(f"Dry-run:  {dry}")

    if shutil.which("yt-dlp") is None:
        print("\nERROR: 'yt-dlp' was not found on PATH. Install it and re-run.")
        raise SystemExit(1)

    run_started = datetime.now()
    run_start_t = time.monotonic()

    # Bootstrap ID index from existing data
    id2file = build_id_index(base_dir, meta_dir)

    results: List[PlaylistResult] = []

    for url in playlists:
        folder = base_dir / url.rstrip("/").split("/")[-1]
        folder.mkdir(parents=True, exist_ok=True)

        print("\n" + "*" * 80)
        print(f"Playlist: {url}")
        print(f"Folder:   {folder}")

        result = PlaylistResult(url=url, folder=str(folder))

        # 1) Pre-pass: symlink duplicates already on disk
        try:
            result.symlinks_created = ensure_playlist_symlinks(url, folder, id2file, dry)
            print(f"Symlinks created: {result.symlinks_created}")
        except RuntimeError as e:
            msg = f"symlink pre-pass failed: {e}"
            result.errors.append(msg)
            print(f"ERROR: {msg}")

        # 2) Download what's new (archive enforces skip-by-ID)
        try:
            result.yt_dlp_returncode, tail_lines = run_yt_dlp_in(folder, url, archive_path, dry)
            if result.yt_dlp_returncode not in (None, 0):
                tail = " | ".join(l for l in tail_lines if l.strip())
                msg = f"yt-dlp exited with code {result.yt_dlp_returncode}"
                if tail:
                    msg += f" - last output: {tail}"
                result.errors.append(msg)
                print(f"WARNING: {msg}")
        except Exception as e:
            msg = f"download failed: {e}"
            result.errors.append(msg)
            print(f"ERROR: {msg}")

        # 3) Post-pass: stash sidecars in .meta and update index
        try:
            result.sidecars_moved, sidecar_errors = ingest_new_sidecars(
                folder, base_dir, meta_dir, id2file, dry
            )
            result.tracks_downloaded = result.sidecars_moved
            print(f"Sidecars moved to .meta: {result.sidecars_moved}")
            for se in sidecar_errors:
                msg = f"sidecar ingest failed: {se}"
                result.errors.append(msg)
                print(f"ERROR: {msg}")
        except Exception as e:
            msg = f"sidecar ingest failed: {e}"
            result.errors.append(msg)
            print(f"ERROR: {msg}")
        print("*" * 80)

        # Persist ID map after each playlist (so later playlists can link newly added items)
        save_id_map(meta_dir, base_dir, id2file, dry)

        results.append(result)

    run_duration_s = time.monotonic() - run_start_t
    report_path = write_report(meta_dir, run_started, run_duration_s, dry, results)

    print("\nDone.")
    print(f"Report:   {report_path}")


if __name__ == "__main__":
    main()
