"use strict";

/* ---------- helpers (also used by thesis.js) ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pretty = (k) => String(k).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u || ""; } };
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");  // never render javascript: or other schemes as links
const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(Number(n)) ? "–" : Number(n).toFixed(d).replace(/\.?0+$/, "") || "0");
const mb = (b) => `${(b / 1048576).toFixed(1)} MB`;

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Requested-With"] = "screening"; // the server rejects writes without it
  if (form) opts.body = form;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch(path, opts); } catch { throw new Error("The app's server is not responding. Is `python app.py` still running?"); }
  if (res.status === 401 && !path.startsWith("/api/login")) { location.href = "/login"; throw new Error("Signing in..."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const err = new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `Request failed (${res.status})`);
    err.can_save_anyway = !!data.can_save_anyway;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}
async function copy(text, what) {
  try { await navigator.clipboard.writeText(text); toast(`${what} copied`); } catch { toast("Copy failed - the browser blocked clipboard access"); }
}

const STATUS = {
  verified: { label: "Verified", hint: "Confirmed by an independent source" },
  consistent: { label: "Company only", hint: "Only the company's own materials or self-reported profiles agree" },
  unverified: { label: "Unverified", hint: "No evidence either way" },
  contradicted: { label: "Contradicted", hint: "Evidence states something different" },
  outdated: { label: "Outdated", hint: "Newer evidence replaces it" },
  not_checked: { label: "Not checked", hint: "Low importance or over the per-run limit" },
};
const KIND = { independent: "Independent", profile: "Self-reported", company: "Company" };
const SEVERITY = { error: "tone-bad", warning: "tone-warn", info: "tone-info" };
const PALETTE = ["#5B4CF0", "#14C8B0", "#FF6B9A", "#E08A00", "#8B5CF6", "#12A878", "#2563EB", "#C04BE8", "#0891B2", "#7A7EA0", "#B4B8D9"];
const badge = (status) => `<span class="badge badge--${esc(status)}" title="${esc(STATUS[status]?.hint || "")}">${esc(STATUS[status]?.label || status)}</span>`;

/* ---------- routing ---------- */
const views = { screen: "#view-screen", history: "#view-history", thesis: "#view-thesis", keys: "#view-keys", run: "#view-run" };
let lastHash = location.hash || "#/screen";
async function route() {
  const target = location.hash || "#/screen";
  if (route.restoring) { route.restoring = false; return; }
  if (typeof thesisDirty !== "undefined" && thesisDirty && lastHash.startsWith("#/thesis/") && target !== lastHash) {  // editor has unsaved edits
    if (!confirm("You have unsaved changes to this fund thesis. Leave without saving?")) { route.restoring = true; location.hash = lastHash; return; }
    setDirty(false);
  }
  lastHash = target;
  const [, name = "screen", id] = target.split("/");
  const key = views[name] ? name : "screen";
  for (const [k, sel] of Object.entries(views)) $(sel).hidden = k !== key;
  for (const a of $$("[data-nav]")) {
    if (a.dataset.nav === (key === "run" ? "history" : key)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  document.title = `${{ screen: "New screening", history: "History", thesis: "Fund theses", keys: "AI keys", run: "Report" }[key]} · Deal Screening`;
  if (key === "screen") await refreshTheses();
  if (key === "history") await loadHistory();
  if (key === "thesis") await loadThesis(id);
  if (key === "keys") await loadKeys();
  if (key === "run") await loadRun(Number(id));
  if (route.ready) { window.scrollTo(0, 0); const h = $(`${views[key]} h1`); if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); } }
  route.ready = true;
}
window.addEventListener("hashchange", route);

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const pill = $("#key-pill");
    pill.hidden = false;
    pill.className = `key-pill${h.key ? "" : " key-pill--missing"}`;
    pill.textContent = h.key ? `${h.ai}${h.ai_count > 1 ? ` +${h.ai_count - 1}` : ""} ready · ${h.search}` : "No AI key";
    $("#no-key").hidden = h.key;
    if (!h.key) $("#screen-body").hidden = true;
    const pe = $("#profiles-error");
    pe.hidden = !h.profiles_error; pe.textContent = h.profiles_error || "";
    $("#go").disabled = !h.key || !!h.profiles_error;
  } catch { /* the form reports server problems on submit */ }
}

/* ---------- fund theses on the new-screening page ---------- */
let theses = [];
const selectedThesis = () => theses.find((t) => String(t.id) === $("#thesis-select").value) || null;

async function refreshTheses() {
  try { theses = (await api("/api/theses")).filter((t) => t.data); } catch { theses = []; }
  const usable = theses.filter((t) => !t.errors.length);
  const sel = $("#thesis-select"), saved = localStorage.getItem("thesisId");     // "" means the user last chose an open screening
  sel.innerHTML = `<option value="">Open screening: any country, stage and sector</option>` +
    theses.map((t) => `<option value="${t.id}"${t.errors.length ? " disabled" : ""}>${esc(t.name)}${t.errors.length ? " (needs fixing)" : ""}</option>`).join("");
  const keep = saved === "" ? null : usable.find((t) => String(t.id) === String(saved)) || usable[0];
  sel.value = keep ? String(keep.id) : "";
  showThesisSummary();
}
function showThesisSummary() {
  const t = selectedThesis(), el = $("#thesis-summary");
  if (!t) {
    el.textContent = "No thesis: the company is screened openly (any country, stage and sector, standard thresholds, default weights). A thesis gives better results.";
    $("#thesis-edit").hidden = true; localStorage.setItem("thesisId", ""); return;
  }
  $("#thesis-edit").hidden = false;
  const s = t.summary, plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const facts = [s.countries ? plural(s.countries, "country").replace("countrys", "countries") : "any country", s.stages.join(", ") || "any stage",
    s.sectors ? plural(s.sectors, "target sector") : "any sector", s.ticket[0] || s.ticket[1] ? `EUR ${(s.ticket[0] / 1e6).toFixed(1)}M-${(s.ticket[1] / 1e6).toFixed(1)}M rounds` : "no round limits",
    `advance at ${fmt(s.advance_at)}`];
  el.innerHTML = esc(facts.join(" · ")) + (t.warnings.length ? `<br><span class="warnline">${esc(t.warnings[0])}</span>` : "");
  $("#thesis-edit").href = `#/thesis/${t.id}`;
  localStorage.setItem("thesisId", String(t.id));
}
$("#thesis-select").addEventListener("change", showThesisSummary);
for (const sel of ["#td-create", "a[href='#/thesis/new']"]) {
  document.querySelector("#view-screen").addEventListener("click", (e) => { if (e.target.closest(sel)) sessionStorage.setItem("afterThesis", "screen"); });  // come back after creating
}

/* ---------- new screening form ---------- */
const form = $("#screen-form");
let deckFile = null, resFiles = [];
const RES_EXT = [".pdf", ".docx", ".txt", ".md", ".csv"];

