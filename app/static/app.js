"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { settings: null, languages: {}, presets: {}, overview: null, glossary: [], suggestions: [] };

const SAMPLE_TEXT = [
  "The census date for Term 3 is Sunday of Week 4. If you drop a course after the census date, you will still be charged fees.",
  "Your first tutorial is in Week 2. Check your class timetable in myUNSW before then.",
  "Special consideration applications must be lodged within three working days of the assessment.",
  "Members can collect their society hoodie at the welcome night in the Roundhouse.",
].join("\n");

const EXAMPLE_TERMS = [
  { source: "census date", target: "退课截止日", note: "Last day to drop without paying fees" },
  { source: "special consideration", target: "特殊考虑申请", note: "UNSW process for illness or misadventure" },
  { source: "tutorial", target: "辅导课", note: "" },
];

/* ---------------- helpers ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const nf = new Intl.NumberFormat();
const fmt = (n) => nf.format(n || 0);

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (res.status === 401 && !path.startsWith("/api/login") && !path.startsWith("/api/setup")) {
    showAuth(false);
    throw new Error("Please sign in.");
  }
  if (!res.ok) {
    const msg = data && (typeof data.detail === "string" ? data.detail : data.error?.message);
    throw new Error(msg || `Request failed (${res.status}).`);
  }
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 5000 : 2400);
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = label; }
}

async function copyText(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast(what);
}

/** Wrap glossary terms in <mark>, escaping everything else. */
function highlight(text, terms, side) {
  const list = (terms || [])
    .map((t) => ({ word: side === "source" ? t.source : t.target, applied: t.applied !== false }))
    .filter((t) => t.word)
    .sort((a, b) => b.word.length - a.word.length);
  if (!list.length) return esc(text);
  const pattern = new RegExp(list.map((t) => t.word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
  let out = "", last = 0, m;
  while ((m = pattern.exec(text))) {
    if (!m[0]) { pattern.lastIndex++; continue; }
    const hit = list.find((t) => t.word.toLowerCase() === m[0].toLowerCase());
    out += esc(text.slice(last, m.index));
    const missed = side === "source" && hit && !hit.applied;
    out += `<mark class="term${missed ? " missed" : ""}"${missed ? ' title="The translation did not use the glossary term"' : ""}>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + esc(text.slice(last));
}

function langOptions(selected) {
  return Object.entries(state.languages)
    .map(([code, name]) => `<option value="${esc(code)}"${code === selected ? " selected" : ""}>${esc(name)}</option>`)
    .join("");
}
function fillLangSelects() {
  const def = state.settings?.default_target_lang || "zh-CN";
  $$(".lang-select").forEach((sel) => { const keep = sel.value; sel.innerHTML = langOptions(keep || def); });
}

function timeAgo(ts) {
  const s = Math.max(1, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

/* ---------------- auth ---------------- */
function showAuth(setup) {
  $("#app").hidden = true;
  $("#auth").hidden = false;
  $("#setup-form").hidden = !setup;
  $("#login-form").hidden = setup;
  $(setup ? "#setup-form input[name=team_name]" : "#login-form input[name=password]").focus();
}

async function boot() {
  const me = await api("/api/me");
  if (!me.authenticated) return showAuth(me.setup_required);
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#team-name").textContent = me.team_name || "";
  await loadSettings();
  route();
}

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/setup", { method: "POST", body: { password: f.password.value, team_name: f.team_name.value } });
    location.hash = "#settings";
    boot();
  } catch (err) { $(".form-error", f).textContent = err.message; }
});

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/login", { method: "POST", body: { password: f.password.value } });
    f.reset();
    boot();
  } catch (err) { $(".form-error", f).textContent = err.message; }
});

$("#signout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showAuth(false);
});

/* ---------------- router ---------------- */
const VIEWS = { overview: loadOverview, glossary: loadGlossary, try: () => {}, settings: renderSettings };
function route() {
  if ($("#app").hidden) return;
  const view = (location.hash || "#overview").slice(1);
  const name = VIEWS[view] ? view : "overview";
  $$("[data-view-panel]").forEach((p) => (p.hidden = p.dataset.viewPanel !== name));
  $$(".nav a").forEach((a) => {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  Promise.resolve().then(VIEWS[name]).catch((err) => toast(err.message, true));
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

/* ---------------- overview ---------------- */
async function loadOverview() {
  const o = await api("/api/overview");
  state.overview = o;
  const s = o.stats;
  $("#setup-notice").hidden = o.configured;
  $("#ep-openai").textContent = o.endpoints.openai;
  $("#ep-custom").textContent = o.endpoints.custom;
  $("#team-key").textContent = o.team_key;

  if (!s.items) {
    $("#statement").innerHTML = "No translations yet. Connect the extension and open any foreign-language page.";
    $("#statement-sub").textContent = "Once your team starts reading, this page shows how much came from shared memory and how well the glossary was followed.";
  } else {
    const pct = Math.round(s.hit_rate * 100);
    $("#statement").innerHTML =
      `Your team has translated <b>${fmt(s.items)}</b> paragraphs. <b class="from-memory">${fmt(s.cache_hits)}</b> came straight from memory.`;
    const parts = [`That's ${pct}% your AI provider never had to process, about ${fmt(s.tokens_saved_est)} tokens (${s.usd_saved_est < 0.01 ? "under $0.01" : "$" + s.usd_saved_est.toFixed(2)}).`];
    if (s.glossary_compliance !== null) parts.push(`Glossary terms were followed in ${Math.round(s.glossary_compliance * 100)}% of ${fmt(s.terms_matched)} matches.`);
    if (s.errors) parts.push(`${fmt(s.errors)} requests failed; see recent activity.`);
    $("#statement-sub").textContent = parts.join(" ");
  }

  renderChart($("#chart"), s.days);

  $("#top-terms").innerHTML = s.top_terms.length
    ? s.top_terms.map((t) => `<li><span class="term-pair"><mark class="term">${esc(t.source)}</mark><span class="arrow" aria-label="becomes">→</span>${esc(t.target)}</span><span class="muted small">${fmt(t.hit_count)}×</span></li>`).join("")
    : `<li class="muted">Terms appear here once translations use them. <a href="#glossary">Add terms</a></li>`;

  const names = { immersive: "Extension (custom API)", openai: "Extension (OpenAI)", dashboard: "Try it" };
  $("#recent").innerHTML = s.recent.length
    ? `<table><thead><tr><th>When</th><th>From</th><th class="num">Paragraphs</th><th class="num">From memory</th><th class="num">Terms</th><th class="num">Time</th><th>Result</th></tr></thead><tbody>${
        s.recent.map((r) => `<tr><td>${timeAgo(r.ts)}</td><td>${esc(names[r.endpoint] || r.endpoint)}</td><td class="num">${fmt(r.items)}</td><td class="num">${fmt(r.cache_hits)}</td><td class="num">${fmt(r.terms_matched)}</td><td class="num">${(r.ms / 1000).toFixed(1)} s</td><td>${r.ok ? '<span class="status-ok">OK</span>' : `<span class="status-err" title="${esc(r.error)}">Failed</span> <span class="muted small">${esc(r.error).slice(0, 80)}</span>`}</td></tr>`).join("")
      }</tbody></table>`
    : `<p class="muted">Nothing yet. Requests from the extension and Try it show up here.</p>`;
}

function renderChart(el, days) {
  const W = 560, H = 190, padL = 30, padB = 26, padT = 10;
  const max = Math.max(4, ...days.map((d) => d.items));
  const nice = Math.ceil(max / 4) * 4;
  const plotW = W - padL, plotH = H - padB - padT;
  const slot = plotW / days.length, bw = Math.min(22, slot * 0.62);
  const y = (v) => padT + plotH - (v / nice) * plotH;
  const roundTop = (x, top, w, h, r) => {
    r = Math.min(r, h, w / 2);
    return `M${x},${top + h}V${top + r}Q${x},${top} ${x + r},${top}H${x + w - r}Q${x + w},${top} ${x + w},${top + r}V${top + h}Z`;
  };
  let cols = "";
  days.forEach((d, i) => {
    const x = padL + i * slot + (slot - bw) / 2;
    const memory = d.hits, fresh = Math.max(0, d.items - d.hits);
    const label = new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
    let segs = "";
    const memH = (memory / nice) * plotH, newH = (fresh / nice) * plotH;
    const base = padT + plotH;
    if (memory) segs += fresh ? `<rect class="seg" x="${x}" y="${base - memH}" width="${bw}" height="${memH}" fill="var(--series-memory)"/>` : `<path class="seg" d="${roundTop(x, base - memH, bw, memH, 4)}" fill="var(--series-memory)"/>`;
    if (fresh) {
      const gap = memory ? 2 : 0;
      segs += `<path class="seg" d="${roundTop(x, base - memH - gap - newH, bw, newH, 4)}" fill="var(--series-new)"/>`;
    }
    cols += `<g class="col" tabindex="0" data-tip="${esc(label)}: ${fmt(d.items)} paragraphs, ${fmt(memory)} from memory" aria-label="${esc(label)}: ${fmt(d.items)} paragraphs, ${fmt(memory)} from memory">${segs}<rect class="hit" x="${padL + i * slot}" y="${padT}" width="${slot}" height="${plotH}"/></g>`;
  });
  const ticks = [0, nice / 2, nice].map((v) => `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-text" x="${padL - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`).join("");
  const xLabels = [0, 7, 13].map((i) => {
    const d = days[i];
    const label = new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return `<text class="axis-text" x="${padL + i * slot + slot / 2}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`;
  }).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Paragraphs translated per day for the last 14 days">${ticks}<line class="baseline" x1="${padL}" x2="${W}" y1="${padT + plotH}" y2="${padT + plotH}"/>${cols}${xLabels}</svg>
    <table class="sr-only"><caption>Paragraphs per day</caption><tr><th>Day</th><th>Paragraphs</th><th>From memory</th></tr>${days.map((d) => `<tr><td>${d.day}</td><td>${d.items}</td><td>${d.hits}</td></tr>`).join("")}</table>
    <div class="tooltip" hidden></div>`;
  const tip = $(".tooltip", el);
  const show = (g) => {
    const hit = $(".hit", g).getBoundingClientRect(), host = el.getBoundingClientRect();
    const segTops = $$(".seg", g).map((s) => s.getBoundingClientRect().top);
    const top = segTops.length ? Math.min(...segTops) : hit.bottom;
    tip.textContent = g.dataset.tip;
    tip.style.left = `${Math.min(Math.max(hit.left - host.left + hit.width / 2, 90), host.width - 90)}px`;
    tip.style.top = `${top - host.top - 8}px`;
    tip.hidden = false;
  };
  $$(".col", el).forEach((g) => {
    g.addEventListener("mouseenter", () => show(g));
    g.addEventListener("focus", () => show(g));
    g.addEventListener("mouseleave", () => (tip.hidden = true));
    g.addEventListener("blur", () => (tip.hidden = true));
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (btn) copyText($("#" + btn.dataset.copy).textContent);
});

$("#copy-invite").addEventListener("click", () => {
  const o = state.overview;
  if (!o) return;
  const msg = [
    "We now share one translation memory and glossary in Immersive Translate (沉浸式翻译). Set it up once:",
    "",
    "1. Immersive Translate settings → Translation services (翻译服务) → OpenAI → turn on Custom API Key (自定义 API Key)",
    `2. API address (自定义 API 接口地址): ${o.endpoints.openai}`,
    `3. API key: ${o.team_key}`,
    "4. Model: leave as is. Then pick OpenAI as your translation service.",
    "",
    "Keep the key inside the team.",
  ].join("\n");
  copyText(msg, "Setup message copied. Paste it in your team chat.");
});

/* ---------------- glossary ---------------- */
async function loadGlossary() {
  const q = $("#glossary-search").value.trim();
  const { terms } = await api("/api/glossary" + (q ? `?q=${encodeURIComponent(q)}` : ""));
  state.glossary = terms;
  renderGlossary(q);
}

function renderGlossary(q) {
  const terms = state.glossary;
  $("#glossary-count").textContent = q ? `${fmt(terms.length)} matching` : `${fmt(terms.length)} ${terms.length === 1 ? "term" : "terms"}`;
  if (!terms.length) {
    $("#glossary-list").innerHTML = q
      ? `<div class="empty"><h3>No terms match “${esc(q)}”.</h3><p>Try a shorter search, or add it above.</p></div>`
      : `<div class="empty"><h3>No terms yet</h3><p>Add the names and phrases your team always wants translated the same way, or let the AI suggest some from a sample below.</p><p style="margin-top:16px"><button class="btn" type="button" id="add-examples">Add 3 example terms</button></p></div>`;
    $("#add-examples")?.addEventListener("click", async () => {
      await api("/api/glossary/bulk", { method: "POST", body: { terms: EXAMPLE_TERMS.map((t) => ({ ...t, target_lang: "zh-CN" })) } });
      toast("Example terms added");
      loadGlossary();
    });
    return;
  }
  $("#glossary-list").innerHTML = `<table class="glossary-table"><thead><tr><th>Term</th><th>Translation</th><th>Language</th><th>Note</th><th class="num">Used</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${
    terms.map((t) => `<tr data-id="${t.id}">
      <td class="src"><mark class="term">${esc(t.source)}</mark>${t.case_sensitive ? ' <span class="pill" title="Matches exact capitalisation">Aa</span>' : ""}</td>
      <td class="tgt">${esc(t.target)}</td>
      <td class="muted">${esc(state.languages[t.target_lang] || t.target_lang)}</td>
      <td class="muted small">${esc(t.note)}</td>
      <td class="num muted">${fmt(t.hit_count)}</td>
      <td class="actions"><button class="btn-quiet btn" type="button" data-edit="${t.id}">Edit</button><button class="btn-quiet btn" type="button" data-del="${t.id}">Delete</button></td>
    </tr>`).join("")
  }</tbody></table>`;
}

$("#glossary-list").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  const edit = e.target.closest("[data-edit]");
  const save = e.target.closest("[data-save]");
  const cancel = e.target.closest("[data-cancel]");
  if (del) {
    const t = state.glossary.find((x) => x.id == del.dataset.del);
    if (!confirm(`Delete “${t.source}”? Teammates will stop getting this translation.`)) return;
    await api(`/api/glossary/${t.id}`, { method: "DELETE" });
    toast("Term deleted");
    loadGlossary();
  } else if (edit) {
    const t = state.glossary.find((x) => x.id == edit.dataset.edit);
    const tr = edit.closest("tr");
    tr.classList.add("editing");
    tr.innerHTML = `<td><input type="text" class="lang-text" name="source" value="${esc(t.source)}" aria-label="Term"></td>
      <td><input type="text" class="lang-text" name="target" value="${esc(t.target)}" aria-label="Translation"></td>
      <td><select name="target_lang" aria-label="Language">${langOptions(t.target_lang)}</select></td>
      <td><input type="text" name="note" value="${esc(t.note)}" aria-label="Note"><label class="check small" style="margin-top:6px"><input type="checkbox" name="case_sensitive"${t.case_sensitive ? " checked" : ""}> Match capitalisation</label></td>
      <td></td>
      <td class="actions"><button class="btn btn-primary" type="button" data-save="${t.id}">Save</button> <button class="btn-quiet btn" type="button" data-cancel>Cancel</button></td>`;
    $("input[name=target]", tr).focus();
  } else if (save) {
    const tr = save.closest("tr");
    const body = {
      source: $("[name=source]", tr).value, target: $("[name=target]", tr).value,
      target_lang: $("[name=target_lang]", tr).value, note: $("[name=note]", tr).value,
      case_sensitive: $("[name=case_sensitive]", tr).checked,
    };
    try {
      await api(`/api/glossary/${save.dataset.save}`, { method: "PUT", body });
      toast("Term saved. New translations use it right away.");
      loadGlossary();
    } catch (err) { toast(err.message, true); }
  } else if (cancel) {
    renderGlossary($("#glossary-search").value.trim());
  }
});

$("#add-term").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/glossary", { method: "POST", body: { source: f.source.value, target: f.target.value, target_lang: f.target_lang.value, note: f.note.value } });
    toast(`Added “${f.source.value.trim()}”`);
    f.source.value = ""; f.target.value = ""; f.note.value = "";
    f.source.focus();
    loadGlossary();
  } catch (err) { toast(err.message, true); }
});

let searchTimer;
$("#glossary-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadGlossary, 200); });

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const res = await api("/api/glossary/import", { method: "POST", body: { csv: await file.text() } });
    toast(`Imported ${fmt(res.added)} terms${res.skipped ? `, skipped ${res.skipped} incomplete rows` : ""}`);
    loadGlossary();
  } catch (err) { toast(err.message, true); }
  e.target.value = "";
});

$("#suggest-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  e.currentTarget.textContent = "Reading the sample…";
  $("#suggest-status").textContent = "";
  try {
    const { terms } = await api("/api/glossary/suggest", { method: "POST", body: { text: $("#suggest-text").value } });
    state.suggestions = terms;
    $("#suggest-results").hidden = !terms.length;
    $("#suggest-status").textContent = terms.length ? `${terms.length} suggestions. Untick any you don't want.` : "No terms found in this sample.";
    $("#suggest-list").innerHTML = terms.map((t, i) => `<li><label><input type="checkbox" data-sugg="${i}"${t.exists ? "" : " checked"}>
      <span class="term-pair"><mark class="term">${esc(t.source)}</mark><span class="arrow">→</span><input type="text" class="lang-text" value="${esc(t.target)}" data-sugg-target="${i}" style="width:auto;display:inline-block;padding:4px 8px"></span>
      <span class="muted small">${t.exists ? "Already in glossary" : esc(t.note)}</span></label></li>`).join("");
  } catch (err) { toast(err.message, true); }
}));

