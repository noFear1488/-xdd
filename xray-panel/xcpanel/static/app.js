"use strict";

// --- tiny helpers -----------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch("api/" + path, {
    method,
    credentials: "same-origin",
    headers: { "X-Requested-With": "xcpanel", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401 && path !== "login") { showLogin(); throw new ApiError(401, "Please sign in again"); }
  if (!res.ok) throw new ApiError(res.status, data.error || res.statusText);
  return data;
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 2500);
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const area = h("textarea", { style: { position: "fixed", opacity: "0" } }, text);
    document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
  }
  toast("Copied");
}

function qrSvg(text) {
  for (const level of ["M", "L"]) {
    try {
      const qr = qrcode(0, level);
      qr.addData(text);
      qr.make();
      return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
    } catch (_) { /* too long for this level, try a lower one */ }
  }
  return "<p>Too long for a QR code</p>";
}

function qrBox(text, cls = "qr") {
  const box = h("div", { class: cls, role: "img", "aria-label": "QR code" });
  box.innerHTML = qrSvg(text); // markup from the QR library; contains no user text
  return box;
}

const GB = 1024 ** 3;
function size(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i < 2 ? v.toFixed(0) : v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)) + " " + units[i];
}

function ago(ts, now) {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}

function expiryText(ts, now) {
  if (!ts) return "No expiry";
  const days = Math.ceil((ts - now) / 86400);
  const date = new Date(ts * 1000).toLocaleDateString();
  if (days < 0) return "Expired " + date;
  if (days === 0) return "Expires today";
  return `${days} day${days === 1 ? "" : "s"} left · ${date}`;
}

const STATUS = { active: "Active", disabled: "Disabled", expired: "Expired", limited: "Over limit" };
const TRANSPORT_LABELS = { xhttp: "XHTTP", ws: "WS", grpc: "gRPC", reality: "Reality" };

// --- modal ------------------------------------------------------------------

function openModal(title, ...content) {
  $("#modal-title").textContent = title;
  const body = $("#modal-body");
  body.replaceChildren(...content.filter((c) => c !== null && c !== undefined && c !== false));
  $("#modal").hidden = false;
  const first = body.querySelector("input, select, textarea, button");
  if (first) first.focus();
}
function closeModal() { $("#modal").hidden = true; $("#modal-body").replaceChildren(); }

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]") || e.target.id === "modal") closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modal").hidden) closeModal(); });

async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  try { return await fn(); }
  catch (err) { toast(err.message, true); }
  finally { button.disabled = false; button.textContent = label; }
}

// --- state ------------------------------------------------------------------

let state = null;
let currentTab = "clients";

async function refresh() {
  state = await api("GET", "state");
  $("#domain").textContent = state.settings.domain;
  $("#version").textContent = "xcpanel " + state.version;
  renderClients();
  renderNodes();
  if (!refresh.settingsFilled) { fillSettings(); refresh.settingsFilled = true; }
}

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-form [name=username]").focus();
}

async function start() {
  try {
    await refresh();
    $("#login").hidden = true;
    $("#app").hidden = false;
  } catch (err) {
    if (err.status !== 401) toast(err.message, true);
  }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const error = $("#login-error");
  error.hidden = true;
  try {
    await api("POST", "login", { username: form.username.value, password: form.password.value });
    form.password.value = "";
    await start();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("POST", "logout").catch(() => {});
  state = null;
  refresh.settingsFilled = false;
  showLogin();
});

for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => {
    currentTab = tab.dataset.tab;
    for (const t of document.querySelectorAll(".tabs button")) t.classList.toggle("active", t === tab);
    for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== "tab-" + currentTab;
  });
}

setInterval(() => {
  if (state && !document.hidden && $("#modal").hidden && currentTab !== "settings") refresh().catch(() => {});
}, 15000);

// --- clients ----------------------------------------------------------------

$("#search").addEventListener("input", () => renderClients());
$("#add-client").addEventListener("click", () => clientForm(null));

