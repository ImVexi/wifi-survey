/* Wi-Fi Survey — first-run wizard and dashboard.
 * Plain ES modules, same-origin fetch only, no external requests.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  setup: null,
  model: null,
  floors: [],
  viewer: null,
  poll: null,
  pairTimer: null,
};

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.headers || {}) },
    ...options,
  });
  let body = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try { body = await response.json(); } catch { body = null; }
  }
  if (!response.ok) {
    const detail = body && (body.message || body.detail);
    const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function show(view) {
  for (const id of ["view-login", "view-wizard", "view-app"]) {
    $("#" + id).hidden = id !== view;
  }
  $("#logout").hidden = view !== "view-app";
  $("#status-pill").hidden = view === "view-app";
}

function stepFor(setup) {
  if (!setup) return "admin";
  if (!setup.has_admin) return "admin";
  if (!setup.has_model) return "model";
  if (!setup.floors || !setup.floors.length) return "floors";
  return "aps";
}

function markSteps(active) {
  const order = ["admin", "model", "floors", "aps"];
  const at = order.indexOf(active);
  $$("#wizard-steps li").forEach((li) => {
    const index = order.indexOf(li.dataset.step);
    li.classList.toggle("is-on", index === at);
    li.classList.toggle("is-done", index < at);
  });
}

function showStep(step) {
  show("view-wizard");
  for (const panel of $$(".step")) {
    panel.hidden = panel.dataset.panel !== step;
  }
  markSteps(step);
  if (step === "floors") renderFloorInputs();
  if (step === "aps") renderApRows();
}

function applyStatus(setup) {
  state.setup = setup;
  state.model = setup.model;
  state.floors = setup.floors || [];
  const pill = $("#status-pill");
  if (!setup.setup_complete) {
    pill.hidden = false;
    pill.textContent = "Setup incomplete";
    pill.className = "pill warn";
  }
}

/* ------------------------------------------------------------------ login */

async function trySession() {
  try {
    const me = await api("/api/v1/auth/me");
    if (!me.authenticated) return null;
    applyStatus(me.setup);
    return me.setup;
  } catch (error) {
    if (error.status === 401) return null;
    throw error;
  }
}

function showLogin() {
  show("view-login");
  setTimeout(() => $("#login-password").focus(), 30);
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = $("#login-error");
  errorEl.hidden = true;
  try {
    const result = await api("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("#login-password").value }),
    });
    $("#login-password").value = "";
    applyStatus(result.setup);
    if (result.setup.setup_required) showStep(stepFor(result.setup));
    else await openDashboard();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST" }); } catch { /* already out */ }
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  state.setup = null;
  showLogin();
});

/* ----------------------------------------------------------------- wizard */

