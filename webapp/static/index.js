function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function loadSettings() {
  const input = document.getElementById("xml-path");
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    input.value = data.rekordbox_xml_path;
    if (!data.exists) {
      document.getElementById("settings-status").innerHTML =
        `<div class="note error">This path does not exist on disk.</div>`;
    }
  } catch (err) {
    // Non-fatal - the input just stays blank if settings can't be loaded.
  }
}

async function saveSettings() {
  const input = document.getElementById("xml-path");
  const status = document.getElementById("settings-status");
  status.textContent = "Saving…";
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rekordbox_xml_path: input.value.trim() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    status.innerHTML = `<div class="note">Saved. Reloading playlists…</div>`;
    await loadPlaylists();
    status.textContent = "";
  } catch (err) {
    status.innerHTML = `<div class="note error">${escapeHtml(err.message)}</div>`;
  }
}

async function uploadXmlFile(file) {
  const status = document.getElementById("settings-status");
  status.textContent = `Uploading ${file.name}…`;
  try {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch("/api/settings/upload", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    document.getElementById("xml-path").value = data.rekordbox_xml_path;
    status.innerHTML = `<div class="note">Uploaded. Reloading playlists…</div>`;
    await loadPlaylists();
    status.textContent = "";
  } catch (err) {
    status.innerHTML = `<div class="note error">${escapeHtml(err.message)}</div>`;
  }
}

async function loadPlaylists() {
  const status = document.getElementById("status");
  const table = document.getElementById("playlist-table");
  const body = document.getElementById("playlist-body");

  status.textContent = "Loading playlists…";
  let playlists;
  try {
    const res = await fetch("/api/playlists");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    playlists = await res.json();
  } catch (err) {
    status.innerHTML = `<div class="note error">Could not load playlists: ${escapeHtml(err.message)}. Is the Rekordbox XML export path configured correctly?</div>`;
    return;
  }

  status.textContent = "";
  table.style.display = "";
  body.innerHTML = playlists
    .map(
      (p) => `
    <tr class="playlist-row" data-path="${escapeHtml(p.path)}">
      <td>${escapeHtml(p.path)}</td>
      <td class="playlist-count">${p.count}</td>
    </tr>`
    )
    .join("");

  body.querySelectorAll(".playlist-row").forEach((row) => {
    row.addEventListener("click", () => {
      const path = row.dataset.path;
      window.location.href = `/tagging.html?playlist=${encodeURIComponent(path)}`;
    });
  });
}

async function resync() {
  // The server already re-reads the XML from disk on every request (no
  // server-side caching to invalidate) - this just refreshes what's shown
  // here, for after you've re-exported from Rekordbox.
  const status = document.getElementById("settings-status");
  status.textContent = "Re-syncing…";
  await loadSettings();
  await loadPlaylists();
  status.innerHTML = `<div class="note">Re-synced.</div>`;
  setTimeout(() => { status.innerHTML = ""; }, 2000);
}

document.getElementById("resync-btn").addEventListener("click", resync);
document.getElementById("save-settings").addEventListener("click", saveSettings);
document.getElementById("browse-btn").addEventListener("click", () => {
  document.getElementById("xml-file").click();
});
document.getElementById("xml-file").addEventListener("change", (e) => {
  if (e.target.files[0]) uploadXmlFile(e.target.files[0]);
});
loadSettings();
loadPlaylists();
