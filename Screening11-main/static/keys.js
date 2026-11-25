"use strict";
/* AI keys page: list in priority order, add (tested first), test, enable/disable, reorder, delete. */
let keyData = { providers: [], keys: [] };
let forceSave = false;
const WHY = { auth: "key rejected", quota: "out of credit", rate: "rate limited", timeout: "too slow", network: "unreachable", server: "server error", not_found: "model unavailable", bad_request: "request rejected", too_large: "request too large", internal: "error", format: "bad answer" };
const keyStatus = (k) => ({ ready: ["tone-ok", "Ready"], cooling: ["tone-warn", `Paused ${k.seconds}s: ${WHY[k.reason] || k.reason}`], disabled: ["tone-info", "Off"], missing: ["tone-bad", "No key value"], incomplete: ["tone-bad", "Incomplete"] })[k.status] || ["tone-info", k.status];

async function loadKeys() {
  try { keyData = await api("/api/llm-keys"); } catch (e) { $("#keys-list").innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }
  const sel = $("#k-provider");
  if (!sel.options.length) {
    sel.innerHTML = keyData.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}${p.free_tier ? " (free tier)" : ""}</option>`).join("");
    sel.value = "groq"; providerChanged();
  }
  renderKeys();
}

function renderKeys() {
  const ks = keyData.keys;
  $("#keys-list").innerHTML = ks.length ? `<ol class="keys">${ks.map((k, i) => {
    const [tone, label] = keyStatus(k);
    return `<li class="panel key-row" data-id="${k.id}">
      <div class="key-row__main"><strong>${i + 1}. ${esc(k.label)}</strong>
        <span class="badge ${tone}">${esc(label)}</span>
        <span class="muted small">${esc(k.provider)} · ${esc(k.models_tried.length > 1 ? k.models_tried.join(" → ") : k.model || "default model")} · ${k.source === "env" ? `from ${esc(k.env_names.join("/"))}` : "saved in app"} ${esc(k.key_hint)}</span></div>
      <div class="key-row__actions">
        <button class="btn btn--ghost btn--sm" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Move ${esc(k.label)} up">↑</button>
        <button class="btn btn--ghost btn--sm" data-act="down" ${i === ks.length - 1 ? "disabled" : ""} aria-label="Move ${esc(k.label)} down">↓</button>
        <button class="btn btn--ghost btn--sm" data-act="test">Test</button>
        <button class="btn btn--ghost btn--sm" data-act="toggle">${k.enabled ? "Turn off" : "Turn on"}</button>
        ${k.source === "env" ? "" : `<button class="btn btn--ghost btn--sm" data-act="delete">Delete</button>`}
      </div><p class="small" data-result role="status" aria-live="polite"></p></li>`;
  }).join("")}</ol>` : `<div class="alert alert--danger" role="alert">No AI key yet. Add one below to start screening.</div>`;
}

$("#keys-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]"); if (!btn) return;
  const row = btn.closest("[data-id]"), id = row.dataset.id, k = keyData.keys.find((x) => String(x.id) === id), out = $("[data-result]", row);
  try {
    if (btn.dataset.act === "test") {
      btn.disabled = true; out.textContent = "Testing…";
      const r = await api(`/api/llm-keys/${id}/test`, { method: "POST" });
      out.textContent = r.ok ? `Works (${r.model}, ${r.ms} ms)` : r.error; out.className = `small ${r.ok ? "" : "form-error"}`;
      keyData = await api("/api/llm-keys"); loadHealth(); setTimeout(renderKeys, 2500); return;
    }
    if (btn.dataset.act === "delete") { if (!confirm(`Delete "${k.label}"?`)) return; await api(`/api/llm-keys/${id}`, { method: "DELETE" }); }
    else await api(`/api/llm-keys/${id}`, { method: "PUT", body: btn.dataset.act === "toggle" ? { enabled: !k.enabled } : { move: btn.dataset.act } });
    keyData = await api("/api/llm-keys"); renderKeys(); loadHealth();
  } catch (err) { out.textContent = err.message; out.className = "small form-error"; btn.disabled = false; }
});

function providerChanged() {
  const p = keyData.providers.find((x) => x.id === $("#k-provider").value); if (!p) return;
  $("#k-help").textContent = `${p.hint}${p.env.length ? ` Or set ${p.env[0]} in the environment.` : ""}`;
  $("#k-base-f").hidden = !p.needs_base_url;
  $("#k-model-opt").textContent = p.needs_model ? "required" : "optional";
  $("#k-model").placeholder = p.default_model || "model name";
  $("#k-error").hidden = true; $("#k-force").hidden = true;
}
$("#k-provider").addEventListener("change", providerChanged);

async function submitKey(skip) {
  const err = $("#k-error"), btn = $("#k-add");
  err.hidden = true; $("#k-force").hidden = true; btn.disabled = true; btn.textContent = skip ? "Saving…" : "Testing…";
  try {
    await api("/api/llm-keys", { method: "POST", body: { provider: $("#k-provider").value, api_key: $("#k-key").value, label: $("#k-label").value, model: $("#k-model").value, base_url: $("#k-base").value, skip_test: skip } });
    $("#k-key").value = ""; $("#k-label").value = ""; $("#k-model").value = "";
    toast("Key saved"); keyData = await api("/api/llm-keys"); renderKeys(); loadHealth();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
    $("#k-force").hidden = !e.can_save_anyway;
  } finally { btn.disabled = false; btn.textContent = "Test and save"; }
}
$("#key-form").addEventListener("submit", (e) => { e.preventDefault(); submitKey(false); });
$("#k-force").addEventListener("click", () => submitKey(true));
