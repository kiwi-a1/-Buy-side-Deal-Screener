"use strict";
/* Fund theses: a list (create / edit / duplicate / delete) and an editor with tag inputs, live suggestions, region shortcuts,
   presets and templates. Uses helpers from app.js ($, $$, esc, api, toast, pretty, fmt). */

let thesisData = null, thesisId = null, thesisDims = {}, sug = null, thesisDirty = false, thesisYaml = "";

const TAG_FIELDS = [
  { key: "geography", label: "Countries", kind: "country", others: [], help: "A deal fits if HQ is here, or the founders come from / studied / worked here. Leave empty for no restriction." },
  { key: "stages_core", label: "Core stages", kind: "stage", others: ["stages_check"], help: "Clear fit." },
  { key: "stages_check", label: "Stages to double-check", kind: "stage", others: ["stages_core"], help: "Allowed, but flagged for a second look." },
  { key: "sectors_strong", label: "Target sectors", kind: "sector", others: ["sectors_weak", "sectors_excluded"], help: "Whole-word match: \"ai\" matches \"vertical ai\", not \"retail\". Leave empty for no sector focus." },
  { key: "sectors_weak", label: "Risky sectors", kind: "weak", others: ["sectors_strong", "sectors_excluded"], help: "Flagged as risky, not rejected." },
  { key: "sectors_excluded", label: "Excluded sectors", kind: "excluded", others: ["sectors_strong", "sectors_weak"], help: "Never invest: a match is a clear mismatch (PASS)." },
];
const FIELD = Object.fromEntries(TAG_FIELDS.map((f) => [f.key, f]));
const fmtEur = (n) => (n >= 1e6 ? `€${+(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `€${Math.round(n / 1e3)}k` : "€0");
const stageLabel = (v) => sug?.stages.find((s) => s.value === v)?.label || v;
const KINDLESS = new Set(["sector", "weak", "excluded"]);
const when = (iso) => { try { return new Date(iso).toLocaleDateString([], { dateStyle: "medium" }); } catch { return ""; } };

/* ---------- routing entry point (called from app.js) ---------- */
async function loadThesis(arg) {
  const list = $("#thesis-list"), form = $("#thesis-form");
  if (!sug) {
    try { const o = await api("/api/thesis-options"); sug = o.suggestions; thesisDims = o.dimensions; }
    catch (err) { list.hidden = false; form.hidden = true; list.innerHTML = `<p class="form-error">${esc(err.message)}</p>`; return; }
  }
  const editing = arg !== undefined && arg !== "";
  list.hidden = editing; form.hidden = !editing; $("#thesis-new-btn").hidden = editing;
  if (!editing) { setHead("Fund theses", "Each fund thesis is a mandate: what you invest in and how much each factor counts. Choose one for every screening."); return renderThesisList(); }
  if (arg === "new") {
    thesisId = null; thesisData = structuredClone(sug.blank); thesisYaml = ""; setDirty(false);
    setHead("New fund thesis", "Start from a template or fill it in yourself. Nothing is saved until you click Save.");
    return renderEditor();
  }
  try {
    const t = await api(`/api/theses/${Number(arg)}`);
    thesisId = t.id; thesisData = t.data; thesisYaml = t.text; setDirty(false);
    setHead(t.name, `Used in ${t.runs} screening${t.runs === 1 ? "" : "s"}. Changes apply to new screenings; past reports keep the thesis they were scored against.`);
    renderEditor(t.warnings);
  } catch (err) { form.innerHTML = `<p class="form-error">${esc(err.message)}</p><a class="btn btn--dark" href="#/thesis">Back to fund theses</a>`; setHead("Fund thesis", ""); }
}

function setHead(title, sub) { $("#thesis-title").textContent = title; $("#thesis-sub").textContent = sub; document.title = `${title} · Deal Screening`; }
function setDirty(on) { thesisDirty = on; const el = $("#dirty"); if (el) el.textContent = on ? "Unsaved changes" : ""; }

/* ---------- list ---------- */
async function renderThesisList() {
  const el = $("#thesis-list");
  let rows;
  try { rows = await api("/api/theses"); } catch (err) { el.innerHTML = `<p class="form-error">${esc(err.message)}</p>`; return; }
  if (!rows.length) {
    el.innerHTML = `<div class="panel gate"><h2 class="h2">No fund theses yet</h2><p class="muted">Create one to start screening. Pick a template (CEE seed, US seed, European Series A, global growth) or build your own.</p>
      <div class="presets">${sug.templates.map((t, i) => `<button type="button" class="btn btn--ghost btn--sm" data-newfrom="${i}">${esc(t.label)}</button>`).join("")}<a class="btn btn--primary btn--sm" href="#/thesis/new">Start blank</a></div></div>`;
    return;
  }
  el.innerHTML = `<div class="thesis-grid">${rows.map((t) => {
    const s = t.summary, bad = t.errors && t.errors.length;
    return `<article class="thesis-card" aria-labelledby="tc-${t.id}">
      <header><h2 class="h3" id="tc-${t.id}">${esc(t.name)}</h2><span class="small faint">${t.runs} screening${t.runs === 1 ? "" : "s"} · updated ${esc(when(t.updated))}</span></header>
      ${s ? `<dl class="kv small"><dt>Countries</dt><dd>${s.countries ? s.countries : "any"}</dd><dt>Stages</dt><dd>${esc(s.stages.map(stageLabel).join(", ") || "-")}</dd>
        <dt>Sectors</dt><dd>${s.sectors ? s.sectors + " target" : "any"}</dd><dt>Round</dt><dd>${s.ticket[0] || s.ticket[1] ? `${fmtEur(s.ticket[0])} - ${fmtEur(s.ticket[1])}` : "no limits"}</dd>
        <dt>Decides at</dt><dd>advance ${fmt(s.advance_at)} · maybe ${fmt(s.maybe_at)}</dd></dl>` : ""}
      ${bad ? `<p class="field-note field-note--warn">Needs fixing before use: ${esc(t.errors[0])}</p>` : ""}
      <footer><a class="btn btn--dark btn--sm" href="#/thesis/${t.id}">Edit</a>
        ${bad ? "" : `<button type="button" class="btn btn--primary btn--sm" data-use="${t.id}">Screen a company</button>`}
        <button type="button" class="btn btn--ghost btn--sm" data-dup="${t.id}">Duplicate</button>
        <button type="button" class="btn btn--danger btn--sm" data-del="${t.id}" data-name="${esc(t.name)}" data-runs="${t.runs}">Delete</button></footer></article>`;
  }).join("")}</div>`;
}

$("#thesis-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.use) { localStorage.setItem("thesisId", b.dataset.use); location.hash = "#/screen"; }
    else if (b.dataset.dup) { const t = await api(`/api/theses/${b.dataset.dup}/duplicate`, { method: "POST" }); toast(`Created "${t.name}"`); renderThesisList(); refreshTheses(); }
    else if (b.dataset.newfrom !== undefined) { sessionStorage.setItem("thesisTemplate", b.dataset.newfrom); location.hash = "#/thesis/new"; }
    else if (b.dataset.del) {
      const n = Number(b.dataset.runs);
      if (!confirm(`Delete the fund thesis "${b.dataset.name}"?${n === 1 ? " Its past report will stay in History with its own copy of the thesis." : n ? ` Its ${n} past reports will stay in History with their own copy of the thesis.` : ""} This can't be undone.`)) return;
      await api(`/api/theses/${b.dataset.del}`, { method: "DELETE" }); toast("Deleted"); renderThesisList(); refreshTheses();
    }
  } catch (err) { toast(err.message); }
});