function renderClients() {
  const { clients, now } = state;
  const active = clients.filter((c) => c.status === "active").length;
  const online = clients.filter((c) => c.online).length;
  const total = clients.reduce((sum, c) => sum + c.used, 0);
  $("#stats").replaceChildren(
    ...[[clients.length, "Clients"], [active, "Active"], [online, "Online now"], [size(total), "Traffic, all time"]]
      .map(([value, label]) => h("div", { class: "stat" }, h("div", { class: "value" }, value), h("div", { class: "label" }, label))),
  );

  const query = $("#search").value.trim().toLowerCase();
  const shown = clients.filter((c) => !query || c.name.toLowerCase().includes(query) || c.note.toLowerCase().includes(query));
  const list = $("#clients");
  if (!clients.length) {
    list.replaceChildren(h("div", { class: "card empty" }, "No clients yet. Add one, then share its subscription link."));
    return;
  }
  list.replaceChildren(...shown.map((c) => clientRow(c, now)));
}

function clientRow(c, now) {
  const share = (c.limit_bytes ? c.used / c.limit_bytes : 0);
  const bar = h("span");
  bar.style.width = Math.min(100, share * 100).toFixed(1) + "%";
  return h("div", { class: "card row" },
    h("div", {},
      h("div", { class: "name" },
        h("span", { class: "dot" + (c.online ? " on" : ""), title: c.online ? "Online" : "Offline" }),
        c.name,
        h("span", { class: "pill " + c.status }, STATUS[c.status])),
      h("div", { class: "sub" }, c.note || "Last seen " + ago(c.last_active, now))),
    h("div", { class: "usage" },
      h("div", { class: "nums" },
        h("span", {}, size(c.used)),
        h("span", { class: "muted" }, c.limit_bytes ? "of " + size(c.limit_bytes) : "no limit")),
      h("div", { class: "bar" + (share >= 1 ? " bad" : share >= 0.8 ? " warn" : "") }, bar),
      h("div", { class: "sub" }, `↑ ${size(c.up_bytes)}  ↓ ${size(c.down_bytes)}`)),
    h("div", { class: "sub" }, expiryText(c.expire_at, now)),
    h("div", { class: "actions" },
      h("button", { class: "btn small primary", onclick: () => shareClient(c) }, "Share"),
      h("button", { class: "btn small", onclick: () => clientForm(c) }, "Edit"),
      h("button", { class: "btn small ghost", onclick: () => clientMenu(c) }, "More")));
}

function clientForm(c) {
  const now = state.now;
  const form = h("form", { class: "form" },
    h("label", {}, "Name", h("input", { name: "name", required: true, maxlength: "64", value: c ? c.name : "" })),
    h("div", { class: "grid2" },
      h("label", {}, "Traffic limit, GB (0 = unlimited)",
        h("input", { name: "limit_gb", type: "number", min: "0", step: "any", value: c ? +(c.limit_bytes / GB).toFixed(2) : 0 })),
      h("label", {}, "Expires on (empty = never)",
        h("input", { name: "expire", type: "date", value: c && c.expire_at ? toDateInput(c.expire_at) : "" }))),
    h("div", { class: "quick" },
      h("span", { class: "muted" }, "Expiry:"),
      ...[[30, "+30 days"], [90, "+90 days"], [365, "+1 year"], [0, "Never"]].map(([days, label]) =>
        h("button", {
          type: "button", class: "btn small",
          onclick: () => {
            const base = c && c.expire_at > now ? c.expire_at : now;
            form.expire.value = days ? toDateInput(base + days * 86400) : "";
          },
        }, label))),
    h("label", {}, "Note", h("input", { name: "note", maxlength: "500", value: c ? c.note : "", placeholder: "optional, e.g. who this is for" })),
    h("label", { class: "check" }, h("input", { name: "enabled", type: "checkbox", checked: c ? c.enabled : true }), "Enabled"),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "btn ghost", "data-close": true }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, c ? "Save" : "Add client")));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const button = form.querySelector("[type=submit]");
    busy(button, async () => {
      const body = {
        name: form.name.value,
        limit_gb: form.limit_gb.value || 0,
        expire_at: form.expire.value ? fromDateInput(form.expire.value) : 0,
        note: form.note.value,
        enabled: form.enabled.checked,
      };
      const saved = c ? await api("PUT", `clients/${c.id}`, body) : await api("POST", "clients", body);
      await refresh();
      if (!c) shareClient(saved); else { closeModal(); toast("Saved. Nodes update in a few seconds."); }
    });
  });
  openModal(c ? "Edit client" : "New client", form);
}

