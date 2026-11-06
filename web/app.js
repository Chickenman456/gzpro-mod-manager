const state = { status: null, busy: false };
const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function setMessage(message, isError = false) {
  const element = byId("action-message");
  element.textContent = message;
  element.classList.toggle("error", isError);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

function renderStatus(status) {
  state.status = status;
  byId("game-dir").value = status.game_dir;
  byId("compatibility-warning").innerHTML = status.ue4ss_installed
    ? "UE4SS is installed. Blueprint mods can load through BPModLoaderMod. Asset override mods do not need UE4SS."
    : 'UE4SS is only required for Blueprint mods. Asset override mods (music, textures, models) work without it. <a href="https://github.com/UE4SS-RE/RE-UE4SS/releases" target="_blank" rel="noopener noreferrer">Download UE4SS</a> for Blueprint mods.';

  const installStatus = byId("install-status");
  if (!status.game_valid) {
    installStatus.textContent = "Game install not found. Enter the folder that contains GZPro\\Binaries and GZPro\\Content.";
    installStatus.classList.add("error");
  } else if (status.game_check_error) {
    installStatus.textContent = status.game_check_error;
    installStatus.classList.add("error");
  } else if (status.game_running) {
    installStatus.textContent = "The game is running. Close it before enabling, disabling, or importing a mod.";
    installStatus.classList.add("error");
  } else {
    const loader = status.ue4ss_installed ? "UE4SS detected" : "UE4SS not installed · asset overrides still work";
    installStatus.textContent = `Ready · ${loader} · ${status.logic_mods_exists ? "LogicMods folder found" : "LogicMods folder will be created on import"}`;
    installStatus.classList.remove("error");
  }

  const modsElement = byId("mods");
  if (!status.mods.length) {
    modsElement.innerHTML = `<p class="empty-state">No mods are managed yet.</p>`;
  } else {
    modsElement.innerHTML = status.mods.map((mod) => {
      const stateLabel = mod.conflict ? "File changed outside the manager" : (mod.enabled ? "Enabled" : "Disabled");
      const actionLabel = mod.enabled ? "Disable" : "Enable";
      const disabled = state.busy || !status.game_valid || status.game_running || status.game_check_error || mod.conflict || !mod.backup_available;
      const deleteDisabled = state.busy || !status.game_valid || status.game_running || status.game_check_error || !mod.backup_available;
      const kindLabel = mod.kind === "blueprint" ? "Blueprint · LogicMods" : "Asset override · Paks";
      const thumbnail = mod.thumbnail ? `<img class="mod-thumbnail" src="${escapeHtml(mod.thumbnail)}" alt="${escapeHtml(mod.stem || "Mod")} thumbnail">` : "";
      const description = mod.description ? `<p class="mod-description">${escapeHtml(mod.description)}</p>` : "";
      return `<article class="mod-card">
        ${thumbnail}
        <div class="mod-info">
          <strong>${escapeHtml(mod.stem || mod.filename)}</strong>
          <span>${formatSize(mod.size)} · ${mod.file_count || 1} files · ${kindLabel} · ${escapeHtml(stateLabel)}</span>
          ${description}
          <small>SHA-256 ${escapeHtml(mod.sha256.slice(0, 16))}… · backup ${mod.backup_available ? "available" : "missing"}</small>
        </div>
        <div class="mod-actions">
          <button class="button ${mod.enabled ? "button-secondary" : "button-primary"}" data-mod-id="${escapeHtml(mod.id)}" data-next-state="${mod.enabled ? "disable" : "enable"}" ${disabled ? "disabled" : ""}>${actionLabel}</button>
          <button class="button button-danger" data-delete-mod="${escapeHtml(mod.id)}" ${deleteDisabled ? "disabled" : ""}>Delete</button>
        </div>
      </article>`;
    }).join("");
    modsElement.querySelectorAll("button[data-mod-id]").forEach((button) => {
      button.addEventListener("click", () => changeModState(button.dataset.modId, button.dataset.nextState));
    });
    modsElement.querySelectorAll("button[data-delete-mod]").forEach((button) => {
      button.addEventListener("click", () => deleteManagedMod(button.dataset.deleteMod));
    });
  }

  const unmanaged = byId("unmanaged");
  unmanaged.innerHTML = status.unmanaged_mods.length
    ? `<h3>Manually installed mods</h3><p class="muted">Adopt a detected mod to create a verified backup and manage its enabled state here. Adoption does not move or alter the installed files.</p>
      <div class="mod-list">${status.unmanaged_mods.map((mod) => {
        const fileNames = mod.files.map(escapeHtml).join(", ");
        const disabled = state.busy || !status.game_valid || status.game_running || status.game_check_error;
        return `<article class="mod-card">
          <div class="mod-info">
            <strong>${escapeHtml(mod.stem)}</strong>
            <span>${escapeHtml(mod.location)} · ${formatSize(mod.size)} · ${mod.files.length} files</span>
            <small>${fileNames}</small>
          </div>
          <button class="button button-primary" data-adopt-location="${escapeHtml(mod.location)}" data-adopt-stem="${escapeHtml(mod.stem)}" ${disabled ? "disabled" : ""}>Adopt</button>
        </article>`;
      }).join("")}</div>`
    : "";
  unmanaged.querySelectorAll("button[data-adopt-location]").forEach((button) => {
    button.addEventListener("click", () => adoptUnmanagedMod(button.dataset.adoptLocation, button.dataset.adoptStem));
  });
  byId("mod-file").disabled = state.busy || !status.game_valid || Boolean(status.game_running) || Boolean(status.game_check_error);
  byId("refresh").disabled = state.busy;
}

async function refreshStatus() {
  try {
    renderStatus(await requestJson("/api/status"));
  } catch (error) {
    setMessage(`Could not read manager status: ${error.message}`, true);
  }
}

function uploadFile(file, kind) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/mods");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-Mod-Name", encodeURIComponent(file.name));
    request.setRequestHeader("X-Mod-Kind", kind);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        byId("upload-progress").value = Math.round((event.loaded / event.total) * 100);
      }
    };
    request.onload = () => {
      let result;
      try {
        result = JSON.parse(request.responseText);
      } catch {
        reject(new Error("The manager returned an invalid response."));
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(result.error || `Upload failed (${request.status}).`));
        return;
      }
      resolve(result);
    };
    request.onerror = () => reject(new Error("Upload failed. Check that the manager is still running."));
    request.send(file);
  });
}