/* ---------- editor ---------- */
/* Turn what the user typed into the name the app uses, or explain why it can't be used. */
function canonical(kind, raw) {
  const v = raw.trim(), low = v.toLowerCase();
  if (!v) return { value: null };
  if (kind === "country") {
    const name = sug.country_aliases[low] || sug.countries.find((c) => c.toLowerCase() === low);
    return name ? { value: name } : { value: v, warn: `"${v}" isn't a country name the app knows - HQ is matched by country name, so check the spelling.` };
  }
  if (kind === "stage") {
    const norm = low.replace(/[\s_]+/g, "-");
    const s = sug.stages.find((x) => x.value === norm || x.label.toLowerCase() === low);
    return s ? { value: s.value } : { value: null, error: `"${v}" isn't a stage the AI reports. Pick one of the suggestions below.` };
  }
  return { value: sug.sectors.find((x) => x.toLowerCase() === low) || low };
}

function addTags(key, raws) {
  const f = FIELD[key], others = f.others.flatMap((k) => thesisData[k]);
  const added = [], notes = [];
  thesisData = { ...readForm() };
  for (const raw of raws) {
    const c = canonical(f.kind, raw);
    if (c.error) { notes.push(c.error); continue; }
    if (!c.value) continue;
    const has = (list) => list.some((x) => x.toLowerCase() === c.value.toLowerCase());
    if (has(thesisData[key])) continue;
    if (has(others)) {
      const where = f.others.find((k) => has(thesisData[k]));
      notes.push(`"${f.kind === "stage" ? stageLabel(c.value) : c.value}" is already in ${FIELD[where].label} - remove it there first.`); continue;
    }
    if (c.warn) notes.push(c.warn);
    thesisData[key] = [...thesisData[key], c.value];
    added.push(c.value);
  }
  if (added.length) setDirty(true);
  renderEditor();
  $(`#tag-${key}`).focus();
  if (notes.length) fieldNote(key, notes.join(" "));
  else if (added.length > 1) fieldNote(key, `Added ${added.length}.`, "ok");
  return added.length;
}