$("#admin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = $("#admin-password").value;
  const confirm = $("#admin-confirm").value;
  const errorEl = $("#admin-error");
  errorEl.hidden = true;
  if (password !== confirm) {
    errorEl.textContent = "The two passwords do not match.";
    errorEl.hidden = false;
    return;
  }
  try {
    const result = await api("/api/v1/setup/admin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    $("#admin-password").value = "";
    $("#admin-confirm").value = "";
    applyStatus(result.setup);
    showStep("model");
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

let selectedFile = null;

$("#drop").addEventListener("click", () => $("#model-file").click());
$("#model-file").addEventListener("change", (event) => {
  selectedFile = event.target.files[0] || null;
  $("#drop-title").textContent = selectedFile ? selectedFile.name : "Choose a scan file";
  $("#drop-note").textContent = selectedFile
    ? `${(selectedFile.size / (1024 * 1024)).toFixed(1)} MB · ready to import`
    : ".glb recommended · up to 150 MB";
  if (selectedFile && !$("#model-name").value) {
    $("#model-name").value = selectedFile.name.replace(/\.[^.]+$/, "");
  }
  $("#upload-model").disabled = !selectedFile;
});

$("#upload-model").addEventListener("click", async () => {
  if (!selectedFile) return;
  const button = $("#upload-model");
  const errorEl = $("#model-error");
  const statusEl = $("#upload-state");
  errorEl.hidden = true;
  button.disabled = true;
  statusEl.textContent = "Importing and analysing the mesh…";
  try {
    const name = $("#model-name").value.trim() || selectedFile.name;
    const result = await api("/api/v1/setup/model", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": selectedFile.name,
        "X-Model-Name": encodeURIComponent(name),
      },
      body: selectedFile,
    });
    state.model = result.model;
    state.floors = result.floors;
    if (state.setup) {
      state.setup.has_model = true;
      state.setup.model = result.model;
      state.setup.floors = result.floors;
    }
    statusEl.textContent = `${result.floors.length} floors, ${result.model.rooms_count} rooms found.`;
    showStep("floors");
  } catch (error) {
    statusEl.textContent = "";
    errorEl.textContent = error.message;
    errorEl.hidden = false;
    button.disabled = false;
  }
});

function renderFloorInputs() {
  const holder = $("#floor-inputs");
  holder.replaceChildren();
  const floors = (state.setup && state.setup.floors) || state.floors || [];
  if (!floors.length) {
    holder.textContent = "Import a model first.";
    return;
  }
  for (const floor of floors) {
    const row = document.createElement("div");
    row.className = "grid2";
    const label = document.createElement("label");
    label.textContent = `Floor ${floor.index} (index ${floor.index})`;
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 64;
    input.value = floor.name;
    input.dataset.floorIndex = String(floor.index);
    label.appendChild(input);
    const meta = document.createElement("p");
    meta.className = "muted small";
    meta.textContent = `${floor.rooms_count || 0} rooms detected · slab ${Number(floor.slab_top_y).toFixed(2)} m`;
    const cell = document.createElement("div");
    cell.append(label, meta);
    row.appendChild(cell);
    holder.appendChild(row);
  }
}

$("#save-floors").addEventListener("click", async () => {
  const errorEl = $("#floors-error");
  errorEl.hidden = true;
  const floors = $$("#floor-inputs input[data-floor-index]").map((input) => ({
    index: Number(input.dataset.floorIndex),
    name: input.value.trim(),
  }));
  if (floors.some((floor) => !floor.name)) {
    errorEl.textContent = "Every floor needs a name.";
    errorEl.hidden = false;
    return;
  }
  try {
    const result = await api("/api/v1/floors", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ floors }),
    });
    state.floors = result.floors;
    if (state.setup) state.setup.floors = result.floors;
    showStep("aps");
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

function apRow(label = "") {
  const row = document.createElement("div");
  row.className = "item ap-row";
  row.innerHTML = `
    <div class="grid2">
      <div><label>Name</label><input type="text" data-ap="label" maxlength="128" placeholder="Office AP"></div>
      <div><label>Floor</label><select data-ap="floor"></select></div>
    </div>
    <div class="grid2">
      <div><label>X (m)</label><input type="number" step="0.1" data-ap="x" placeholder="0"></div>
      <div><label>Z (m)</label><input type="number" step="0.1" data-ap="z" placeholder="0"></div>
    </div>
    <button class="btn ghost small remove-ap" type="button">Remove</button>`;
  row.querySelector('[data-ap="label"]').value = label;
  const select = row.querySelector('[data-ap="floor"]');
  const floors = (state.setup && state.setup.floors) || state.floors || [];
  for (const floor of floors) {
    const option = document.createElement("option");
    option.value = String(floor.index);
    option.textContent = floor.name;
    select.appendChild(option);
  }
  row.querySelector(".remove-ap").addEventListener("click", () => row.remove());
  return row;
}

function renderApRows() {
  const holder = $("#ap-rows");
  if (!holder.children.length) holder.appendChild(apRow());
}

$("#add-ap").addEventListener("click", () => $("#ap-rows").appendChild(apRow()));

$("#skip-aps").addEventListener("click", finishSetup);

$("#finish-setup").addEventListener("click", async () => {
  const errorEl = $("#aps-error");
  errorEl.hidden = true;
  const rows = $$("#ap-rows .ap-row");
  const aps = [];
  for (const row of rows) {
    const label = row.querySelector('[data-ap="label"]').value.trim();
    if (!label) continue;
    const floor = row.querySelector('[data-ap="floor"]').value;
    const x = row.querySelector('[data-ap="x"]').value;
    const z = row.querySelector('[data-ap="z"]').value;
    aps.push({
      label,
      floor: floor === "" ? null : Number(floor),
      x: x === "" ? null : Number(x),
      z: z === "" ? null : Number(z),
    });
  }
  try {
    if (aps.length) {
      await api("/api/v1/aps", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aps),
      });
    }
    await finishSetup();
  } catch (error) {
    errorEl.textContent = error.message;
    errorEl.hidden = false;
  }
});