// A date input means "through the end of that day" in the admin's time zone.
function toDateInput(ts) {
  const d = new Date(ts * 1000 - 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function fromDateInput(value) {
  const [y, m, d] = value.split("-").map(Number);
  return Math.floor(new Date(y, m - 1, d + 1).getTime() / 1000);
}

async function shareClient(c) {
  let data;
  try { data = await api("GET", `clients/${c.id}/links`); }
  catch (err) { toast(err.message, true); return; }
  const linkItems = data.links.map((link) =>
    h("li", {},
      h("span", { class: "badge" }, link.transport),
      h("span", { class: "grow" }, link.node),
      h("button", { class: "btn small", onclick: () => copy(link.url) }, "Copy"),
      h("button", { class: "btn small ghost", onclick: () => openModal(`${link.node} · ${link.transport}`, qrBox(link.url), backButton(c)) }, "QR")));
  openModal(`Share: ${c.name}`,
    h("p", { class: "muted" }, "One subscription link covers every node. Import it in Hiddify, v2RayTun, Happ, Streisand, v2rayNG or NekoBox; apps refresh it on their own."),
    qrBox(data.sub_url),
    h("div", { class: "copy-row" },
      h("input", { readonly: true, value: data.sub_url, "aria-label": "Subscription link" }),
      h("button", { class: "btn primary", onclick: () => copy(data.sub_url) }, "Copy")),
    h("p", { class: "hint" }, "Opening the link in a browser shows a page with usage, a QR code and import buttons — handy to send as is."),
    h("h2", {}, "Single configs"),
    data.links.length ? h("ul", { class: "link-list" }, linkItems)
      : h("p", { class: "muted" }, "No enabled transports on any node yet."),
    c.status !== "active" ? h("p", { class: "error" }, `This client is ${STATUS[c.status].toLowerCase()}: the links will not connect until that changes.`) : null);
}

function backButton(c) {
  return h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => shareClient(c) }, "Back"));
}

function clientMenu(c) {
  const action = (label, cls, fn) => h("button", { class: "btn " + cls, onclick: (e) => busy(e.target, fn) }, label);
  openModal(c.name,
    h("div", { class: "list" },
      action(c.enabled ? "Disable" : "Enable", "", async () => {
        await api("PUT", `clients/${c.id}`, { ...c, limit_gb: c.limit_bytes / GB, enabled: !c.enabled });
        await refresh(); closeModal(); toast(c.enabled ? "Disabled" : "Enabled");
      }),
      action("Reset traffic counter", "", async () => {
        await api("POST", `clients/${c.id}/reset`); await refresh(); closeModal(); toast("Traffic reset");
      }),
      action("New UUID and subscription link", "", async () => {
        if (!confirm("The old link and configs stop working. Continue?")) return;
        await api("POST", `clients/${c.id}/regenerate`); await refresh(); closeModal(); toast("New link created");
      }),
      action("Delete client", "danger", async () => {
        if (!confirm(`Delete ${c.name}? This cannot be undone.`)) return;
        await api("DELETE", `clients/${c.id}`); await refresh(); closeModal(); toast("Deleted");
      })),
    h("p", { class: "hint" }, "UUID: ", h("code", {}, c.uuid)));
}

// --- nodes ------------------------------------------------------------------

$("#add-node").addEventListener("click", async () => {
  try {
    const defaults = await api("GET", "node-defaults");
    nodeForm(null, defaults.transports);
  } catch (err) { toast(err.message, true); }
});