function fieldNote(key, msg, tone = "warn") {
  const n = $(`#note-${key}`);
  if (!n) return;
  n.textContent = msg; n.className = `field-note field-note--${tone}`; n.hidden = !msg;
}

function suggestionsFor(key, q) {
  const f = FIELD[key], have = new Set([...thesisData[key], ...f.others.flatMap((k) => thesisData[k])].map((x) => x.toLowerCase()));
  const free = (v) => !have.has(v.toLowerCase());
  const match = (list, get = (x) => x) => {
    const ql = q.toLowerCase();
    return [...list.filter((x) => get(x).toLowerCase().startsWith(ql)), ...list.filter((x) => !get(x).toLowerCase().startsWith(ql) && get(x).toLowerCase().includes(ql))];
  };
  if (f.kind === "country") {
    const regions = Object.entries(sug.regions).filter(([name]) => !q || name.toLowerCase().includes(q.toLowerCase()))
      .map(([name, list]) => ({ region: name, missing: list.filter(free).length })).filter((r) => r.missing > 0);
    const countries = (q ? match(sug.countries) : sug.popular_countries).filter(free).slice(0, 10);
    return { regions, items: countries.map((c) => ({ value: c, label: c })) };
  }
  if (f.kind === "stage") {
    const list = q ? match(sug.stages, (s) => `${s.label} ${s.value}`) : sug.stages;
    return { items: list.filter((s) => free(s.value)).map((s) => ({ value: s.value, label: s.label, hint: s.hint })) };
  }
  const popular = f.kind === "weak" ? sug.popular_weak : f.kind === "excluded" ? sug.popular_excluded : sug.popular_sectors;
  return { items: (q ? match(sug.sectors) : popular).filter(free).slice(0, 12).map((s) => ({ value: s, label: s })) };
}