async function finishSetup() {
  try {
    const status = await api("/api/v1/setup/complete", { method: "POST" });
    applyStatus(status);
    await openDashboard();
  } catch (error) {
    toast(error.message);
  }
}

$("#save-unifi").addEventListener("click", async () => {
  const statusEl = $("#unifi-state");
  statusEl.textContent = "Saving…";
  try {
    const result = await api("/api/v1/setup/unifi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: true,
        url: $("#unifi-url").value.trim(),
        username: $("#unifi-user").value.trim(),
        password: $("#unifi-pass").value,
        site: $("#unifi-site").value.trim() || "default",
        verify_tls: $("#unifi-tls").checked,
      }),
    });
    $("#unifi-pass").value = "";
    statusEl.textContent = result.enabled
      ? "Saved. Read-only polling is configured; the password stays on this host."
      : "Saved, disabled.";
  } catch (error) {
    statusEl.textContent = error.message;
  }
});

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((item) => {
      const on = item === tab;
      item.classList.toggle("is-on", on);
      item.setAttribute("aria-selected", String(on));
    });
    $$("[data-tabpanel]").forEach((panel) => {
      panel.hidden = panel.dataset.tabpanel !== tab.dataset.tab;
    });
  });
});

/* --------------------------------------------------------------- dashboard */

async function openDashboard() {
  show("view-app");
  await refreshAll();
  if (!state.poll) {
    state.poll = setInterval(() => {
      if (!$("#view-app").hidden && document.visibilityState === "visible") refreshAll();
    }, 10000);
  }
}

async function refreshAll() {
  try {
    const [model, nodes, stats, scans, aps] = await Promise.all([
      api("/api/v1/model"),
      api("/api/v1/nodes"),
      api("/api/v1/stats"),
      api("/api/v1/scans/recent?limit=12"),
      api("/api/v1/aps"),
    ]);
    renderModel(model);
    renderNodes(nodes);
    renderStats(stats);
    renderScans(scans);
    renderAps(aps);
  } catch (error) {
    if (error.status === 401) {
      showLogin();
      return;
    }
    toast(error.message);
  }
}

function renderModel(payload) {
  state.model = payload.model;
  state.floors = payload.floors || [];
  const title = $("#model-title");
  const sub = $("#model-sub");
  if (!payload.model) {
    title.textContent = "Your survey";
    sub.textContent = "No model imported.";
    $("#viewer-empty").hidden = false;
    $("#viewer-empty").textContent = "No model imported yet.";
    return;
  }
  title.textContent = payload.model.name;
  const warnings = payload.model.warnings.length;
  sub.textContent = `${plural(payload.model.floors_count, "floor")} · ${plural(payload.model.rooms_count, "room")}${warnings ? ` · ${plural(warnings, "warning")}` : ""}`;
  const list = $("#floors-list");
  list.replaceChildren();
  for (const floor of payload.floors) {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `<div class="item-top"><span class="item-name"></span><span class="item-meta"></span></div>`;
    item.querySelector(".item-name").textContent = floor.name;
    item.querySelector(".item-meta").textContent = `${floor.rooms.length} rooms`;
    list.appendChild(item);
  }
  buildFloorButtons(payload.floors);
  if (!state.viewer) initViewer();
}