$("#suggest-add").addEventListener("click", async () => {
  const picked = $$("[data-sugg]").filter((c) => c.checked).map((c) => {
    const t = state.suggestions[c.dataset.sugg];
    return { ...t, target: $(`[data-sugg-target="${c.dataset.sugg}"]`).value };
  });
  if (!picked.length) return toast("Tick at least one term", true);
  const res = await api("/api/glossary/bulk", { method: "POST", body: { terms: picked } });
  toast(`Added ${fmt(res.added)} terms`);
  $("#suggest-results").hidden = true;
  $("#suggest-status").textContent = "";
  loadGlossary();
});

/* ---------------- try it ---------------- */
$("#try-sample").addEventListener("click", () => { $("#try-text").value = SAMPLE_TEXT; $("#try-text").focus(); });

$("#try-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const btn = $("button[type=submit]", f);
  busy(btn, async () => {
    btn.textContent = "Translating…";
    try {
      const r = await api("/api/translate", { method: "POST", body: { text: f.text.value, target_lang: f.target_lang.value } });
      const items = r.items.filter((i) => !i.skipped);
      const cached = items.filter((i) => i.cached).length;
      const summary = `Translated ${items.length} ${items.length === 1 ? "paragraph" : "paragraphs"} in ${(r.ms / 1000).toFixed(1)} s with ${r.llm_calls} AI ${r.llm_calls === 1 ? "call" : "calls"}. ${cached ? `${cached} came from memory.` : "Nothing came from memory yet; run it again."}`;
      $("#try-output").innerHTML = `<p class="result-summary">${esc(summary)}</p>` + items.map((i) => {
        const applied = i.terms.filter((t) => t.applied).length;
        const meta = [
          i.cached ? '<span class="pill pill-memory">From memory</span>' : "",
          i.terms.length ? `<span class="pill">${applied} of ${i.terms.length} glossary ${i.terms.length === 1 ? "term" : "terms"} used</span>` : "",
        ].join("");
        return `<div class="para${i.cached ? " cached" : ""}"><p class="src">${highlight(i.source, i.terms, "source")}</p><p class="tgt">${highlight(i.text, i.terms, "target")}</p>${meta ? `<div class="meta">${meta}</div>` : ""}</div>`;
      }).join("");
    } catch (err) {
      $("#try-output").innerHTML = `<p class="result-empty status-err">${esc(err.message)}</p>`;
    }
  });
});

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  state.languages = s.languages;
  state.presets = s.presets;
  fillLangSelects();
}