async function importFile(file) {
  if (!file) return;
  if (!file.name.toLocaleLowerCase().endsWith(".zip")) {
    setMessage("Mods install from a .zip package holding the cooked .utoc and .ucas files. A raw WAV or loose Unreal asset cannot be dropped in directly.", true);
    return;
  }
  if (!state.status?.game_valid || state.status.game_running || state.status.game_check_error) {
    setMessage("The game install must be valid and the game must be closed before importing.", true);
    return;
  }
  const kind = document.querySelector('input[name="mod-kind"]:checked')?.value || "override";
  const target = kind === "blueprint" ? "the game's LogicMods folder" : "the game's Paks folder as GZPro-" + file.name.replace(/\.zip$/i, "") + ".*";
  if (!window.confirm(
    `Deploy ${file.name} into ${target}? The manager keeps a hash-checked backup and never overwrites existing files.${kind === "blueprint" ? " Loading it in-game requires UE4SS." : ""}`
  )) return;
  state.busy = true;
  byId("upload-progress").hidden = false;
  byId("upload-progress").value = 0;
  setMessage(`Importing ${file.name}…`);
  renderStatus(state.status);
  try {
    const mod = await uploadFile(file, kind);
    const note = mod.kind === "blueprint"
      ? "UE4SS loads it the next time the game starts."
      : "It overrides the game's original assets the next time the game starts.";
    setMessage(`${mod.stem || mod.filename} was deployed (${mod.file_count} files). ${note}`);
  } catch (error) {
    setMessage(`Import failed: ${error.message}`, true);
  } finally {
    state.busy = false;
    byId("upload-progress").hidden = true;
    byId("mod-file").value = "";
    await refreshStatus();
  }
}

async function adoptUnmanagedMod(location, stem) {
  if (!location || !stem) return;
  if (!window.confirm(
    `Adopt ${stem} from ${location}? The manager will create a backup and register the existing files without moving or changing them. You can then disable or re-enable the mod here.`
  )) return;
  state.busy = true;
  setMessage(`Backing up ${stem}…`);
  renderStatus(state.status);
  try {
    await requestJson("/api/mods/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ location, stem })
    });
    setMessage(`${stem} was adopted. Its original files are still installed and can now be managed here.`);
  } catch (error) {
    setMessage(`Could not adopt ${stem}: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}

async function deleteManagedMod(modId) {
  const mod = state.status?.mods.find((item) => item.id === modId);
  if (!mod) return;
  if (!window.confirm(`Permanently delete ${mod.stem || mod.filename}? This removes its verified installed files, its manager backup, and its library entry. Files changed outside the manager will be left untouched and will block deletion.`)) return;
  state.busy = true;
  setMessage(`Deleting ${mod.stem || mod.filename}…`);
  renderStatus(state.status);
  try {
    await requestJson(`/api/mods/${encodeURIComponent(modId)}/delete`, { method: "POST" });
    setMessage(`${mod.stem || mod.filename} was deleted from the manager.`);
  } catch (error) {
    setMessage(`Could not delete mod: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}
async function changeModState(modId, action) {
  if (!modId || !["enable", "disable"].includes(action)) return;
  const verb = action === "disable" ? "disable" : "enable";
  if (!window.confirm(`Close the game, then ${verb} this mod? The manager will not overwrite another file.`)) return;
  state.busy = true;
  setMessage(`${verb === "disable" ? "Disabling" : "Enabling"} mod…`);
  renderStatus(state.status);
  try {
    await requestJson(`/api/mods/${encodeURIComponent(modId)}/${action}`, { method: "POST" });
    setMessage(`Mod ${verb}d. The saved backup was kept.`);
  } catch (error) {
    setMessage(`Could not ${verb} mod: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}

byId("mod-file").addEventListener("change", (event) => importFile(event.target.files[0]));
byId("refresh").addEventListener("click", refreshStatus);
byId("game-dir-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage("Checking the selected game folder…");
  try {
    renderStatus(await requestJson("/api/game-dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_dir: byId("game-dir").value })
    }));
    setMessage("Game folder selected. No game files were changed.");
  } catch (error) {
    setMessage(`Could not select game folder: ${error.message}`, true);
  }
});

const dropZone = byId("drop-zone");
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
}
dropZone.addEventListener("drop", (event) => importFile(event.dataTransfer.files[0]));

refreshStatus();
