const PHASES = ["opening", "first_boost", "plateau", "second_boost", "closing"];
const PHASE_LABELS = {
  opening: "Opening",
  first_boost: "First Boost",
  plateau: "Plateau",
  second_boost: "Second Boost",
  closing: "Closing",
};

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function getPlaylistPath() {
  return new URLSearchParams(window.location.search).get("playlist") || "";
}

function fmtDuration(seconds) {
  if (!seconds) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function toggleTag(playlistPath, trackId, phase, isActive) {
  const url = `/api/playlists/${encodeURIComponent(playlistPath)}/tags/${encodeURIComponent(trackId)}/${phase}`;
  const res = await fetch(url, { method: isActive ? "DELETE" : "PUT" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
}

function rowHtml(track) {
  const title = escapeHtml(track.title || "");
  const artist = track.artist ? escapeHtml(track.artist) : "";
  const pills = PHASES.map((p) => {
    const active = (track.phases || []).includes(p);
    return `<span class="pill ${active ? "active" : ""}" data-track-id="${track.track_id}" data-phase="${p}">${PHASE_LABELS[p]}</span>`;
  }).join("");

  return `
    <tr data-track-id="${track.track_id}">
      <td>
        <div class="track-title">${title}</div>
        ${artist ? `<div class="track-artist">${artist}</div>` : ""}
      </td>
      <td>${track.key || "?"}</td>
      <td>${track.energy != null ? track.energy : "?"}</td>
      <td>${track.bpm ? Math.round(track.bpm) : "?"}</td>
      <td>${fmtDuration(track.duration_s)}</td>
      <td><div class="pill-row">${pills}</div></td>
    </tr>`;
}

async function loadTagging() {
  const playlistPath = getPlaylistPath();
  const status = document.getElementById("status");
  document.getElementById("playlist-name").textContent = playlistPath;
  document.getElementById("build-btn").addEventListener("click", () => {
    window.location.href = `/build.html?playlist=${encodeURIComponent(playlistPath)}`;
  });

  if (!playlistPath) {
    status.innerHTML = `<div class="note error">No playlist selected. Go back to the <a href="/index.html">playlist list</a>.</div>`;
    return;
  }

  status.textContent = "Loading tracks…";
  let tracks;
  try {
    const res = await fetch(`/api/playlists/${encodeURIComponent(playlistPath)}/tracks`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    tracks = await res.json();
  } catch (err) {
    status.innerHTML = `<div class="note error">Could not load tracks: ${escapeHtml(err.message)}</div>`;
    return;
  }
  status.textContent = "";

  const table = document.getElementById("tracks-table");
  const body = document.getElementById("tracks-body");
  table.style.display = "";
  body.innerHTML = tracks.map(rowHtml).join("");

  body.addEventListener("click", async (e) => {
    const pill = e.target.closest(".pill");
    if (!pill || pill.classList.contains("loading")) return;

    const trackId = pill.dataset.trackId;
    const phase = pill.dataset.phase;
    const track = tracks.find((t) => t.track_id === trackId);
    const wasActive = pill.classList.contains("active");

    pill.classList.add("loading");
    try {
      await toggleTag(playlistPath, trackId, phase, wasActive);
      if (wasActive) {
        track.phases = track.phases.filter((p) => p !== phase);
        pill.classList.remove("active");
      } else {
        track.phases.push(phase);
        pill.classList.add("active");
      }
    } catch (err) {
      status.innerHTML = `<div class="note error">Could not update tag: ${escapeHtml(err.message)}</div>`;
    } finally {
      pill.classList.remove("loading");
    }
  });
}

loadTagging();