function deployPill(n) {
  if (!n.enabled && !n.deploy.pending && n.deploy.ok) return h("span", { class: "pill disabled" }, "Disabled");
  if (n.deploy.pending && n.deploy.ok !== false) return h("span", { class: "pill pending" }, n.deploy.at ? "Applying…" : "Not deployed");
  if (n.deploy.ok === false) return h("span", { class: "pill failed" }, "Deploy failed");
  if (n.deploy.ok) return h("span", { class: "pill ok" }, "Up to date");
  return h("span", { class: "pill never" }, "Never deployed");
}

function renderNodes() {
  const { nodes, now } = state;
  $("#nodes").replaceChildren(...nodes.map((n) => {
    const chips = Object.keys(TRANSPORT_LABELS).filter((k) => n.transports[k] && n.transports[k].enabled).map((k) => {
      const t = n.transports[k];
      let text = TRANSPORT_LABELS[k];
      if (k === "reality") text += n.reality_front ? " :443 (in front of Caddy)" : ` :${t.port}`;
      return h("span", { class: "badge" }, text);
    });
    const log = h("pre", { class: "log", hidden: n.deploy.ok !== false }, n.deploy.log || "");
    return h("div", { class: "card" },
      h("div", { class: "node-head" }, h("h3", {}, n.name), deployPill(n)),
      h("div", { class: "node-meta" },
        h("span", {}, "Address ", h("b", {}, n.address)),
        h("span", {}, n.local ? h("b", {}, "This server") : ["SSH ", h("b", {}, `${n.ssh_host}:${n.ssh_port}`)]),
        h("span", {}, "Last deploy ", h("b", {}, ago(n.deploy.at, now)))),
      h("div", { class: "chips" }, chips.length ? chips : h("span", { class: "muted" }, "No transports enabled")),
      log,
      h("div", { class: "node-actions" },
        h("button", { class: "btn small primary", onclick: (e) => busy(e.target, () => deployNode(n, e.target)) }, "Deploy now"),
        h("button", { class: "btn small", onclick: () => nodeForm(n, n.transports) }, "Edit"),
        h("button", { class: "btn small", onclick: (e) => busy(e.target, () => checkNode(n)) }, "Check"),
        h("button", { class: "btn small ghost", onclick: () => showConfig(n) }, "View config"),
        n.deploy.log ? h("button", { class: "btn small ghost", onclick: () => { log.hidden = !log.hidden; } }, "Log") : null,
        n.local ? null : h("button", { class: "btn small ghost danger", onclick: (e) => busy(e.target, () => deleteNode(n)) }, "Remove")));
  }));
}

async function deployNode(n, button) {
  button.textContent = "Deploying…";
  const res = await api("POST", `nodes/${n.id}/deploy`);
  await refresh();
  if (res.ok) toast(`${n.name}: deployed`);
  else openModal(`${n.name}: deploy failed`, h("pre", { class: "log" }, res.output), h("p", { class: "hint" }, "The node keeps running its previous config."));
}

async function checkNode(n) {
  const res = await api("POST", `nodes/${n.id}/check`);
  openModal(`${n.name}: ${res.ok ? "reachable" : "problem"}`, h("pre", { class: "log" }, res.output || "(no output)"));
}

async function deleteNode(n) {
  if (!confirm(`Remove ${n.name} from the panel? Xray keeps running there with its last config.`)) return;
  await api("DELETE", `nodes/${n.id}`);
  await refresh();
  toast("Removed");
}

async function showConfig(n) {
  const cfg = await api("GET", `nodes/${n.id}/config`).catch((err) => { toast(err.message, true); });
  if (!cfg) return;
  openModal(`${n.name}: generated config`,
    h("h2", {}, "Xray — /usr/local/etc/xray/config.json"),
    h("pre", { class: "log" }, cfg.xray),
    cfg.caddy ? h("h2", {}, "Caddy — /etc/caddy/Caddyfile") : null,
    cfg.caddy ? h("pre", { class: "log" }, cfg.caddy) : null);
}