function renderSuggestions(key) {
  const box = $(`#sug-${key}`);
  if (!box) return;
  const q = $(`#tag-${key}`).value.trim();
  const { regions = [], items } = suggestionsFor(key, q);
  const regionBtns = regions.map((r) => `<button type="button" class="sugg sugg--region" data-region="${esc(r.region)}" data-field="${key}"
      title="Adds ${r.missing} countr${r.missing === 1 ? "y" : "ies"}">+ ${esc(r.region)} <span class="muted">(${r.missing})</span></button>`).join("");
  const itemBtns = items.map((i) => `<button type="button" class="sugg" data-add="${esc(i.value)}" data-field="${key}"${i.hint ? ` title="${esc(i.hint)}"` : ""}>+ ${esc(i.label)}</button>`).join("");
  const empty = q ? `<span class="muted small">No matches - press Enter to add "${esc(q)}" anyway.</span>` : `<span class="muted small">All suggestions added.</span>`;
  box.innerHTML = regionBtns || itemBtns ? `<span class="suggs__label">${q ? "Matches" : regions.length ? "Quick add" : "Suggestions"}</span>${regionBtns}${itemBtns}` : empty;
}

function tagEditor(f) {
  const values = thesisData[f.key];
  const shown = (v) => (f.kind === "stage" ? stageLabel(v) : v);
  return `<div class="field">
    <div class="field__top"><label class="label" for="tag-${f.key}">${f.label} <span class="count">${values.length}</span></label>
      ${values.length ? `<button type="button" class="link-btn" data-clear="${f.key}">Clear all</button>` : ""}</div>
    <div class="tags" data-tags="${f.key}">${values.map((v, i) => `<span class="tag">${esc(shown(v))}<button type="button" data-remove="${i}" aria-label="Remove ${esc(shown(v))}">✕</button></span>`).join("")}
      <input type="text" id="tag-${f.key}" autocomplete="off" aria-describedby="help-${f.key} note-${f.key}"
        placeholder="${f.kind === "country" ? "Type a country or region..." : f.kind === "stage" ? "Type a stage, e.g. Series B..." : "Type a sector..."}"></div>
    <p class="small muted" id="help-${f.key}" style="margin:4px 0 0">${f.help} Paste a comma-separated list to add many at once.</p>
    <p class="field-note" id="note-${f.key}" role="status" hidden></p>
    <div class="suggs" id="sug-${f.key}" role="group" aria-label="Suggestions for ${f.label}"></div>
  </div>`;
}

function presetRow(kind, items, label) {
  return `<div class="presets" role="group" aria-label="${label}"><span class="suggs__label">${label}</span>${items.map((p, i) =>
    `<button type="button" class="sugg sugg--preset" data-preset="${kind}" data-i="${i}"${p.hint ? ` title="${esc(p.hint)}"` : ""}>${esc(p.text)}</button>`).join("")}</div>`;
}