function guessProvider(s) {
  if (s.provider) return s.provider;
  const hit = Object.entries(state.presets).find(([, p]) => p.base_url === s.llm_base_url);
  return hit ? hit[0] : "custom";
}

async function renderSettings() {
  await loadSettings();
  const s = state.settings, f = $("#settings-form");
  $("#provider").innerHTML = Object.entries(s.presets).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("") + `<option value="custom">Other (OpenAI-compatible)</option>`;
  f.provider.value = guessProvider(s);
  f.llm_base_url.value = s.llm_base_url;
  f.llm_model.value = s.llm_model;
  f.llm_api_key.value = "";
  f.llm_api_key.placeholder = s.has_api_key ? `Saved: ${s.llm_api_key_masked}` : "Paste your API key";
  $("#api-key-help").textContent = s.has_api_key ? "Leave blank to keep the saved key." : "Required. Get one from your provider's dashboard.";
  f.default_target_lang.innerHTML = langOptions(s.default_target_lang);
  f.style_guide.value = s.style_guide;
  f.temperature.value = s.temperature;
  f.price_per_m_tokens.value = s.price_per_m_tokens;
  f.team_name.value = s.team_name;
  $("#test-result").textContent = "";
  const st = await api("/api/stats");
  $("#memory-size").textContent = `${fmt(st.cache_entries)} saved translations, reused for everyone.`;
}