function nodeForm(n, transports) {
  const t = JSON.parse(JSON.stringify(transports));
  const isLocal = n ? n.local : false;
  const field = (label, name, value, attrs = {}) => h("label", {}, label, h("input", { name, value: value ?? "", ...attrs }));

  const section = (key, title, hint, ...fields) => {
    const box = h("fieldset", { class: t[key].enabled ? "" : "off" },
      h("legend", {}, h("label", { class: "check" },
        h("input", { type: "checkbox", name: key + ".enabled", checked: t[key].enabled,
          onchange: (e) => box.classList.toggle("off", !e.target.checked) }),
        title)),
      h("p", { class: "hint" }, hint),
      h("div", { class: "grid3" }, ...fields));
    return box;
  };

  const realityKeys = t.reality.public_key
    ? h("p", { class: "hint" }, "Public key ", h("code", {}, t.reality.public_key), " · short id ", h("code", {}, t.reality.short_id))
    : h("p", { class: "hint" }, "Keys are generated when you save.");

  const form = h("form", { class: "form" },
    h("div", { class: "grid2" },
      field("Name (shown in client apps)", "name", n ? n.name : "", { required: true, maxlength: "64", placeholder: "🇩🇪 Germany" }),
      field("Address clients connect to", "address", n ? n.address : "", { required: true, placeholder: "de.nordpine.site" })),
    isLocal ? h("p", { class: "hint" }, "This is the server the panel runs on.")
      : h("div", { class: "grid2" },
        field("SSH target", "ssh_host", n ? n.ssh_host : "", { required: true, placeholder: "root@203.0.113.7" }),
        field("SSH port", "ssh_port", n ? n.ssh_port : 22, { type: "number", min: "1", max: "65535" })),
    isLocal ? null : h("label", { class: "check" }, h("input", { type: "checkbox", name: "caddy", checked: n ? n.caddy : true }),
      "Run Caddy on this node (needed for XHTTP/WS/gRPC and for Reality on 443 behind a real site)"),
    h("p", { class: "hint" }, "XHTTP, WS and gRPC go over normal HTTPS to the address above, so it must be a domain whose DNS points to this node."),
    section("xhttp", "XHTTP", "Recommended. Looks like ordinary HTTPS traffic to your site.",
      field("Path", "xhttp.path", t.xhttp.path), field("Local port", "xhttp.port", t.xhttp.port, { type: "number" })),
    section("reality", "VLESS Reality (Vision)",
      "Port 443 with Caddy: Xray takes 443 and forwards everything that is not a Reality client to your real site (best). Another port, or no Caddy: Reality borrows the TLS handshake of the target site below.",
      field("Port", "reality.port", t.reality.port, { type: "number" }),
      field("SNI (non-443 / no Caddy)", "reality.sni", t.reality.sni),
      field("Target", "reality.dest", t.reality.dest)),
    t.reality.enabled ? realityKeys : null,
    n ? h("label", { class: "check" }, h("input", { type: "checkbox", name: "reality.regenerate" }), "Generate new Reality keys (old links stop working)") : null,
    section("ws", "WebSocket (legacy)", "Older clients only; Xray marks it deprecated.",
      field("Path", "ws.path", t.ws.path), field("Local port", "ws.port", t.ws.port, { type: "number" })),
    section("grpc", "gRPC (legacy)", "Older clients only; Xray marks it deprecated.",
      field("Service name", "grpc.service", t.grpc.service), field("Local port", "grpc.port", t.grpc.port, { type: "number" })),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "enabled", checked: n ? n.enabled : true }), "Node enabled (when off it leaves the subscriptions and nobody can connect to it)"),
    h("p", { class: "error", hidden: true }),
    h("div", { class: "actions" },
      isLocal ? null : h("button", { type: "button", class: "btn ghost", onclick: () => setupScript(form) }, "Setup script for the server"),
      h("button", { type: "button", class: "btn ghost", "data-close": true }, "Cancel"),
      h("button", { type: "submit", class: "btn primary" }, n ? "Save" : "Add node")));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const val = (name) => form.elements[name] ? form.elements[name].value : undefined;
    const on = (name) => !!(form.elements[name] && form.elements[name].checked);
    const body = {
      name: val("name"),
      address: val("address"),
      ssh_host: isLocal ? "" : val("ssh_host"),
      ssh_port: isLocal ? 22 : val("ssh_port"),
      caddy: isLocal ? true : on("caddy"),
      enabled: on("enabled"),
      transports: {
        xhttp: { enabled: on("xhttp.enabled"), path: val("xhttp.path"), port: val("xhttp.port") },
        ws: { enabled: on("ws.enabled"), path: val("ws.path"), port: val("ws.port") },
        grpc: { enabled: on("grpc.enabled"), service: val("grpc.service"), port: val("grpc.port") },
        reality: {
          enabled: on("reality.enabled"), port: val("reality.port"), sni: val("reality.sni"),
          dest: val("reality.dest"), short_id: t.reality.short_id, regenerate: on("reality.regenerate"),
        },
      },
    };
    const error = form.querySelector(".error");
    error.hidden = true;
    const button = form.querySelector("[type=submit]");
    button.disabled = true;
    (n ? api("PUT", `nodes/${n.id}`, body) : api("POST", "nodes", body))
      .then(async () => {
        await refresh();
        closeModal();
        toast(n ? "Saved. Deploying in a few seconds." : "Node added. Press Deploy once the server is prepared.");
      })
      .catch((err) => { error.textContent = err.message; error.hidden = false; })
      .finally(() => { button.disabled = false; });
  });
  openModal(n ? `Edit node: ${n.name}` : "Add node", form);
}