function renderEditor(warnings = []) {
  const t = thesisData;
  const pending = sessionStorage.getItem("thesisTemplate");
  if (pending !== null && thesisId === null) { sessionStorage.removeItem("thesisTemplate"); thesisData = structuredClone(sug.templates[Number(pending)].data); setDirty(true); return renderEditor(); }
  $("#thesis-form").innerHTML = `
    <p><a href="#/thesis">← All fund theses</a></p>
    ${thesisId === null ? `<div class="card card--template"><h2>Start from a template</h2><p class="small muted">Fills the whole form. Nothing is saved until you click "Save".</p>
      <div class="presets">${sug.templates.map((tp, i) => `<button type="button" class="btn btn--ghost btn--sm" data-template="${i}">${esc(tp.label)}</button>`).join("")}</div></div>` : ""}
    ${warnings.length ? `<div class="field-note field-note--warn" role="status"><strong>Worth a look:</strong> ${warnings.map(esc).join(" ")}</div>` : ""}
    <div class="card"><h2>Fund</h2>
      <div class="field"><label class="label" for="fund_name">Name <span class="req" aria-hidden="true">*</span><span class="visually-hidden">(required)</span></label>
        <input type="text" id="fund_name" maxlength="80" value="${esc(t.fund_name)}" placeholder="e.g. Seed Fund I - Europe" aria-describedby="name-err"><p class="field-error" id="name-err" hidden></p></div>
      ${tagEditor(FIELD.geography)}
    </div>
    <div class="card"><h2>Stages</h2><div class="row2">${tagEditor(FIELD.stages_core)}${tagEditor(FIELD.stages_check)}</div></div>
    <div class="card"><h2>Sectors</h2><div class="row2">${tagEditor(FIELD.sectors_strong)}${tagEditor(FIELD.sectors_weak)}</div>${tagEditor(FIELD.sectors_excluded)}</div>
    <div class="card"><h2>Round size</h2>
      ${presetRow("ticket", sug.ticket_presets.map((p) => ({ text: `${p.label} ${fmtEur(p.min)}-${fmtEur(p.max)}` })), "Typical ranges")}
      <div class="row2">
        <div class="field"><label class="label" for="ticket_min_eur">Smallest round (EUR)</label><input type="number" id="ticket_min_eur" min="0" step="50000" value="${esc(t.ticket_min_eur)}" aria-describedby="tmin-h"><p class="small muted" id="tmin-h" style="margin:4px 0 0">${fmtEur(t.ticket_min_eur)} - smaller rounds are flagged. 0 = no limit.</p></div>
        <div class="field"><label class="label" for="ticket_max_eur">Largest ticket (EUR)</label><input type="number" id="ticket_max_eur" min="0" step="50000" value="${esc(t.ticket_max_eur)}" aria-describedby="tmax-h"><p class="small muted" id="tmax-h" style="margin:4px 0 0">${fmtEur(t.ticket_max_eur)} - rounds over 3× this are flagged. 0 = no limit.</p></div>
      </div>
    </div>
    <div class="card"><h2>Decision thresholds</h2>
      ${presetRow("threshold", sug.threshold_presets.map((p) => ({ text: `${p.label} (${p.advance_at} / ${p.maybe_at})`, hint: p.hint })), "Presets")}
      <div class="row2">
        <div class="field"><label class="label" for="advance_at">Advance at (score 0-10)</label><input type="number" id="advance_at" min="0" max="10" step="0.1" value="${esc(t.advance_at)}"></div>
        <div class="field"><label class="label" for="maybe_at">Maybe at (score 0-10)</label><input type="number" id="maybe_at" min="0" max="10" step="0.1" value="${esc(t.maybe_at)}"></div>
      </div>
    </div>
    <div class="card"><h2>How much each dimension counts</h2>
      ${presetRow("weights", sug.weight_presets.map((p) => ({ text: p.label, hint: p.hint })), "Presets")}
      <p class="small muted">0 = ignore, 1 = normal, 3 = triple weight. These are the base weights; they are then adapted to each company's stage and business model.</p>
      <div class="whatif__grid">${Object.keys(thesisDims).map((k) => `<div class="slider">
        <label for="tw-${k}"><span>${esc(pretty(k))}</span></label>
        <input type="range" id="tw-${k}" data-w="${k}" min="0" max="3" step="0.1" value="${esc(t.weights[k] ?? 1)}" aria-describedby="twd-${k}">
        <output for="tw-${k}">${Number(t.weights[k] ?? 1).toFixed(1)}</output>
        <span class="small muted" id="twd-${k}" style="grid-column:1/-1">${esc(thesisDims[k])}</span></div>`).join("")}</div>
    </div>
    ${thesisId === null ? "" : `<details class="card"><summary>Advanced: edit as YAML</summary>
      <label class="label" for="yaml">YAML</label>
      <textarea id="yaml" class="yaml" rows="18" spellcheck="false">${esc(thesisYaml)}</textarea>
      <div class="actions" style="margin-top:12px"><button type="button" class="btn btn--dark btn--sm" id="save-yaml">Save YAML</button></div>
    </details>`}
    <p class="form-error" id="thesis-error" role="alert" hidden></p>
    <div class="sticky-save"><button type="submit" class="btn btn--primary">${thesisId === null ? "Create fund thesis" : "Save changes"}</button>
      <a class="btn btn--ghost" href="#/thesis" id="thesis-cancel">Cancel</a>
      <span class="dirty" id="dirty" role="status" aria-live="polite">${thesisDirty ? "Unsaved changes" : ""}</span></div>`;
  TAG_FIELDS.forEach((f) => renderSuggestions(f.key));
}