$("#provider").addEventListener("change", (e) => {
  const p = state.presets[e.target.value];
  if (!p) return;
  const f = $("#settings-form");
  f.llm_base_url.value = p.base_url;
  f.llm_model.value = p.model;
});

function settingsBody() {
  const f = $("#settings-form");
  return Object.fromEntries(["provider", "llm_base_url", "llm_model", "llm_api_key", "default_target_lang", "style_guide", "temperature", "price_per_m_tokens", "team_name"].map((k) => [k, f[k].value]));
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: settingsBody() });
    $("#team-name").textContent = $("#settings-form").team_name.value;
    toast("Settings saved");
    renderSettings();
  } catch (err) { toast(err.message, true); }
});

$("#test-btn").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  e.currentTarget.textContent = "Testing…";
  const out = $("#test-result");
  out.className = "test-result muted";
  out.textContent = "";
  try {
    await api("/api/settings", { method: "PUT", body: settingsBody() });
    const r = await api("/api/settings/test", { method: "POST" });
    const msg = `Connected to ${r.model} in ${(r.ms / 1000).toFixed(1)} s. It replied: “${r.reply}”`;
    await renderSettings(); // shows the newly saved key as masked
    out.className = "test-result status-ok";
    out.textContent = msg;
  } catch (err) {
    out.className = "test-result status-err";
    out.textContent = err.message;
  }
}));

$("#rotate-btn").addEventListener("click", async () => {
  if (!confirm("Generate a new team key? The current key stops working immediately, so everyone needs the new one.")) return;
  await api("/api/team-key/rotate", { method: "POST" });
  toast("New team key generated. Share it from the overview.");
});

$("#clear-btn").addEventListener("click", async () => {
  if (!confirm("Clear all saved translations? The glossary stays. Pages will be translated again (and billed) the next time someone opens them.")) return;
  const r = await api("/api/cache/clear", { method: "POST" });
  toast(`Cleared ${fmt(r.cleared)} saved translations`);
  renderSettings();
});

boot().catch((err) => toast(err.message, true));