async function setupScript(form) {
  const withCaddy = form.elements.caddy ? form.elements.caddy.checked : true;
  let data;
  try { data = await api("GET", "node-setup?caddy=" + (withCaddy ? 1 : 0)); }
  catch (err) { toast(err.message, true); return; }
  const area = h("textarea", { readonly: true, rows: "10" }, data.script);
  const box = h("div", { class: "form" },
    h("p", {}, "Log in to the new server as root and paste this. It installs Xray", withCaddy ? " and Caddy" : "", " and lets the panel in with its SSH key:"),
    area,
    h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: () => copy(data.script) }, "Copy script")),
    h("p", { class: "hint" }, "Panel key: ", h("code", {}, data.public_key)));
  // Keep the half-filled node form: show the script in a native dialog-like overlay on top.
  const overlay = h("div", { class: "modal" }, h("div", { class: "modal-box" },
    h("div", { class: "modal-head" }, h("h2", {}, "Prepare the server"),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: () => overlay.remove() }, "✕")),
    box));
  overlay.style.zIndex = "25";
  document.body.append(overlay);
}

// --- settings ---------------------------------------------------------------

function fillSettings() {
  const s = state.settings;
  const form = $("#settings-form");
  for (const key of ["domain", "acme_email", "panel_path", "sub_path", "sub_title", "sub_update_hours", "fingerprint", "stats_interval"]) {
    form.elements[key].value = s[key] ?? "";
  }
  form.elements.block_torrents.checked = !!s.block_torrents;
  $("#password-form").elements.username.value = s.admin_user;
}

$("#settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  busy(form.querySelector("[type=submit]"), async () => {
    const body = {};
    for (const key of ["domain", "acme_email", "panel_path", "sub_path", "sub_title", "sub_update_hours", "fingerprint", "stats_interval"]) {
      body[key] = form.elements[key].value;
    }
    body.block_torrents = form.elements.block_torrents.checked;
    const res = await api("PUT", "settings", body);
    if (res.redirect) { toast("Moved. Taking you to the new address…"); setTimeout(() => { location.href = res.redirect; }, 1200); return; }
    await refresh();
    toast("Settings saved");
  });
});

$("#password-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  busy(form.querySelector("[type=submit]"), async () => {
    await api("POST", "password", { username: form.username.value, current: form.current.value, new: form.new.value });
    form.current.value = form.new.value = "";
    toast("Password changed; other sessions were signed out");
  });
});

start();