function readForm() {
  const f = $("#thesis-form"), num = (id) => Number($(`#${id}`, f)?.value || 0);
  if (!$("#fund_name", f)) return { ...thesisData };
  return { ...thesisData, fund_name: $("#fund_name", f).value.trim(),
    ticket_min_eur: num("ticket_min_eur"), ticket_max_eur: num("ticket_max_eur"), advance_at: num("advance_at"), maybe_at: num("maybe_at"),
    weights: Object.fromEntries($$("[data-w]", f).map((s) => [s.dataset.w, Number(s.value)])) };
}

async function saveThesis(body) {
  const err = $("#thesis-error"), nameErr = $("#name-err");
  err.hidden = true;
  if (nameErr) { nameErr.hidden = true; $("#fund_name").removeAttribute("aria-invalid"); }
  if (body.data && !body.data.fund_name) {
    nameErr.textContent = "Give the fund thesis a name."; nameErr.hidden = false; $("#fund_name").setAttribute("aria-invalid", "true"); $("#fund_name").focus(); return;
  }
  try {
    const t = await api(thesisId === null ? "/api/theses" : `/api/theses/${thesisId}`, { method: thesisId === null ? "POST" : "PUT", body });
    setDirty(false); await refreshTheses();
    if (t.warnings.length) toast(`Saved. ${t.warnings[0]}`); else toast(thesisId === null ? "Fund thesis created" : "Saved - new screenings use it");
    if (thesisId === null) localStorage.setItem("thesisId", String(t.id));          // a new thesis becomes the one selected for screening
    const back = sessionStorage.getItem("afterThesis");                              // set when the user came here from the screening page
    sessionStorage.removeItem("afterThesis");
    location.hash = back === "screen" ? "#/screen" : "#/thesis";
  } catch (e) {
    err.textContent = e.message; err.hidden = false; err.scrollIntoView({ block: "center" });
    if (/already exists/.test(e.message) && nameErr) { nameErr.textContent = e.message; nameErr.hidden = false; $("#fund_name").setAttribute("aria-invalid", "true"); }
  }
}

const thesisForm = $("#thesis-form");
thesisForm.addEventListener("submit", (e) => { e.preventDefault(); saveThesis({ data: readForm() }); });

thesisForm.addEventListener("input", (e) => {
  const el = e.target;
  if (el.dataset.w) el.nextElementSibling.textContent = Number(el.value).toFixed(1);
  if (el.id === "ticket_min_eur" || el.id === "ticket_max_eur") {
    const h = $(el.id === "ticket_min_eur" ? "#tmin-h" : "#tmax-h");
    h.textContent = `${fmtEur(Number(el.value) || 0)} - ${el.id === "ticket_min_eur" ? "smaller rounds are flagged." : "rounds over 3× this are flagged."} 0 = no limit.`;
  }
  const box = el.closest("[data-tags]");
  if (box) { renderSuggestions(box.dataset.tags); fieldNote(box.dataset.tags, ""); return; }  // typing a tag isn't a change yet
  if (el.id !== "yaml") setDirty(true);
});