function fieldError(id, msg) {
  const el = $(`#${id}-err`);
  el.textContent = msg || ""; el.hidden = !msg;
  const input = id === "deck" ? $("#deck-drop") : id === "res" ? $("#res-drop") : $(`#${id}`);
  if (id === "website") input.setAttribute("aria-invalid", msg ? "true" : "false");
  else input.classList.toggle("is-invalid", !!msg);
}

function setDeck(f) {
  fieldError("deck", "");
  if (f && !/\.pdf$/i.test(f.name)) return fieldError("deck", "The pitch deck must be a PDF.");
  if (f && f.size > 20 * 1048576) return fieldError("deck", `That PDF is ${mb(f.size)}; the limit is 20 MB.`);
  deckFile = f || null;
  $("#deck-drop").hidden = !!deckFile;
  $("#deck-file").innerHTML = deckFile ? `<div class="file-row file-row--deck"><span class="file-row__name">${esc(deckFile.name)}</span>
    <span class="file-row__meta">${mb(deckFile.size)}</span><button type="button" class="icon-btn" id="deck-clear" aria-label="Remove pitch deck">✕</button></div>` : "";
  $("#deck-clear")?.addEventListener("click", () => { $("#deck").value = ""; setDeck(null); $("#deck").focus(); });
}

function addResources(files) {
  const errs = [];
  for (const f of files) {
    const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (!RES_EXT.includes(ext)) { errs.push(`${f.name}: use PDF, DOCX, TXT, MD or CSV.`); continue; }
    if (f.size > 20 * 1048576) { errs.push(`${f.name}: over 20 MB.`); continue; }
    if (resFiles.some((x) => x.name === f.name && x.size === f.size)) continue;
    if (resFiles.length >= 5) { errs.push("At most 5 additional files."); break; }
    resFiles.push(f);
  }
  fieldError("res", errs.join(" "));
  renderResources();
}
function renderResources() {
  $("#res-files").innerHTML = resFiles.map((f, i) => `<li class="file-row"><span class="file-row__name">${esc(f.name)}</span>
    <span class="file-row__meta">${mb(f.size)}</span><button type="button" class="icon-btn" data-rm="${i}" aria-label="Remove ${esc(f.name)}">✕</button></li>`).join("");
}
$("#res-files").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rm]"); if (!b) return;
  resFiles.splice(Number(b.dataset.rm), 1); renderResources(); $("#resources").focus();
});
$("#deck").addEventListener("change", (e) => setDeck(e.target.files[0]));
$("#resources").addEventListener("change", (e) => { addResources([...e.target.files]); e.target.value = ""; });
for (const [id, onDrop] of [["deck-drop", (fs) => setDeck(fs[0])], ["res-drop", (fs) => addResources([...fs])]]) {
  const el = $(`#${id}`);
  ["dragenter", "dragover"].forEach((ev) => el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => el.addEventListener(ev, (e) => { e.preventDefault(); el.classList.remove("is-over"); }));
  el.addEventListener("drop", (e) => onDrop(e.dataTransfer.files));
}
$("#website").addEventListener("input", () => fieldError("website", ""));

function showFormError(msg) { const e = $("#form-error"); e.textContent = msg; e.hidden = !msg; }

form.addEventListener("submit", (e) => { e.preventDefault(); submitScreening(false); });
$("#td-create").addEventListener("click", () => $("#thesis-dialog").close());
$("#thesis-dialog").addEventListener("close", () => { if ($("#thesis-dialog").returnValue === "open") submitScreening(true); });

async function submitScreening(openConfirmed) {
  showFormError("");
  const site = $("#website").value.trim();
  let firstBad = null;
  const thesis = selectedThesis();
  if (!deckFile) { fieldError("deck", "Upload the pitch deck (PDF)."); firstBad = firstBad || $("#deck"); }
  if (!site) { fieldError("website", "Enter the company's website."); firstBad = firstBad || $("#website"); }
  else if (!/^(https?:\/\/)?[^\s/]+\.[^\s/]{2,}/i.test(site)) { fieldError("website", "That does not look like a website address."); firstBad = firstBad || $("#website"); }
  if (firstBad) { firstBad.focus(); return; }
  if (!thesis && !openConfirmed) { $("#thesis-dialog").returnValue = ""; $("#thesis-dialog").showModal(); return; }  // recommend a thesis before an open screening
  const fd = new FormData();
  fd.append("deck", deckFile); fd.append("website", site); fd.append("hint", $("#hint").value.trim()); fd.append("thesis_id", thesis ? String(thesis.id) : "");
  resFiles.forEach((f) => fd.append("resources", f));
  const btn = $("#go");
  btn.disabled = true; btn.classList.add("is-loading"); btn.textContent = "Uploading...";
  try {
    const { job_id } = await api("/api/screen", { method: "POST", form: fd });
    sessionStorage.setItem("job", job_id);
    startProgress(job_id);
  } catch (err) { showFormError(err.message); resetGo(); }
}
function resetGo() { const b = $("#go"); b.classList.remove("is-loading"); b.textContent = "Start screening"; loadHealth(); }

/* ---------- progress ---------- */
function startProgress(jobId) {
  $(".screen-grid").hidden = true;
  $("#progress").hidden = false;
  $("#progress-title").textContent = $("#hint").value.trim() || hostOf($("#website").value.trim().replace(/^(?!https?:)/, "https://")) || "Analysing";
  poll(jobId);
}
async function poll(jobId) {
  let j;
  try { j = await api(`/api/jobs/${jobId}`); }
  catch (err) { endProgress(); if (!/Unknown job/.test(err.message)) showFormError(err.message); return; }
  $("#steps").innerHTML = Object.entries(j.step_labels).map(([k, label]) => {
    const s = j.steps[k] || { state: "pending", detail: "" };
    return `<li class="step step--${esc(s.state)}"><span class="step__icon" aria-hidden="true"></span>
      <span class="step__name">${esc(label)}<span class="visually-hidden"> - ${esc(s.state)}</span></span><span class="step__detail">${esc(s.detail)}</span></li>`;
  }).join("");
  const states = Object.keys(j.step_labels).map((k) => (j.steps[k] || {}).state), finished = states.filter((s) => s === "done" || s === "warning").length;
  $("#progress-bar").style.width = `${Math.max(4, Math.round(((finished + (states.includes("running") ? 0.5 : 0)) / states.length) * 100))}%`;
  $("#elapsed").textContent = mmss(j.elapsed);
  if ($("#progress-note").textContent !== (j.note || "")) $("#progress-note").textContent = j.note || "";
  if (j.status === "done") { endProgress(); toast("Screening complete"); if (!$("#view-screen").hidden) location.hash = `#/run/${j.run_id}`; return; }
  if (j.status === "error") { endProgress(); showFormError(j.error); return; }
  setTimeout(() => poll(jobId), 1000);
}
function endProgress() { sessionStorage.removeItem("job"); $("#progress").hidden = true; $(".screen-grid").hidden = false; resetGo(); }