function buildFloorButtons(floors) {
  const holder = $("#floor-buttons");
  holder.replaceChildren();
  if (!floors.length) return;
  const all = document.createElement("button");
  all.className = "btn ghost small is-on";
  all.type = "button";
  all.textContent = "All";
  all.addEventListener("click", () => selectFloor(null, all));
  holder.appendChild(all);
  for (const floor of floors) {
    const button = document.createElement("button");
    button.className = "btn ghost small";
    button.type = "button";
    button.textContent = floor.name;
    button.dataset.floor = String(floor.index);
    button.addEventListener("click", () => selectFloor(floor.index, button));
    holder.appendChild(button);
  }
}

function selectFloor(index, button) {
  $$("#floor-buttons .btn").forEach((item) => item.classList.toggle("is-on", item === button));
  const viewer = state.viewer;
  if (!viewer) return;
  for (const entry of viewer.floors) {
    entry.object.visible = index === null || entry.index === index;
  }
}

function renderStats(stats) {
  $("#stat-nodes").textContent = stats.nodes;
  $("#stat-scans").textContent = stats.scans;
  $("#stat-readings").textContent = stats.readings;
  $("#stat-bssids").textContent = stats.bssids;
}

function renderNodes(nodes) {
  const list = $("#nodes-list");
  list.replaceChildren();
  if (!nodes.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = "No nodes paired yet.";
    list.appendChild(p);
    return;
  }
  for (const node of nodes) {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <div class="item-top">
        <span class="item-name"><span class="dot"></span> <span data-name></span></span>
        <span class="item-meta" data-seen></span>
      </div>
      <div class="item-meta" data-meta></div>`;
    item.querySelector(".dot").classList.toggle("on", Boolean(node.online));
    item.querySelector("[data-name]").textContent = node.node_id;
    item.querySelector("[data-seen]").textContent = node.online ? "online" : "idle";
    item.querySelector("[data-meta]").textContent =
      `${node.node_type} · paired ${formatTime(node.paired_at)}${node.last_seen ? " · last seen " + formatTime(node.last_seen) : ""}`;
    list.appendChild(item);
  }
}

function renderScans(scans) {
  const body = $("#scans-body");
  body.replaceChildren();
  if (!scans.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="4" class="muted">No scans yet. Pair a node and start a scan.</td>';
    body.appendChild(row);
    return;
  }
  for (const scan of scans) {
    const row = document.createElement("tr");
    const cells = [
      formatTime(scan.timestamp),
      scan.node_id,
      scan.position && scan.position.floor !== null ? floorName(scan.position.floor) : "—",
      String(scan.n_readings),
    ];
    for (const value of cells) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    body.appendChild(row);
  }
}

function renderAps(aps) {
  const list = $("#aps-list");
  list.replaceChildren();
  if (!aps.length) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = "No APs configured.";
    list.appendChild(p);
    return;
  }
  for (const ap of aps) {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `<div class="item-top"><span class="item-name"></span><span class="item-meta"></span></div>`;
    item.querySelector(".item-name").textContent = ap.label;
    const where = ap.floor === null ? "unplaced" : floorName(ap.floor);
    item.querySelector(".item-meta").textContent =
      `${where}${ap.model ? " · " + ap.model : ""}${ap.bssids.length ? " · " + ap.bssids.length + " BSSIDs" : ""}`;
    list.appendChild(item);
  }
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function plural(count, word) {
  return `${count} ${word}${Number(count) === 1 ? "" : "s"}`;
}

function floorName(index) {
  if (index === null || index === undefined) return "—";
  const floor = (state.floors || []).find((item) => Number(item.index) === Number(index));
  return floor ? floor.name : `Floor ${index}`;
}

/* ------------------------------------------------------------------ 3D */

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(
      window.WebGLRenderingContext &&
      (canvas.getContext("webgl2") || canvas.getContext("webgl"))
    );
  } catch {
    return false;
  }
}

async function initViewer() {
  if (state.viewer || !state.model) return;
  const holder = $("#viewer");
  if (!webglAvailable()) {
    $("#viewer-empty").hidden = false;
    $("#viewer-empty").textContent = "This browser has no WebGL; the 3D view is unavailable.";
    return;
  }
  try {
    const THREE = await import("three");
    const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");
    const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x060b10);
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 500);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.setAttribute("aria-label", "3D floor model");
    holder.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    scene.add(new THREE.HemisphereLight(0xcfe9ff, 0x14212b, 1.15));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(8, 14, 6);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x7dd3fc, 0.5);
    fill.position.set(-10, 6, -8);
    scene.add(fill);

    const loader = new GLTFLoader();
    const gltf = await new Promise((resolve, reject) => {
      loader.load("/assets/model.glb", resolve, undefined, reject);
    });
    const root = gltf.scene;
    scene.add(root);

    const box = new THREE.Box3().setFromObject(root);
    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);
    const span = Math.max(size.x, size.z, 4);
    camera.position.set(center.x + span * 0.9, center.y + span * 1.0, center.z + span * 1.1);
    camera.near = 0.05;
    camera.far = span * 20;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();

    const floors = [];
    const rangeFor = (y) => {
      for (const floor of state.floors) {
        const bottom = Number(floor.slab_top_y) - 0.35;
        const top = Number(floor.ceiling_y || floor.slab_top_y + 3);
        if (y >= bottom && y < top) return floor.index;
      }
      return null;
    };
    const groups = new Map();
    root.traverse((child) => {
      if (!child.isMesh) return;
      const childBox = new THREE.Box3().setFromObject(child);
      const y = (childBox.min.y + childBox.max.y) / 2;
      const index = rangeFor(y);
      if (index === null) return;
      if (!groups.has(index)) groups.set(index, []);
      groups.get(index).push(child);
    });
    for (const [index, objects] of groups) {
      floors.push({ index, object: { visible: true, set visible(value) { objects.forEach((mesh) => { mesh.visible = value; }); }, get visible() { return objects[0].visible; } } });
    }

    const resize = () => {
      const width = holder.clientWidth || 640;
      const height = holder.clientHeight || 360;
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    resize();
    if (window.ResizeObserver) new ResizeObserver(resize).observe(holder);

    const tick = () => {
      controls.update();
      renderer.render(scene, camera);
      state.viewer.raf = requestAnimationFrame(tick);
    };
    state.viewer = { scene, camera, renderer, controls, floors };
    $("#viewer-empty").hidden = true;
    tick();
  } catch {
    $("#viewer-empty").hidden = false;
    $("#viewer-empty").textContent = "The 3D preview could not be initialised.";
  }
}

/* -------------------------------------------------------------- pairing */

$("#pair-button").addEventListener("click", async () => {
  try {
    const result = await api("/api/v1/pairing/code", { method: "POST" });
    $("#pair-panel").hidden = false;
    $("#pair-code").textContent = result.code;
    const expires = new Date(result.expires_at);
    const update = () => {
      const seconds = Math.max(0, Math.round((expires.getTime() - Date.now()) / 1000));
      $("#pair-expiry").textContent = seconds
        ? `Expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
        : "Expired — generate a new code.";
      if (!seconds) clearInterval(state.pairTimer);
    };
    clearInterval(state.pairTimer);
    update();
    state.pairTimer = setInterval(update, 1000);
  } catch (error) {
    toast(error.message);
  }
});

$("#refresh-nodes").addEventListener("click", refreshAll);

/* ----------------------------------------------------------------- boot */

async function boot() {
  try {
    const status = await api("/api/v1/setup/status");
    applyStatus(status);
    if (!status.has_admin) {
      showStep("admin");
      return;
    }
    const session = await trySession();
    if (!session) {
      showLogin();
      return;
    }
    if (session.setup_required) showStep(stepFor(session));
    else await openDashboard();
  } catch (error) {
    show("view-login");
    $("#login-error").textContent = error.message;
    $("#login-error").hidden = false;
  }
}

boot();
