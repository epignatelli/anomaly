const DEFAULT_SHAPE = "0:3,0.35:6,0.6:6,1:9";

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function getPlaylistPath() {
  return new URLSearchParams(window.location.search).get("playlist") || "";
}

function fmtDuration(seconds) {
  if (!seconds) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function addPinRow(title = "", position = "") {
  const container = document.getElementById("pins-container");
  const row = document.createElement("div");
  row.className = "pin-row";
  row.innerHTML = `
    <input type="text" placeholder="Title substring" class="pin-title" value="${escapeHtml(title)}" style="flex:2">
    <input type="text" placeholder="Position (1, last, ...)" class="pin-position" value="${escapeHtml(position)}" style="flex:1">
    <button type="button" class="secondary remove-pin">✕</button>`;
  row.querySelector(".remove-pin").addEventListener("click", () => row.remove());
  container.appendChild(row);
}

function collectPins() {
  return Array.from(document.querySelectorAll(".pin-row"))
    .map((row) => {
      const title = row.querySelector(".pin-title").value.trim();
      const position = row.querySelector(".pin-position").value.trim();
      return title && position ? `${title}=${position}` : null;
    })
    .filter(Boolean);
}

function addShapePointRow(percent = "", energy = "") {
  const container = document.getElementById("shape-container");
  const row = document.createElement("div");
  row.className = "pin-row";
  row.innerHTML = `
    <input type="number" min="0" max="100" placeholder="% through set" class="shape-percent" value="${escapeHtml(percent)}" style="flex:1">
    <span style="color: var(--text-dim)">% through set →</span>
    <input type="number" min="0" max="10" step="0.5" placeholder="Energy 0-10" class="shape-energy" value="${escapeHtml(energy)}" style="flex:1">
    <span style="color: var(--text-dim)">energy</span>
    <button type="button" class="secondary remove-shape-point">✕</button>`;
  row.querySelector(".remove-shape-point").addEventListener("click", () => row.remove());
  container.appendChild(row);
}

function populateDefaultShape() {
  // Mirrors DEFAULT_SHAPE = "0:3,0.35:6,0.6:6,1:9", expressed as
  // percent-through-set (0-100) instead of a raw 0-1 fraction.
  addShapePointRow(0, 3);
  addShapePointRow(35, 6);
  addShapePointRow(60, 6);
  addShapePointRow(100, 9);
}

function collectShape() {
  const points = Array.from(document.querySelectorAll("#shape-container .pin-row"))
    .map((row) => {
      const percent = row.querySelector(".shape-percent").value;
      const energy = row.querySelector(".shape-energy").value;
      if (percent === "" || energy === "") return null;
      return `${(parseFloat(percent) / 100).toFixed(4)}:${energy}`;
    })
    .filter(Boolean);
  return points.length ? points.join(",") : DEFAULT_SHAPE;
}

function init() {
  const playlistPath = getPlaylistPath();
  populateDefaultShape();

  const taggingLink = document.getElementById("tagging-link");
  taggingLink.textContent = playlistPath || "(no playlist)";
  taggingLink.href = `/tagging.html?playlist=${encodeURIComponent(playlistPath)}`;

  document.getElementById("add-pin").addEventListener("click", () => addPinRow());
  document.getElementById("add-shape-point").addEventListener("click", () => addShapePointRow());
  document.getElementById("use-phase-tags").addEventListener("change", (e) => {
    document.getElementById("phase-shape-fields").style.display = e.target.checked ? "" : "none";
  });

  document.getElementById("build-form").addEventListener("submit", onSubmit);
  document.getElementById("export-btn").addEventListener("click", onExport);
}

let lastBuildId = null;

async function onSubmit(e) {
  e.preventDefault();
  const status = document.getElementById("status");
  const results = document.getElementById("results");
  results.style.display = "none";
  status.textContent = "Building…";

  const playlistPath = getPlaylistPath();
  const numTracks = document.getElementById("num-tracks").value;
  const targetMinutes = document.getElementById("target-minutes").value;
  const usePhaseTags = document.getElementById("use-phase-tags").checked;

  const body = {
    playlist_path: playlistPath,
    num_tracks: numTracks ? parseInt(numTracks, 10) : null,
    target_minutes: targetMinutes ? parseFloat(targetMinutes) : null,
    pins: collectPins(),
    shape: collectShape(),
    use_phase_tags: usePhaseTags,
    phase_shape: usePhaseTags
      ? [
          parseFloat(document.getElementById("ps-opening").value),
          parseFloat(document.getElementById("ps-first-boost").value),
          parseFloat(document.getElementById("ps-plateau").value),
          parseFloat(document.getElementById("ps-second-boost").value),
          parseFloat(document.getElementById("ps-closing").value),
        ]
      : null,
    key_strict: document.getElementById("key-strict").checked,
    key_weight: parseFloat(document.getElementById("key-weight").value),
    key_energy_blend: parseFloat(document.getElementById("key-energy-blend").value),
    iterations: parseInt(document.getElementById("iterations").value, 10),
  };

  let data;
  try {
    const res = await fetch("/api/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  } catch (err) {
    status.innerHTML = `<div class="note error">Build failed: ${escapeHtml(err.message)}</div>`;
    return;
  }

  status.textContent = "";
  lastBuildId = data.build_id;
  renderResults(data);
}

function badgeClass(cat) {
  if (!cat) return "";
  if (cat === "other") return "other";
  if (cat === "perfect") return "perfect";
  return "";
}

function renderResults(data) {
  const results = document.getElementById("results");
  results.style.display = "";

  const summary = document.getElementById("summary");
  const notes = [];
  if (data.shrink_warning) notes.push(`<div class="note">${escapeHtml(data.shrink_warning)}</div>`);
  if (data.bad_transitions.length) {
    notes.push(
      `<div class="note">${data.bad_transitions.length} off-key transition(s) remain (soft mode).</div>`
    );
  }
  if (data.excluded.length) {
    notes.push(`<div class="note">${data.excluded.length} track(s) excluded (missing key/energy or phase tag).</div>`);
  }
  summary.innerHTML =
    `<p>${data.order.length} tracks, ${fmtDuration(data.total_duration_s)} total.</p>` + notes.join("");

  const body = document.getElementById("results-body");
  body.innerHTML = data.order
    .map((t) => {
      const cls = badgeClass(t.transition_from_prev);
      const transBadge = t.transition_from_prev
        ? `<span class="badge ${cls}">${escapeHtml(t.transition_from_prev)}</span>`
        : "";
      const pinnedBadge = t.pinned ? `<span class="badge pinned">pinned</span>` : "";
      return `
      <tr>
        <td>${t.position}</td>
        <td>${escapeHtml(t.artist ? t.artist + " - " + t.title : t.title)} ${pinnedBadge}</td>
        <td>${t.phase ? escapeHtml(t.phase) : ""}</td>
        <td>${escapeHtml(t.key)}</td>
        <td>${t.energy != null ? t.energy : ""}</td>
        <td>${t.achieved_energy}</td>
        <td>${t.target_energy}</td>
        <td>${t.bpm ? Math.round(t.bpm) : ""}</td>
        <td>${fmtDuration(t.duration_s)}</td>
        <td>${transBadge}</td>
      </tr>`;
    })
    .join("");
}

async function onExport() {
  const status = document.getElementById("export-status");
  if (!lastBuildId) {
    status.innerHTML = `<div class="note error">Build a set first.</div>`;
    return;
  }
  const kind = document.getElementById("export-kind").value;
  const path = document.getElementById("export-path").value.trim();
  const playlistName = document.getElementById("export-playlist-name").value.trim();
  if (!path) {
    status.innerHTML = `<div class="note error">Enter an output path.</div>`;
    return;
  }

  status.textContent = "Exporting…";
  try {
    const res = await fetch(`/api/build/${lastBuildId}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, path, playlist_name: playlistName || null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    let msg = `<div class="note">Wrote ${escapeHtml(data.written)} (${data.resolved}/${data.total} tracks resolved)</div>`;
    if (data.skipped.length) {
      msg += `<div class="note error">Could not resolve: ${data.skipped.map(escapeHtml).join(", ")}</div>`;
    }
    status.innerHTML = msg;
  } catch (err) {
    status.innerHTML = `<div class="note error">Export failed: ${escapeHtml(err.message)}</div>`;
  }
}

init();