/* ---------- motion helpers ---------- */
function countUp(el, to, ms = 900) {
  if (!el || matchMedia("(prefers-reduced-motion: reduce)").matches || !Number.isFinite(to)) return;
  const t0 = performance.now();
  const tick = (t) => { const p = Math.min((t - t0) / ms, 1); el.textContent = fmt(to * (1 - Math.pow(1 - p, 3))); if (p < 1) requestAnimationFrame(tick); else el.textContent = fmt(to); };
  requestAnimationFrame(tick);
}

/* ---------- report ---------- */
const SECTIONS = [["overview", "Overview"], ["flags", "Red flags"], ["claims", "Claim verification"], ["scorecard", "Scorecard"],
  ["context", "Scoring context"], ["mandate", "Mandate fit"], ["profile", "Company profile"], ["sources", "Sources"],
  ["quality", "Data quality"], ["questions", "Founder questions"]];
const list = (items, empty = "None") => (items && items.length ? `<ul class="list">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="faint">${empty}</p>`);
const section = (id, eyebrow, title, body, extra = "") =>
  `<section class="rsec" id="sec-${id}" aria-labelledby="h-${id}"><div class="rsec__head"><div><p class="eyebrow">${esc(eyebrow)}</p><h2 id="h-${id}">${esc(title)}</h2></div>${extra}</div>${body}</section>`;

function contributions(r) {
  const tw = Object.values(r.weights).reduce((a, w) => a + w.applied, 0) || 1;
  return Object.fromEntries(Object.entries(r.dimensions).map(([k, d]) => [k, (r.weights[k].applied * d.final) / tw]));
}

function flagsHtml(r) {
  const bad = r.claims.filter((c) => ["contradicted", "outdated"].includes(c.status));
  const checks = r.consistency.filter((c) => ["warn", "fail"].includes(c.state));
  if (!bad.length && !checks.length) return `<p class="empty-note">No contradictions or discrepancies were found in the evidence available. This is not proof of accuracy: see how many claims were verified above.</p>`;
  const rows = bad.map((c) => `<li class="flag">${badge(c.status)}<div><div class="flag__title">${esc(c.id)} · ${esc(c.text)}</div>
      <div class="flag__detail">${esc(c.note)}</div><div class="flag__links">${c.evidence.filter((e) => e.url).map((e) => `<a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener">${esc(e.id)}: ${esc(hostOf(e.url))}</a>`).join("")}</div></div></li>`)
    .concat(checks.map((c) => `<li class="flag"><span class="badge ${c.state === "fail" ? "tone-bad" : "tone-warn"}">${c.state === "fail" ? "Failed" : "Check"}</span>
      <div><div class="flag__title">${esc(c.check)}</div><div class="flag__detail">${esc(c.detail)}</div>
      <div class="flag__links">${(c.links || []).map((l) => `<a href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.title || hostOf(l.url))}</a>`).join("")}</div></div></li>`));
  return `<ul class="flags">${rows.join("")}</ul>`;
}