thesisForm.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.id === "save-yaml") return saveThesis({ text: $("#yaml").value });
  if (t.dataset.add) return addTags(t.dataset.field, [t.dataset.add]);
  if (t.dataset.region) return addTags(t.dataset.field, sug.regions[t.dataset.region]);
  if (t.dataset.clear) {
    const key = t.dataset.clear;
    thesisData = { ...readForm(), [key]: [] }; setDirty(true); renderEditor(); $(`#tag-${key}`).focus();
    return;
  }
  if (t.dataset.remove !== undefined) {
    const key = t.closest("[data-tags]").dataset.tags;
    thesisData = { ...readForm(), [key]: thesisData[key].filter((_, i) => i !== Number(t.dataset.remove)) };
    setDirty(true); renderEditor(); $(`#tag-${key}`).focus();
    return;
  }
  if (t.dataset.template !== undefined) {
    const tp = sug.templates[Number(t.dataset.template)];
    if (thesisDirty && !confirm(`Replace the form with the "${tp.label}" template? Your unsaved changes will be lost.`)) return;
    thesisData = structuredClone(tp.data); setDirty(true); renderEditor();
    toast(`"${tp.label}" loaded - review it, then save`);
    return;
  }
  if (t.dataset.preset) {
    const i = Number(t.dataset.i), f = thesisForm;
    if (t.dataset.preset === "ticket") {
      const p = sug.ticket_presets[i];
      $("#ticket_min_eur", f).value = p.min; $("#ticket_max_eur", f).value = p.max;
      ["ticket_min_eur", "ticket_max_eur"].forEach((id) => $(`#${id}`, f).dispatchEvent(new Event("input", { bubbles: true })));
    } else if (t.dataset.preset === "threshold") {
      const p = sug.threshold_presets[i];
      $("#advance_at", f).value = p.advance_at; $("#maybe_at", f).value = p.maybe_at;
    } else {
      const w = sug.weight_presets[i].weights;
      $$("[data-w]", f).forEach((s) => { s.value = w[s.dataset.w]; s.nextElementSibling.textContent = Number(w[s.dataset.w]).toFixed(1); });
    }
    setDirty(true);
    toast("Applied - remember to save");
  }
});

thesisForm.addEventListener("keydown", (e) => {
  // Arrow keys move between a field's input and its suggestion chips; Escape goes back to the input.
  if (e.target.classList?.contains("sugg") && e.target.dataset.field) {
    const chips = $$(`#sug-${e.target.dataset.field} .sugg`), i = chips.indexOf(e.target);
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); (chips[i + 1] || chips[0]).focus(); }
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); i === 0 ? $(`#tag-${e.target.dataset.field}`).focus() : chips[i - 1].focus(); }
    if (e.key === "Escape") $(`#tag-${e.target.dataset.field}`).focus();
    return;
  }
  const box = e.target.closest?.("[data-tags]");
  if (!box || e.target.tagName !== "INPUT") return;
  const key = box.dataset.tags, val = e.target.value.trim().replace(/,$/, "");
  if (e.key === "ArrowDown") { const first = $(`#sug-${key} .sugg`); if (first) { e.preventDefault(); first.focus(); } return; }
  if ((e.key === "Enter" || e.key === ",") && val) {
    e.preventDefault();
    const f = FIELD[key], c = canonical(f.kind, val), top = $(`#sug-${key} .sugg[data-add]`);
    const region = f.kind === "country" && Object.keys(sug.regions).find((r) => r.toLowerCase() === val.toLowerCase());
    if (region) return addTags(key, sug.regions[region]);
    // A partial word picks the top match ("ger" -> Germany, "fin" -> fintech). With no match, what was typed is used:
    // fine for a custom sector, flagged for an unknown country, refused for an unknown stage.
    const known = f.kind === "country" ? c.value && !c.warn : f.kind === "stage" ? !!c.value : sug.sectors.some((x) => x.toLowerCase() === val.toLowerCase());
    addTags(key, [known || !top ? val : top.dataset.add]);
  } else if (e.key === "Enter") e.preventDefault();
  else if (e.key === "Backspace" && !e.target.value && thesisData[key].length) {
    thesisData = { ...readForm(), [key]: thesisData[key].slice(0, -1) };
    setDirty(true); renderEditor(); $(`#tag-${key}`).focus();
  }
});

thesisForm.addEventListener("paste", (e) => {
  const box = e.target.closest?.("[data-tags]");
  if (!box) return;
  const text = e.clipboardData.getData("text");
  if (!/[,;\n]/.test(text)) return;  // single word: let it paste normally
  e.preventDefault();
  addTags(box.dataset.tags, text.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean));
});

window.addEventListener("beforeunload", (e) => { if (thesisDirty) { e.preventDefault(); e.returnValue = ""; } });