let claimFilter = "all";
function claimsHtml(r) {
  const counts = Object.fromEntries(Object.keys(STATUS).map((s) => [s, r.claims.filter((c) => c.status === s).length]));
  const chips = [["all", "All", r.claims.length], ...Object.keys(STATUS).filter((s) => counts[s]).map((s) => [s, STATUS[s].label, counts[s]])]
    .map(([k, l, n]) => `<button type="button" class="fchip" data-cf="${k}" aria-pressed="${claimFilter === k}">${esc(l)}<span class="num">${n}</span></button>`).join("");
  const rows = r.claims.filter((c) => claimFilter === "all" || c.status === claimFilter).map((c) => `
    <tr class="row"><td class="id">${esc(c.id)}</td>
      <td><button type="button" class="rowbtn" aria-expanded="false" aria-controls="cd-${esc(c.id)}"><span class="claimtext">${esc(c.text)}</span></button>
        ${c.quote_found ? "" : `<div class="warnline">⚠ Quote not found in source - extraction may be inaccurate</div>`}</td>
      <td class="hide-sm">${esc(c.source_label)}</td><td>${badge(c.status)}</td><td class="r num">${c.evidence.length}</td></tr>
    <tr class="detail" id="cd-${esc(c.id)}" hidden><td></td><td colspan="4">
      <h4>Assessment</h4><p style="margin:0">${esc(c.note) || "<span class='faint'>No note.</span>"}</p>
      <h4>Source quote</h4><p class="quote" style="margin:0">${c.quote ? esc(c.quote) : "<span class='faint'>No quote given.</span>"}</p>
      <p class="small faint" style="margin:4px 0 0">${esc(c.source_label)} · ${esc(pretty(c.category))} · ${esc(c.importance)} importance${c.url ? ` · <a href="${esc(safeUrl(c.url))}" target="_blank" rel="noopener">open page</a>` : ""}</p>
      <h4>Evidence</h4>${c.evidence.length ? `<ul class="evlist">${c.evidence.map((e) => `<li><span class="badge ${e.kind === "independent" ? "tone-ok" : "tone-info"}">${esc(KIND[e.kind] || e.kind)}</span>
        <div>${e.url ? `<a href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener">${esc(e.title || e.url)}</a>` : `<strong>${esc(e.title)}</strong>`}<div class="small muted">${esc(e.snippet)}</div></div></li>`).join("")}</ul>`
        : `<p class="faint" style="margin:0">No evidence cited.</p>`}</td></tr>`).join("");
  return `<div class="filters" role="group" aria-label="Filter claims by status">${chips}</div>
    <div class="tablewrap"><table class="dtable"><caption class="visually-hidden">Company claims with verification status</caption>
    <thead><tr><th scope="col">#</th><th scope="col">Claim</th><th scope="col" class="hide-sm">Source</th><th scope="col">Status</th><th scope="col" class="r">Evidence</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="5" class="faint">No claims in this filter.</td></tr>`}</tbody></table></div>`;
}

let dimSort = "contribution";
function scorecardHtml(r) {
  const contrib = contributions(r);
  const keys = Object.keys(r.dimensions);
  const ranked = [...keys].sort((a, b) => contrib[b] - contrib[a]);
  const color = Object.fromEntries(ranked.map((k, i) => [k, PALETTE[i % PALETTE.length]]));
  const total = Object.values(contrib).reduce((a, b) => a + b, 0) || 1;
  const comp = `<div class="composition"><p class="small muted" style="margin:0 0 6px">Score composition - how much each dimension adds to ${fmt(r.overall_raw)}/10 (before any cap or boost)</p>
    <div class="compbar" role="img" aria-label="${esc(ranked.map((k) => `${pretty(k)} ${fmt(contrib[k])}`).join(", "))}">${ranked.map((k) =>
      `<span style="width:${(contrib[k] / total) * 100}%;background:${color[k]}" title="${esc(pretty(k))}: ${fmt(contrib[k])}"></span>`).join("")}</div>
    <div class="complegend" aria-hidden="true">${ranked.slice(0, 6).map((k) => `<span><i style="background:${color[k]}"></i>${esc(pretty(k))} ${fmt(contrib[k])}</span>`).join("")}${ranked.length > 6 ? "<span>…</span>" : ""}</div></div>`;
  let order = keys;
  if (dimSort === "contribution") order = ranked;
  if (dimSort === "weak") order = [...keys].sort((a, b) => r.dimensions[a].final - r.dimensions[b].final);
  const maxC = Math.max(...Object.values(contrib), 0.01);
  const ticks = [...Array(10)].map((_, i) => `<span class="track__tick" style="left:${(i / 9) * 100}%"></span>`).join("");
  const pos = (s) => ((s - 1) / 9) * 100;
  const rows = order.map((k) => {
    const d = r.dimensions[k], w = r.weights[k];
    const change = w.base ? (w.applied / w.base - 1) * 100 : 0;
    return `<tr class="row"><td><button type="button" class="rowbtn" aria-expanded="false" aria-controls="dd-${k}"><strong class="claimtext">${esc(pretty(k))}</strong></button></td>
      <td class="r num">${fmt(d.final, 1)}${d.penalty ? `<div class="small" style="color:var(--danger-text)">raw ${d.raw}</div>` : ""}</td>
      <td class="hide-sm"><div class="track" aria-hidden="true">${ticks}${d.penalty ? `<span class="track__raw" style="left:${pos(d.raw)}%"></span>` : ""}<span class="track__dot" style="left:${pos(d.final)}%"></span></div></td>
      <td class="r num">${fmt(w.base)} → ${fmt(w.applied)}${Math.abs(change) >= 1 ? `<div class="wchange wchange--${change > 0 ? "up" : "down"}">${change > 0 ? "+" : ""}${Math.round(change)}%</div>` : ""}</td>
      <td class="hide-sm"><div class="contrib" title="${fmt(contrib[k])} points"><span style="width:${(contrib[k] / maxC) * 100}%"></span></div><div class="small num muted">${fmt(contrib[k])}</div></td>
      <td class="r num">${d.claim_ids.length}</td></tr>
    <tr class="detail" id="dd-${k}" hidden><td colspan="6">
      <h4>Reasoning</h4><p style="margin:0">${esc(d.reasoning) || "<span class='faint'>None given.</span>"}</p>
      ${d.penalty ? `<h4>Verification penalty</h4><p style="margin:0">−${fmt(d.penalty)} points because ${esc(d.penalty_claims.join(", "))} ${d.penalty_claims.length > 1 ? "were" : "was"} contradicted or outdated (raw ${d.raw} → ${fmt(d.final, 1)}).</p>` : ""}
      <h4>Weight</h4>${w.factors.length ? w.factors.map((f) => `<p class="factor"><strong>${esc(f.source)}</strong> ×${fmt(f.x)} - ${esc(f.why)}</p>`).join("") : `<p class="factor faint">Your thesis weight, unchanged for this company's stage and model.</p>`}
      <h4>Claims used</h4><p style="margin:0">${d.claim_ids.length ? d.claim_ids.map((id) => `<a href="#sec-claims" data-claim="${esc(id)}">${esc(id)}</a>`).join(", ") : "<span class='faint'>None</span>"}</p>
      <div class="cols3" style="margin-top:8px"><div><h4>Evidence</h4>${list(d.evidence)}</div><div><h4>Missing</h4>${list(d.missing)}</div><div></div></div></td></tr>`;
  }).join("");
  return comp + `<div class="tablewrap"><table class="dtable"><caption class="visually-hidden">Scorecard: score, weight and contribution per dimension</caption>
    <thead><tr><th scope="col">Dimension</th><th scope="col" class="r">Score</th><th scope="col" class="hide-sm">1 – 10</th><th scope="col" class="r">Weight base → applied</th>
    <th scope="col" class="hide-sm">Contribution</th><th scope="col" class="r">Claims</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function sourcesHtml(r) {
  const cw = r.crawl, s = r.search;
  const groups = ["independent", "profile", "company"].map((k) => [k, s.hits.filter((h) => h.kind === k)]).filter(([, h]) => h.length);
  const skipped = Object.entries(cw.skipped || {}).map(([k, v]) => `${v} ${k}`).join(" · ");
  return `<div class="ctxgrid">
    <div class="panel" style="margin:0"><h3 class="h3">Website</h3>
      <dl class="kv" style="margin-top:12px"><dt>Start</dt><dd><a href="${esc(safeUrl(cw.start_url))}" target="_blank" rel="noopener">${esc(cw.start_url)}</a></dd>
        ${cw.final_url && cw.final_url !== cw.start_url ? `<dt>Redirected to</dt><dd>${esc(cw.final_url)}</dd>` : ""}
        <dt>Pages read</dt><dd class="num">${cw.pages_crawled}</dd><dt>Analysed</dt><dd class="num">${cw.pages_analysed.length}</dd>
        <dt>Failed</dt><dd class="num">${cw.errors.length}</dd><dt>Stopped</dt><dd>${esc(cw.stopped_reason)}</dd>
        ${skipped ? `<dt>Skipped</dt><dd class="small">${esc(skipped)}</dd>` : ""}</dl>
      ${cw.pages_analysed.length ? `<h4 class="eyebrow" style="margin-top:16px">Pages analysed</h4><ul class="list small">${cw.pages_analysed.map((p) => `<li><a href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener">${esc(new URL(p.url).pathname)}</a> <span class="faint">${esc(p.title)}</span></li>`).join("")}</ul>` : ""}
      ${cw.errors.length ? `<details style="margin-top:8px"><summary class="small">Failed pages (${cw.errors.length})</summary><ul class="list small">${cw.errors.map((e) => `<li>${esc(e.url)} - ${esc(e.error)}</li>`).join("")}</ul></details>` : ""}
    </div>
    <div class="panel" style="margin:0"><h3 class="h3">Web search</h3>
      <dl class="kv" style="margin-top:12px"><dt>Provider</dt><dd>${esc(s.provider || "–")}</dd><dt>Status</dt><dd>${s.available ? "Available" : "<strong style='color:var(--danger-text)'>Unavailable</strong>"}</dd>
        <dt>Relevant</dt><dd class="num">${s.hits.length}</dd><dt>Off-topic dropped</dt><dd class="num">${s.irrelevant}</dd></dl>
      <details style="margin-top:12px"><summary class="small">Queries (${s.queries.length})</summary><ul class="list small">${s.queries.map((q) => `<li><code>${esc(q)}</code></li>`).join("")}${s.failed.map((f) => `<li class="warnline">Failed: ${esc(f.query)} - ${esc(f.error)}</li>`).join("")}</ul></details>
    </div></div>
    ${groups.map(([k, hits]) => `<h3 class="eyebrow" style="margin:20px 0 8px">${esc(KIND[k])} sources (${hits.length})</h3>
      <div class="tablewrap"><table class="dtable"><tbody>${hits.map((h) => `<tr><td class="id">${esc(h.id)}</td><td><a href="${esc(safeUrl(h.url))}" target="_blank" rel="noopener">${esc(h.title || h.url)}</a>
        <div class="small muted">${esc(h.snippet)}</div></td><td class="hide-sm small faint">${esc(hostOf(h.url))}</td></tr>`).join("")}</tbody></table></div>`).join("")}`;
}

function legacyHtml(r) {
  return `<p class="crumbs"><a href="#/history">History</a> / ${esc(r.company)}</p>
    <div class="panel"><span class="badge badge--legacy">Earlier version</span><h1 style="margin:12px 0 8px">${esc(r.company)}</h1>
    <p><span class="badge badge--${esc(r.decision)}">${esc(r.decision)}</span> <span class="num">${fmt(r.overall)}/10</span></p>
    <p class="muted">This screening was made before claim verification, website crawling and adaptive scoring existed. Run it again for a full report.</p>
    <div class="ractions"><a class="btn btn--primary btn--sm" href="#/screen">New screening</a><button type="button" class="btn btn--danger btn--sm" id="delete-run">Delete</button></div></div>`;
}

async function loadRun(id) {
  const view = $("#view-run");
  view.innerHTML = `<p class="muted">Loading report...</p>`;
  let r;
  try { r = await api(`/api/runs/${id}`); }
  catch (err) { view.innerHTML = `<div class="panel empty"><h1>Report not found</h1><p class="muted">${esc(err.message)}</p><a class="btn btn--dark" href="#/history">Back to history</a></div>`; return; }
  if (r.version !== 2) { view.innerHTML = legacyHtml(r); bindDelete(r); return; }
  claimFilter = "all"; dimSort = "contribution";
  const c = r.claim_counts, checked = r.claims.filter((x) => x.status !== "not_checked").length, cf = r.confidence, ctx = r.context, p = r.profile;
  const head = `<p class="crumbs"><a href="#/history">History</a> / ${esc(r.company)}</p>
    <div class="rhead"><div><p class="eyebrow">Screening report · ${esc(r.fund)}</p><h1 id="run-title">${esc(r.company)}</h1>
      <div class="rhead__meta"><a href="${esc(safeUrl(r.website))}" target="_blank" rel="noopener">${esc(hostOf(r.website))}</a>
        ${ctx.stage ? `<span class="chip">${esc(pretty(ctx.stage))}</span>` : `<span class="chip">Stage not stated</span>`}<span class="chip">${esc(ctx.model_label)}</span>
        ${p.hq_country ? `<span class="chip">${esc(p.hq_country)}</span>` : ""}</div></div>
      <div class="verdict"><div class="verdict__tag verdict__tag--${esc(r.decision)}">${esc(r.decision)}</div>
        <div class="verdict__score"><span class="verdict__big">${fmt(r.overall)}</span><span class="verdict__of"> /10</span>
          <div class="verdict__sub">${esc(cf.level)} confidence · mandate ${esc(r.fit.status.replace("_", " ").toLowerCase())}</div></div></div></div>
    <div class="ractions"><a class="btn btn--primary btn--sm" href="/api/runs/${r.id}/memo.pdf" download>Download PDF memo</a>
      <button type="button" class="btn btn--ghost btn--sm" id="copy-memo">Copy as Markdown</button><button type="button" class="btn btn--ghost btn--sm" onclick="window.print()">Print</button>
      <a class="btn btn--ghost btn--sm" href="#/screen">New screening</a><button type="button" class="btn btn--danger btn--sm" id="delete-run">Delete</button></div>
    <div class="kpis">
      <div class="kpi"><div class="kpi__label">Overall</div><div class="kpi__value">${fmt(r.overall)}</div><div class="kpi__note">${r.adjustment ? "adjusted" : `raw ${fmt(r.overall_raw)}`}</div></div>
      <div class="kpi"><div class="kpi__label">Confidence</div><div class="kpi__value">${esc(cf.level)}</div><div class="kpi__note">${r.evidence_coverage}/11 dims evidenced</div></div>
      <div class="kpi"><div class="kpi__label">Verified</div><div class="kpi__value">${c.verified}<span class="faint">/${checked}</span></div><div class="kpi__note">independent source</div></div>
      <div class="kpi"><div class="kpi__label">Contradicted</div><div class="kpi__value" style="${c.contradicted + c.outdated ? "color:var(--danger-text)" : ""}">${c.contradicted + c.outdated}</div><div class="kpi__note">incl. outdated</div></div>
      <div class="kpi"><div class="kpi__label">Red flags</div><div class="kpi__value">${r.red_flag_count}</div><div class="kpi__note">claims + checks</div></div>
      <div class="kpi"><div class="kpi__label">Sources</div><div class="kpi__value">${r.crawl.pages_crawled}<span class="faint"> · </span>${r.search.hits.length}</div><div class="kpi__note">pages · web results</div></div></div>`;

  const b = r.brief;
  const body = [
    section("overview", "Summary", "Overview", `<div class="reason">${esc(r.decision_reason)}${r.adjustment ? ` <span class="muted">(${esc(r.adjustment)})</span>` : ""}</div>
      <p class="lede">${esc(b.summary) || "<span class='faint'>No summary.</span>"}</p>
      <div class="cols3"><div class="col col--plus"><h3>Strengths</h3>${list(b.strengths)}</div><div class="col col--minus"><h3>Concerns</h3>${list(b.concerns)}</div><div class="col"><h3>Next steps</h3>${list(b.next_steps)}</div></div>`),
    section("flags", "Diligence", "Red flags & discrepancies", flagsHtml(r)),
    section("claims", "Evidence", "Claim verification", `<div id="claims-body">${claimsHtml(r)}</div>`,
      `<span class="small muted">"Verified" requires an independent source. Click a claim for evidence.</span>`),
    section("scorecard", "Assessment", "Scorecard", `<div id="score-body">${scorecardHtml(r)}</div>`,
      `<label class="small">Sort <select id="dim-sort" style="width:auto;min-height:32px;padding:2px 8px"><option value="contribution">By contribution</option><option value="weak">Weakest first</option><option value="default">Default order</option></select></label>`),
    section("context", "Method", "Scoring context", `<div class="ctxgrid">
      <div class="panel" style="margin:0"><h3 class="h3">How this company was scored</h3><dl class="kv" style="margin-top:12px">
        <dt>Stage</dt><dd>${esc(ctx.stage || "not stated")}${ctx.stage_group ? ` <span class="faint">(${esc(ctx.stage_group)} group)</span>` : " <span class='faint'>(no stage weighting)</span>"}</dd>
        <dt>Business model</dt><dd>${esc(ctx.model_label)}</dd><dt>Confidence</dt><dd>${esc(cf.level)} - ${esc(cf.reasons.join("; "))}</dd>
        ${r.adjustment ? `<dt>Adjustment</dt><dd>${esc(r.adjustment)}</dd>` : ""}</dl>
        <p class="small muted" style="margin:12px 0 0">Base weights come from your fund thesis; stage and business-model multipliers and verification penalties come from <code>scoring_profiles.yaml</code>. Open a scorecard row to see each factor and its reason.</p></div>
      <details class="panel whatif" id="whatif" style="margin:0"><summary>What if? Re-weight</summary>
        <p class="small muted">Change the base weights or thresholds of the fund thesis this report used. Stage/model adjustments and verification penalties still apply. No AI call; nothing is saved unless you choose to.</p>
        <div class="whatif__out" role="status" aria-live="polite" id="whatif-out"></div>
        <div class="thresholds"><div><label class="label" for="wi-adv">Advance at</label><input type="number" id="wi-adv" min="0" max="10" step="0.1"></div>
          <div><label class="label" for="wi-may">Maybe at</label><input type="number" id="wi-may" min="0" max="10" step="0.1"></div></div>
        <div class="whatif__grid" id="wi-sliders"></div>
        <div class="actions" style="margin-top:16px"><button type="button" class="btn btn--ghost btn--sm" id="wi-reset">Reset</button><button type="button" class="btn btn--dark btn--sm" id="wi-save">Save to fund thesis</button></div></details></div>`),
    section("mandate", "Fund thesis", "Mandate fit", mandateHtml(r), `<span class="badge badge--${esc(r.fit.status)}">${esc(r.fit.status.replace("_", " "))}</span>`),
    section("profile", "As stated by the company", "Company profile", (() => {
      const src = (k) => (r.profile_sources[k] ? `<span class="srctag">${esc(r.profile_sources[k])}</span>` : "");
      const row = (label, k, v) => (v ? `<dt>${label}</dt><dd>${v}${src(k)}</dd>` : "");
      const founders = (p.founders || []).map((f) => `<strong>${esc(f.name)}</strong>${f.role ? `, ${esc(f.role)}` : ""}${f.background ? ` <span class="muted">- ${esc(f.background)}</span>` : ""}`).join("<br>");
      return `<p class="small muted">Not independently verified unless the matching claim above says so.</p><dl class="kv">
        ${row("One-liner", "one_liner", esc(p.one_liner))}${row("Founders", "founders", founders)}${row("HQ", "hq_country", esc(p.hq_country))}
        ${row("Founder origin", "founder_origin", esc(p.founder_origin))}${row("Founded", "founded_year", esc(p.founded_year))}${row("Team size", "team_size", esc(p.team_size))}
        ${row("Stage", "stage", esc(p.stage))}${row("Sectors", "sectors", esc((p.sectors || []).join(", ")))}${row("Business model", "business_model", esc(p.business_model))}
        ${row("Market", "market", esc(p.market))}${row("Traction", "traction", (p.traction || []).map(esc).join("<br>"))}${row("Customers", "customers", esc(p.customers))}
        ${row("Raising", "ask_text", esc(p.ask_text || (p.ask_eur ? `EUR ${Number(p.ask_eur).toLocaleString()}` : "")))}</dl>`;
    })()),
    section("sources", "Provenance", "Sources", sourcesHtml(r)),
    section("quality", "Transparency", "Data quality & workflow issues", r.issues.length
      ? `<ul class="flags">${r.issues.map((i) => `<li class="flag"><span class="badge ${SEVERITY[i.severity] || "tone-info"}">${esc(pretty(i.severity))}</span><div><div class="flag__title">${esc(pretty(i.stage))}</div><div class="flag__detail">${esc(i.message)}</div></div></li>`).join("")}</ul>`
      : `<p class="empty-note">No technical or workflow issues during this run.</p>`),
    section("questions", "Next meeting", "Founder questions", `<div class="ctxgrid"><div>${r.questions_for_founder.length
      ? `<ul class="qs">${r.questions_for_founder.map((q, i) => `<li><input type="checkbox" id="q${i}"><label for="q${i}">${esc(q)}</label></li>`).join("")}</ul>
         <button type="button" class="btn btn--ghost btn--sm" id="copy-qs" style="margin-top:8px">Copy questions</button>` : "<p class='faint'>None.</p>"}</div>
      <div><h3 class="eyebrow">Missing data</h3>${list(r.missing_critical_data)}</div></div>`),
  ].join("");

  view.innerHTML = `<div class="report"><nav class="toc" aria-label="Report sections"><ol>${SECTIONS.map(([id, label], i) =>
    `<li><a href="#sec-${id}" data-toc="${id}"><span>${esc(label)}</span><span class="num">${String(i + 1).padStart(2, "0")}</span></a></li>`).join("")}</ol></nav>
    <div>${head}<div class="rbody">${body}</div>
    <p class="foot" style="margin-top:40px">Model ${esc(r.model)} · ${r.llm_calls} AI calls${(r.llm?.switches || []).length ? ` · switched provider ${r.llm.switches.length}× (${esc([...new Set(r.llm.switches.map((s) => `${s.from}: ${s.reason}`))].join("; "))})` : ""} · ${fmt(r.seconds, 0)} s · inputs: ${esc(r.inputs.deck)}${r.inputs.resources.length ? ` + ${esc(r.inputs.resources.join(", "))}` : ""} · ${esc(r.inputs.website)}.
      AI-assisted screening: claims marked anything other than "Verified" have not been independently confirmed.</p></div></div>`;
  bindReport(r);
}

const STATE_BADGE = { ok: ["tone-ok", "Meets"], warn: ["tone-warn", "Check"], unknown: ["tone-warn", "Unknown"], fail: ["tone-bad", "Does not meet"] };
function mandateHtml(r) {
  const th = r.thesis, detailed = r.fit.checks.every((k) => "rule" in k);
  const head = `<p class="small muted" style="margin:0 0 10px">Compared against <strong>${esc(r.fund)}</strong>${th ? "" : " (an earlier report: the thesis copy was not saved)"}. `
    + `A clear mismatch on any criterion is a PASS whatever the score. Values that could not be read are shown as Unknown, never as a mismatch.</p>`;
  const result = (k) => `<span class="badge ${STATE_BADGE[k.state][0]}">${STATE_BADGE[k.state][1]}</span><div class="small muted" style="margin-top:4px">${esc(k.detail)}</div>`;
  if (!detailed) return head + `<div class="tablewrap"><table class="dtable"><tbody>${r.fit.checks.map((k) => `<tr><td style="width:140px"><strong>${esc(k.name)}</strong></td><td>${result(k)}</td></tr>`).join("")}</tbody></table></div>`;
  return head + `<div class="tablewrap"><table class="dtable"><caption class="visually-hidden">Company compared with the fund thesis</caption>
    <thead><tr><th scope="col">Criterion</th><th scope="col">Company</th><th scope="col">Fund requires</th><th scope="col">Result</th></tr></thead>
    <tbody>${r.fit.checks.map((k) => `<tr><td><strong>${esc(k.name)}</strong></td><td>${esc(k.value)}${k.source ? `<span class="srctag">from ${esc(k.source)}</span>` : ""}</td>
      <td class="muted">${esc(k.rule)}</td><td>${result(k)}</td></tr>`).join("")}</tbody></table></div>`;
}

function bindDelete(r) {
  $("#delete-run")?.addEventListener("click", async () => {
    if (!confirm(`Delete the screening of ${r.company}? This can't be undone.`)) return;
    try { await api(`/api/runs/${r.id}`, { method: "DELETE" }); toast("Deleted"); location.hash = "#/history"; } catch (err) { toast(err.message); }
  });
}

function toggleRow(btn) {
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", String(!open));
  $(`#${CSS.escape(btn.getAttribute("aria-controls"))}`).hidden = open;
}

let currentRun = null;
// One delegated listener for the report view, registered once (re-adding it per report would make clicks fire twice).
$("#view-run").addEventListener("click", (e) => {
  const r = currentRun; if (!r) return;
  const rb = e.target.closest(".rowbtn"); if (rb) return toggleRow(rb);
  const cf = e.target.closest("[data-cf]");
  if (cf) { claimFilter = cf.dataset.cf; $("#claims-body").innerHTML = claimsHtml(r); return; }
  const link = e.target.closest("[data-claim]");
  if (link) {
    e.preventDefault(); claimFilter = "all"; $("#claims-body").innerHTML = claimsHtml(r);
    const b = $(`[aria-controls="cd-${CSS.escape(link.dataset.claim)}"]`);
    if (b) { if (b.getAttribute("aria-expanded") !== "true") toggleRow(b); b.scrollIntoView({ block: "center" }); b.focus({ preventScroll: true }); }
    return;
  }
  const toc = e.target.closest("[data-toc]");
  if (toc) { e.preventDefault(); $(`#sec-${toc.dataset.toc}`).scrollIntoView({ behavior: "smooth", block: "start" }); }
});

function bindReport(r) {
  currentRun = r;
  bindDelete(r);
  countUp($(".verdict__big"), r.overall);
  $("#dim-sort").addEventListener("change", (e) => { dimSort = e.target.value; $("#score-body").innerHTML = scorecardHtml(r); });
  $("#copy-memo").addEventListener("click", async () => { try { copy(await (await fetch(`/api/runs/${r.id}/memo`)).text(), "Memo"); } catch { toast("Could not load the memo"); } });
  $("#copy-qs")?.addEventListener("click", () => copy(r.questions_for_founder.map((q) => `- ${q}`).join("\n"), "Questions"));
  $$(".qs input").forEach((cb) => cb.addEventListener("change", () => cb.parentElement.classList.toggle("is-done", cb.checked)));
  if ("IntersectionObserver" in window) {
    const obs = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) $$("[data-toc]").forEach((a) => a.classList.toggle("is-active", `sec-${a.dataset.toc}` === en.target.id));
    }), { rootMargin: "-90px 0px -65% 0px" });
    $$(".rsec").forEach((s) => obs.observe(s));
  }
  setupWhatIf(r);
}

async function setupWhatIf(r) {
  // Start from the thesis this report was scored against (its saved copy), not whichever thesis is current.
  const base = r.thesis?.data;
  const thesis = {
    weights: base?.weights || Object.fromEntries(Object.entries(r.weights).map(([k, w]) => [k, w.base])),
    advance_at: r.thresholds?.advance_at ?? base?.advance_at ?? 7, maybe_at: r.thresholds?.maybe_at ?? base?.maybe_at ?? 5.5,
  };
  const box = $("#wi-sliders"), adv = $("#wi-adv"), may = $("#wi-may"), out = $("#whatif-out");
  const render = (w, a, m) => {
    adv.value = a; may.value = m;
    box.innerHTML = Object.keys(r.dimensions).map((k) => `<div class="slider"><label for="w-${k}"><span>${esc(pretty(k))}</span><span class="faint">score ${fmt(r.dimensions[k].final, 1)}</span></label>
      <input type="range" id="w-${k}" data-k="${k}" min="0" max="3" step="0.1" value="${w[k] ?? 1}"><output id="wo-${k}" for="w-${k}">${Number(w[k] ?? 1).toFixed(1)}</output></div>`).join("");
  };
  const show = (v) => {
    const d = Math.round((v.overall - r.overall) * 100) / 100;
    out.innerHTML = `<span><strong class="num" style="font-size:20px">${fmt(v.overall)}</strong><span class="faint"> /10</span></span>
      <span class="num ${d > 0 ? "delta--up" : d < 0 ? "delta--down" : ""}">${d > 0 ? "▲ +" : d < 0 ? "▼ " : "± "}${Math.abs(d) ? fmt(d) : 0} vs report</span>
      <span class="badge badge--${esc(v.decision)}">${esc(v.decision)}</span><span class="small muted" style="flex-basis:100%">${esc(v.decision_reason)}</span>`;
  };
  const current = () => ({ weights: Object.fromEntries($$("input[type=range]", box).map((s) => [s.dataset.k, Number(s.value)])), advance_at: Number(adv.value), maybe_at: Number(may.value) });
  const recompute = debounce(async () => {
    try { show(await api(`/api/runs/${r.id}/rescore`, { method: "POST", body: current() })); }
    catch (err) { out.innerHTML = `<span style="color:var(--danger-text)">${esc(err.message)}</span>`; }
  }, 180);
  render(thesis.weights, thesis.advance_at, thesis.maybe_at);
  show({ overall: r.overall, decision: r.decision, decision_reason: "As in the report." });
  $("#whatif").addEventListener("input", (e) => {
    if (e.target.type === "range") $(`#wo-${e.target.dataset.k}`).textContent = Number(e.target.value).toFixed(1);
    if (e.target.matches("input")) recompute();
  });
  $("#wi-reset").addEventListener("click", () => { render(thesis.weights, thesis.advance_at, thesis.maybe_at); recompute(); });
  const save = $("#wi-save");
  if (!r.thesis_exists) { save.disabled = true; save.textContent = r.thesis.id ? "Fund thesis was deleted" : "Open screening: no thesis to save to"; }
  else save.textContent = `Save to "${r.thesis.name}"`;
  save.addEventListener("click", async () => {
    try {
      const live = await api(`/api/theses/${r.thesis.id}`);                       // merge into the thesis as it is now, so other edits aren't lost
      await api(`/api/theses/${r.thesis.id}`, { method: "PUT", body: { data: { ...live.data, ...current() } } });
      toast(`Saved to "${r.thesis.name}" - new screenings use these weights`);
    } catch (err) { toast(err.message); }
  });
}

/* ---------- history ---------- */
let historyRows = [], historyFilter = "ALL", historyFund = "", historySort = { key: "id", dir: -1 };
async function loadHistory() {
  try { historyRows = await api("/api/runs"); } catch (err) { $("#history-list").innerHTML = `<p class="form-error">${esc(err.message)}</p>`; return; }
  const n = (d) => historyRows.filter((r) => r.decision === d).length;
  const scored = historyRows.filter((r) => typeof r.overall === "number");
  const avg = scored.length ? scored.reduce((a, r) => a + r.overall, 0) / scored.length : null;
  $("#history-stats").innerHTML = [["Screened", historyRows.length], ["Advance", n("ADVANCE")], ["Maybe", n("MAYBE")], ["Pass", n("PASS")], ["Average score", avg === null ? "–" : fmt(avg, 1)]]
    .map(([l, v]) => `<div class="kpi"><div class="kpi__label">${l}</div><div class="kpi__value">${v}</div></div>`).join("");
  const funds = [...new Set(historyRows.map((r) => r.thesis_name).filter(Boolean))].sort();
  const fsel = $("#fund-filter");
  fsel.hidden = funds.length < 2;                                   // only useful with more than one fund
  fsel.innerHTML = `<option value="">All funds</option>` + funds.map((f) => `<option value="${esc(f)}"${f === historyFund ? " selected" : ""}>${esc(f)}</option>`).join("");
  if (!funds.includes(historyFund)) historyFund = "";
  renderHistory();
}
function renderHistory() {
  const q = $("#search").value.trim().toLowerCase();
  let rows = historyRows.filter((r) => (historyFilter === "ALL" || r.decision === historyFilter) && (!historyFund || r.thesis_name === historyFund) &&
    (!q || `${r.company} ${r.website || ""}`.toLowerCase().includes(q)));
  const { key, dir } = historySort;
  rows = [...rows].sort((a, b) => { const x = a[key] ?? "", y = b[key] ?? ""; return (x > y ? 1 : x < y ? -1 : 0) * dir; });
  const el = $("#history-list");
  if (!historyRows.length) {
    el.innerHTML = `<div class="panel empty"><h2>No screenings yet</h2><p class="muted">Upload a pitch deck and the company website to create the first report.</p><a class="btn btn--primary" href="#/screen">New screening</a></div>`;
    return;
  }
  const th = (k, label, cls = "") => `<th scope="col" class="${cls}" aria-sort="${key === k ? (dir > 0 ? "ascending" : "descending") : "none"}"><button type="button" class="sortbtn" data-sort="${k}">${label}</button></th>`;
  el.innerHTML = `<div class="tablewrap"><table class="dtable"><caption class="visually-hidden">Screened companies</caption>
    <thead><tr>${th("company", "Company")}${th("thesis_name", "Fund thesis", "hide-sm")}${th("stage", "Stage", "hide-sm")}${th("decision", "Decision")}${th("overall", "Score", "r")}${th("confidence", "Confidence", "hide-sm")}
      ${th("verified", "Verified", "r hide-sm")}${th("red_flags", "Flags", "r hide-sm")}${th("fit", "Mandate", "hide-sm")}${th("id", "Date", "hide-sm")}</tr></thead>
    <tbody>${rows.map((r) => `<tr class="row" data-id="${r.id}">
      <td><a class="co" href="#/run/${r.id}">${esc(r.company || "Unknown")}</a><span class="co-sub">${esc(hostOf(r.website) || (r.version === 2 ? "" : "earlier version"))}${r.model ? ` · ${esc(r.model)}` : ""}</span></td>
      <td class="hide-sm">${esc(r.thesis_name || "–")}</td>
      <td class="hide-sm">${esc(r.stage ? pretty(r.stage) : "–")}</td>
      <td>${r.version === 2 ? "" : `<span class="badge badge--legacy">Legacy</span> `}<span class="badge badge--${esc(r.decision)}">${esc(r.decision)}</span></td>
      <td class="r num">${fmt(r.overall)}<span class="minibar" aria-hidden="true"><span style="width:${(Number(r.overall) || 0) * 10}%"></span></span></td>
      <td class="hide-sm">${r.confidence ? `<span class="badge badge--${esc(r.confidence)}">${esc(r.confidence)}</span>` : "–"}</td>
      <td class="r num hide-sm">${r.version === 2 ? `${r.verified ?? 0}/${r.claims ?? 0}` : "–"}</td>
      <td class="r num hide-sm">${r.version === 2 ? r.red_flags ?? 0 : "–"}</td>
      <td class="hide-sm"><span class="badge badge--${esc(r.fit)}">${esc((r.fit || "").replace("_", " "))}</span></td>
      <td class="hide-sm small muted">${esc(new Date(r.created).toLocaleDateString([], { dateStyle: "medium" }))}</td></tr>`).join("")
      || `<tr><td colspan="10" class="faint">No companies match.</td></tr>`}</tbody></table></div>`;
}
$("#search").addEventListener("input", renderHistory);
$("#fund-filter").addEventListener("change", (e) => { historyFund = e.target.value; renderHistory(); });
$$("[data-filter]").forEach((c) => c.addEventListener("click", () => {
  historyFilter = c.dataset.filter;
  $$("[data-filter]").forEach((x) => { x.classList.toggle("is-on", x === c); x.setAttribute("aria-pressed", String(x === c)); });
  renderHistory();
}));
$("#history-list").addEventListener("click", (e) => {
  const s = e.target.closest("[data-sort]");
  if (s) { const k = s.dataset.sort; historySort = { key: k, dir: historySort.key === k ? -historySort.dir : (k === "company" ? 1 : -1) }; return renderHistory(); }
  const tr = e.target.closest("tr[data-id]");
  if (tr && !e.target.closest("a")) location.hash = `#/run/${tr.dataset.id}`;
});

/* ---------- start ---------- */
// DOMContentLoaded fires after thesis.js has loaded too, so routing can call loadThesis().
window.addEventListener("DOMContentLoaded", () => {
  loadHealth();
  route();
  const pending = sessionStorage.getItem("job");
  if (pending) { location.hash = "#/screen"; startProgress(pending); }
});


/* ---------- light / dark theme ---------- */
(function () {
  const root = document.documentElement, btn = $("#theme-btn");
  const current = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const paint = () => { const dark = current() === "dark"; btn.textContent = dark ? "☀" : "☾"; btn.setAttribute("aria-pressed", String(dark)); };
  btn.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode: the choice just lasts this visit */ }
    paint();
  });
  paint();
})();
