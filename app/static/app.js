"use strict";
/* Admin dashboard. Shared helpers (t, tn, fmt, esc, $, toast, …) come from i18n.js. */

const state = {
  settings: null, presets: {}, overview: null, glossary: [], aiSuggestions: [],
  proposals: [], history: [], historyAll: false, view: null, sets: [], links: [],
};

const SAMPLE_TEXT = [
  "The census date for Term 3 is Sunday of Week 4. If you drop a course after the census date, you will still be charged fees.",
  "Your first tutorial is in Week 2. Check your class timetable in myUNSW before then.",
  "Special consideration applications must be lodged within three working days of the assessment.",
  "Members can collect their society hoodie at the welcome night in the Roundhouse.",
].join("\n");

function onLangChange() {
  fillLangSelects();
  if (!$("#app").hidden) route(true);
}

/* ---------------- helpers ---------------- */
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    const e = new Error(t("err.network"));
    e.code = "network";
    throw e;
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const e = new Error("");
    e.status = res.status;
    e.code = data?.code;
    e.params = data?.params;
    e.raw = data?.raw || "";
    e.message = errorText(e);
    if (res.status === 401 && e.code === "not_signed_in") showAuth(false);
    throw e;
  }
  return data;
}

async function busy(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  if (label) btn.textContent = label;
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = original; }
}

function langOptions(selected) {
  return Object.keys(LANG_FALLBACK)
    .map((code) => `<option value="${esc(code)}"${code === selected ? " selected" : ""}>${esc(langName(code))}</option>`)
    .join("");
}
function fillLangSelects() {
  if (!Object.keys(LANG_FALLBACK).length) return;
  const def = state.settings?.default_target_lang || "zh-CN";
  $$(".lang-select").forEach((sel) => { const keep = sel.value; sel.innerHTML = langOptions(keep || def); });
}

function rawDetails(raw) {
  return raw ? `<details class="err-details"><summary>${esc(t("err.details"))}</summary><pre>${esc(raw)}</pre></details>` : "";
}

/* ---------------- term warnings ---------------- */
/** Explains why a term looks risky and asks whether to save it anyway. Resolves true to save. */
function warningDialog(p) {
  return new Promise((resolve) => {
    const dlg = $("#warn-dialog");
    const reasons = p.warnings.map((w) => `<li>${esc(t("warn." + w, { affected: fmt(p.affected), total: fmt(p.total) }))}</li>`).join("");
    const affected = p.affected && !p.warnings.includes("broad") ? `<p class="small muted">${esc(tn("warn.affected", p.affected))}</p>` : "";
    const samples = p.samples?.length
      ? `<p class="small muted">${esc(t("warn.examples"))}</p><ul class="warn-samples">${p.samples.map((s) => `<li class="lang-text">${highlight(s, [{ source: p.term }], "source")}</li>`).join("")}</ul>`
      : "";
    $("#warn-body").innerHTML = `<h2 id="warn-title">${esc(t("warn.title", { term: p.term }))}</h2><ul class="warn-list">${reasons}</ul>${affected}${samples}<p class="small muted warn-tip">${esc(t("warn.tip"))}</p>`;
    const done = (ok) => { dlg.close(); resolve(ok); };
    $("#warn-save").onclick = () => done(true);
    $("#warn-back").onclick = () => done(false);
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    dlg.showModal();
    $("#warn-back").focus();
  });
}

/** Sends a glossary change. If the server flags the term, asks first and resends with force. Returns null if cancelled. */
async function saveWithCheck(path, method, body) {
  try {
    return await api(path, { method, body });
  } catch (err) {
    if (err.code !== "term_warning") throw err;
    if (!(await warningDialog(err.params))) return null;
    return api(path, { method, body: { ...body, force: true } });
  }
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
  await loadI18n();
  const me = await api("/api/me");
  if (!me.authenticated) return showAuth(me.setup_required);
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#team-name").textContent = me.team_name || "";
  await loadSettings();
  route();
  refreshProposals().catch(() => {});
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
    $(".form-error", f).textContent = "";
    boot();
  } catch (err) { $(".form-error", f).textContent = err.message; }
});

$("#signout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showAuth(false);
});

/* ---------------- router ---------------- */
const VIEWS = { overview: loadOverview, glossary: loadGlossaryView, sets: loadSetsView, try: loadTryView, settings: renderSettings };
function route(keepScroll = false) {
  if ($("#app").hidden) return;
  const view = (location.hash || "#overview").slice(1);
  const name = VIEWS[view] ? view : "overview";
  state.view = name;
  $$("[data-view-panel]").forEach((p) => (p.hidden = p.dataset.viewPanel !== name));
  $$(".nav a").forEach((a) => {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  Promise.resolve().then(VIEWS[name]).catch((err) => toast(err.message, true));
  if (!keepScroll) window.scrollTo(0, 0);
}
window.addEventListener("hashchange", () => route());

function setPendingBadge(n) {
  const badge = $("#nav-badge");
  badge.hidden = !n;
  badge.textContent = fmt(n);
  badge.setAttribute("aria-label", tn("nav.pending", n));
}

/* ---------------- overview ---------------- */
async function loadOverview() {
  const o = await api("/api/overview");
  state.overview = o;
  const s = o.stats;
  $("#setup-notice").hidden = o.configured;
  $("#proposal-notice").hidden = !o.pending_proposals;
  $("#proposal-notice-text").textContent = tn("overview.proposals", o.pending_proposals);
  setPendingBadge(o.pending_proposals);
  $("#ep-openai").textContent = o.endpoints.openai;
  $("#ep-custom").textContent = o.endpoints.custom;
  $("#team-key").textContent = o.team_key;
  $("#suggest-url").textContent = o.suggest_url;

  const reused = s.cache_hits + s.glossary_direct;
  if (!s.items) {
    $("#statement").textContent = t("overview.empty_statement");
    $("#statement-sub").textContent = t("overview.empty_sub");
  } else {
    const key = s.items === 1 && I18N[LANG]["overview.statement_html_one"] ? "overview.statement_html_one" : "overview.statement_html";
    $("#statement").innerHTML = t(key, { items: fmt(s.items), hits: fmt(reused) });
    const amount = s.usd_saved_est < 0.01 ? t("overview.usd_tiny", { amount: usd(0.01) }) : usd(s.usd_saved_est);
    const parts = [t("overview.sub_saved", { pct: pct(s.hit_rate), tokens: fmt(s.tokens_saved_est), usd: amount })];
    if (s.glossary_compliance !== null) parts.push(t("overview.sub_compliance", { pct: pct(s.glossary_compliance), n: fmt(s.terms_matched) }));
    if (s.errors_24h) parts.push(tn("overview.sub_errors", s.errors_24h));
    $("#statement-sub").textContent = parts.join(" ");
  }
  const callParams = { calls: fmt(s.calls_last_hour), peak: fmt(s.peak_rpm_last_hour), limit: fmt(s.max_rpm) };
  $("#calls-line").textContent = s.calls_last_hour ? t(s.max_rpm ? "overview.calls" : "overview.calls_nolimit", callParams) : "";

  renderChart($("#chart"), s.days);

  $("#top-terms").innerHTML = s.top_terms.length
    ? s.top_terms.map((x) => `<li><span class="term-pair"><mark class="term">${esc(x.source)}</mark><span class="arrow" aria-label="${esc(t("terms.becomes"))}">→</span>${esc(x.target)}</span><span class="muted small">${esc(t("terms.count", { n: fmt(x.hit_count) }))}</span></li>`).join("")
    : `<li class="muted">${esc(t("terms.empty"))} <a href="#glossary">${esc(t("terms.add_link"))}</a></li>`;

  $("#recent").innerHTML = s.recent.length
    ? `<table><thead><tr><th>${esc(t("recent.when"))}</th><th>${esc(t("recent.from"))}</th><th class="num">${esc(t("recent.paragraphs"))}</th><th class="num">${esc(t("recent.reused"))}</th><th class="num">${esc(t("recent.ai_calls"))}</th><th class="num">${esc(t("recent.terms"))}</th><th class="num">${esc(t("recent.time"))}</th><th>${esc(t("recent.result"))}</th></tr></thead><tbody>${
        s.recent.map((r) => {
          const result = r.ok
            ? `<span class="status-ok">${esc(t("recent.ok"))}</span>`
            : `<span class="status-err" title="${esc(r.error)}">${esc(t("recent.failed"))}</span> <span class="muted small" title="${esc(r.error)}">${esc(errorText({ code: r.error_code || "upstream_error" }))}</span>`;
          return `<tr><td>${esc(timeAgo(r.ts))}</td><td>${esc(t("source." + r.endpoint))}</td><td class="num">${fmt(r.items)}</td><td class="num">${fmt(r.cache_hits + (r.glossary_direct || 0))}</td><td class="num">${fmt(r.llm_calls)}</td><td class="num">${fmt(r.terms_matched)}</td><td class="num">${esc(seconds(r.ms))}</td><td class="result-cell">${result}</td></tr>`;
        }).join("")
      }</tbody></table>`
    : `<p class="muted">${esc(t("recent.empty"))}</p>`;
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
    const reused = Math.min(d.hits, d.items), fresh = Math.max(0, d.items - reused);
    const tip = d.items === 1 && I18N[LANG]["chart.tip_one"]
      ? t("chart.tip_one", { date: shortDate(d.day), items: fmt(d.items), hits: fmt(reused) })
      : t("chart.tip", { date: shortDate(d.day), items: fmt(d.items), hits: fmt(reused) });
    let segs = "";
    const memH = (reused / nice) * plotH, newH = (fresh / nice) * plotH;
    const base = padT + plotH;
    if (reused) segs += fresh ? `<rect class="seg" x="${x}" y="${base - memH}" width="${bw}" height="${memH}" fill="var(--series-memory)"/>` : `<path class="seg" d="${roundTop(x, base - memH, bw, memH, 4)}" fill="var(--series-memory)"/>`;
    if (fresh) segs += `<path class="seg" d="${roundTop(x, base - memH - (reused ? 2 : 0) - newH, bw, newH, 4)}" fill="var(--series-new)"/>`;
    cols += `<g class="col" tabindex="0" data-tip="${esc(tip)}" aria-label="${esc(tip)}">${segs}<rect class="hit" x="${padL + i * slot}" y="${padT}" width="${slot}" height="${plotH}"/></g>`;
  });
  const ticks = [0, nice / 2, nice].map((v) => `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-text" x="${padL - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`).join("");
  const xLabels = [0, 7, 13].map((i) => `<text class="axis-text" x="${padL + i * slot + slot / 2}" y="${H - 6}" text-anchor="middle">${esc(shortDate(days[i].day))}</text>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("chart.aria"))}">${ticks}<line class="baseline" x1="${padL}" x2="${W}" y1="${padT + plotH}" y2="${padT + plotH}"/>${cols}${xLabels}</svg>
    <table class="sr-only"><caption>${esc(t("chart.caption"))}</caption><tr><th>${esc(t("chart.col_day"))}</th><th>${esc(t("chart.col_items"))}</th><th>${esc(t("chart.col_hits"))}</th></tr>${days.map((d) => `<tr><td>${esc(shortDate(d.day))}</td><td>${fmt(d.items)}</td><td>${fmt(d.hits)}</td></tr>`).join("")}</table>
    <div class="tooltip" hidden></div>`;
  const tipEl = $(".tooltip", el);
  const show = (g) => {
    const hit = $(".hit", g).getBoundingClientRect(), host = el.getBoundingClientRect();
    const segTops = $$(".seg", g).map((s) => s.getBoundingClientRect().top);
    const top = segTops.length ? Math.min(...segTops) : hit.bottom;
    tipEl.textContent = g.dataset.tip;
    tipEl.style.left = `${Math.min(Math.max(hit.left - host.left + hit.width / 2, 110), host.width - 110)}px`;
    tipEl.style.top = `${top - host.top - 8}px`;
    tipEl.hidden = false;
  };
  $$(".col", el).forEach((g) => {
    g.addEventListener("mouseenter", () => show(g));
    g.addEventListener("focus", () => show(g));
    g.addEventListener("mouseleave", () => (tipEl.hidden = true));
    g.addEventListener("blur", () => (tipEl.hidden = true));
  });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (btn) copyText($("#" + btn.dataset.copy).textContent);
});

$("#copy-invite").addEventListener("click", () => {
  const o = state.overview;
  if (!o) return;
  const text = t("invite.text", { url: o.endpoints.openai, key: o.team_key }) + "\n\n" + t("invite.member", { link: o.suggest_url });
  copyText(text, t("toast.invite_copied"));
});

/* ---------------- glossary sets (shared helpers) ---------------- */
function setName(id) {
  const set = state.sets.find((x) => x.id === id);
  if (!set) return "?";
  return set.name || t("sets.general");  // the built-in set has no stored name, so it follows the interface language
}
function setOptions(selected) {
  return state.sets.map((x) => `<option value="${x.id}"${x.id === selected ? " selected" : ""}>${esc(setName(x.id))}</option>`).join("");
}
function multiSet() { return state.sets.length > 1; }

async function loadSets() {
  const { sets, links } = await api("/api/sets");
  state.sets = sets;
  state.links = links;
  const filter = $("#glossary-set-filter");
  const keep = Number(filter.value || 0);
  filter.innerHTML = `<option value="0">${esc(t("sets.all"))}</option>` + state.sets.map((x) => `<option value="${x.id}"${x.id === keep ? " selected" : ""}>${esc(setName(x.id))}</option>`).join("");
  filter.hidden = !multiSet();
  $$(".set-select").forEach((sel) => { const v = Number(sel.value || keep || 1); sel.innerHTML = setOptions(state.sets.some((x) => x.id === v) ? v : 1); });
  $(".set-pick").hidden = !multiSet();
}

/* ---------------- glossary ---------------- */
async function loadGlossaryView() {
  await loadSets();
  await Promise.all([loadGlossary(), refreshProposals(), loadHistory()]);
}

async function loadGlossary() {
  const q = $("#glossary-search").value.trim();
  const setId = Number($("#glossary-set-filter").value || 0);
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (setId) params.set("set_id", setId);
  const { terms } = await api("/api/glossary" + (params.toString() ? `?${params}` : ""));
  state.glossary = terms;
  renderGlossary(Boolean(q || setId));
}

$("#glossary-set-filter").addEventListener("change", () => {
  // New terms go into the set being looked at.
  const v = Number($("#glossary-set-filter").value || 0);
  if (v) $("#add-term select[name=set_id]").value = v;
  loadGlossary();
});

/** After any glossary change: the list, and the history that now has a new entry. */
function glossaryChanged() {
  return Promise.all([loadGlossary(), loadHistory()]);
}

function termPills(x) {
  const pills = [];
  if (!x.enabled) pills.push(`<span class="pill pill-off" title="${esc(t("glossary.off_title"))}">${esc(t("glossary.off_pill"))}</span>`);
  if (x.case_sensitive) pills.push(`<span class="pill" title="${esc(t("glossary.case_title"))}">Aa</span>`);
  if (x.conditional) pills.push(`<span class="pill pill-cond" title="${esc(t("glossary.cond_title"))}">${esc(t("glossary.cond_pill"))}</span>`);
  if (x.warn?.length) pills.push(`<span class="pill pill-warn" title="${esc(x.warn.map((w) => t("warn." + w)).join(" "))}">${esc(t("glossary.warn_pill"))}</span>`);
  return pills.length ? " " + pills.join(" ") : "";
}

/** `filtered`: a search or a set filter is active, so an empty list means "nothing matches". */
function renderGlossary(filtered) {
  const terms = state.glossary;
  const q = $("#glossary-search").value.trim();
  $("#glossary-count").textContent = filtered ? t("glossary.count_matching", { n: fmt(terms.length) }) : tn("glossary.count", terms.length);
  if (!terms.length) {
    $("#glossary-list").innerHTML = filtered
      ? `<div class="empty"><h3>${esc(q ? t("glossary.no_match_title", { q }) : t("sets.empty_set"))}</h3><p>${esc(t("glossary.no_match_body"))}</p></div>`
      : `<div class="empty"><h3>${esc(t("glossary.empty_title"))}</h3><p>${esc(t("glossary.empty_body"))}</p><p style="margin-top:16px"><button class="btn" type="button" id="add-examples">${esc(t("glossary.add_examples"))}</button></p></div>`;
    $("#add-examples")?.addEventListener("click", async () => {
      const examples = [
        { source: "census date", target: "退课截止日", note: t("example.census_note") },
        { source: "special consideration", target: "特殊考虑申请", note: t("example.special_note") },
        { source: "tutorial", target: "辅导课", note: "" },
      ];
      await api("/api/glossary/bulk", { method: "POST", body: { terms: examples.map((x) => ({ ...x, target_lang: "zh-CN" })) } });
      toast(t("glossary.examples_added"));
      glossaryChanged();
    });
    return;
  }
  const sets = multiSet();
  $("#glossary-list").innerHTML = `<table class="glossary-table${sets ? " with-sets" : ""}"><thead><tr><th class="switch-col"><span class="sr-only">${esc(t("glossary.col_on"))}</span></th><th>${esc(t("glossary.col_term"))}</th><th>${esc(t("glossary.col_translation"))}</th><th>${esc(t("glossary.col_language"))}</th><th>${esc(t("glossary.col_note"))}</th>${sets ? `<th>${esc(t("sets.label"))}</th>` : ""}<th class="num">${esc(t("glossary.col_used"))}</th><th><span class="sr-only">${esc(t("glossary.col_actions"))}</span></th></tr></thead><tbody>${
    terms.map((x) => `<tr data-id="${x.id}"${x.enabled ? "" : ' class="off"'}>
      <td class="switch-col"><label class="switch" title="${esc(t(x.enabled ? "glossary.turn_off" : "glossary.turn_on"))}"><input type="checkbox" data-toggle="${x.id}"${x.enabled ? " checked" : ""} aria-label="${esc(t(x.enabled ? "glossary.turn_off" : "glossary.turn_on"))}: ${esc(x.source)}"><span></span></label></td>
      <td class="src"><mark class="term${x.conditional ? " cond" : ""}">${esc(x.source)}</mark>${termPills(x)}</td>
      <td class="tgt">${esc(x.target)}</td>
      <td class="muted">${esc(langName(x.target_lang))}</td>
      <td class="muted small">${esc(x.note)}</td>
      ${sets ? `<td class="small"><span class="pill pill-set">${esc(setName(x.set_id))}</span></td>` : ""}
      <td class="num muted">${fmt(x.hit_count)}</td>
      <td class="actions"><button class="btn-quiet btn" type="button" data-edit="${x.id}">${esc(t("glossary.edit"))}</button><button class="btn-quiet btn" type="button" data-del="${x.id}">${esc(t("glossary.delete"))}</button></td>
    </tr>`).join("")
  }</tbody></table>`;
}

$("#glossary-list").addEventListener("change", async (e) => {
  const box = e.target.closest("[data-toggle]");
  if (!box) return;
  const x = state.glossary.find((g) => g.id == box.dataset.toggle);
  try {
    await api(`/api/glossary/${x.id}/enabled`, { method: "POST", body: { enabled: box.checked } });
    toast(t(box.checked ? "glossary.turned_on" : "glossary.turned_off", { term: x.source }));
    glossaryChanged();
  } catch (err) { box.checked = !box.checked; toast(err.message, true); }
});

$("#glossary-list").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  const edit = e.target.closest("[data-edit]");
  const save = e.target.closest("[data-save]");
  const cancel = e.target.closest("[data-cancel]");
  try {
    if (del) {
      const x = state.glossary.find((g) => g.id == del.dataset.del);
      if (!confirm(t("glossary.confirm_delete", { term: x.source }))) return;
      await api(`/api/glossary/${x.id}`, { method: "DELETE" });
      toast(t("glossary.deleted"));
      glossaryChanged();
    } else if (edit) {
      const x = state.glossary.find((g) => g.id == edit.dataset.edit);
      const tr = edit.closest("tr");
      tr.classList.add("editing");
      tr.innerHTML = `<td class="switch-col"></td><td><input type="text" class="lang-text" name="source" value="${esc(x.source)}" aria-label="${esc(t("glossary.col_term"))}"></td>
        <td><input type="text" class="lang-text" name="target" value="${esc(x.target)}" aria-label="${esc(t("glossary.col_translation"))}"></td>
        <td><select name="target_lang" aria-label="${esc(t("glossary.col_language"))}">${langOptions(x.target_lang)}</select></td>
        <td><input type="text" name="note" value="${esc(x.note)}" aria-label="${esc(t("glossary.col_note"))}" placeholder="${esc(t("glossary.note_ph"))}">
          <label class="check small" style="margin-top:6px"><input type="checkbox" name="case_sensitive"${x.case_sensitive ? " checked" : ""}> ${esc(t("glossary.case_label"))}</label>
          <label class="check small" style="margin-top:4px"><input type="checkbox" name="conditional"${x.conditional ? " checked" : ""}> ${esc(t("glossary.cond_label"))}</label></td>
        ${multiSet() ? `<td><select name="set_id" aria-label="${esc(t("sets.label"))}">${setOptions(x.set_id)}</select></td>` : ""}
        <td></td>
        <td class="actions"><button class="btn btn-primary" type="button" data-save="${x.id}">${esc(t("glossary.save"))}</button> <button class="btn-quiet btn" type="button" data-cancel>${esc(t("glossary.cancel"))}</button></td>`;
      $("input[name=target]", tr).focus();
    } else if (save) {
      const tr = save.closest("tr");
      const body = {
        source: $("[name=source]", tr).value, target: $("[name=target]", tr).value,
        target_lang: $("[name=target_lang]", tr).value, note: $("[name=note]", tr).value,
        case_sensitive: $("[name=case_sensitive]", tr).checked, conditional: $("[name=conditional]", tr).checked,
        set_id: $("[name=set_id]", tr)?.value || state.glossary.find((g) => g.id == save.dataset.save).set_id,
      };
      if (await saveWithCheck(`/api/glossary/${save.dataset.save}`, "PUT", body)) {
        toast(t("glossary.saved"));
        glossaryChanged();
      }
    } else if (cancel) {
      renderGlossary(true);
    }
  } catch (err) { toast(err.message, true); }
});

$("#add-term").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const body = {
    source: f.source.value, target: f.target.value, target_lang: f.target_lang.value, note: f.note.value,
    case_sensitive: f.case_sensitive.checked, conditional: f.conditional.checked, set_id: f.set_id.value || 1,
  };
  try {
    if (!(await saveWithCheck("/api/glossary", "POST", body))) return;
    toast(t("glossary.added", { term: body.source.trim() }));
    f.source.value = ""; f.target.value = ""; f.note.value = "";
    f.case_sensitive.checked = false; f.conditional.checked = false;
    f.source.focus();
    glossaryChanged();
  } catch (err) { toast(err.message, true); }
});

let searchTimer;
$("#glossary-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadGlossary, 200); });

$("#import-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const res = await api("/api/glossary/import", { method: "POST", body: { csv: await file.text() } });
    toast([tn("glossary.imported", res.added), res.skipped ? tn("glossary.skipped", res.skipped) : ""].join(" ").trim());
    glossaryChanged();
  } catch (err) { toast(err.message, true); }
  e.target.value = "";
});

/* ---------- proposals from teammates ---------- */
async function refreshProposals() {
  const { proposals } = await api("/api/proposals");
  state.proposals = proposals;
  setPendingBadge(proposals.length);
  if (state.view === "glossary") renderProposals();
}

function renderProposals() {
  const list = state.proposals;
  $("#proposals").hidden = !list.length;
  if (!list.length) return;
  $("#proposals-title").textContent = tn("proposal.title", list.length);
  $("#proposal-list").innerHTML = list.map((p) => {
    const who = p.name ? t("proposal.from", { name: p.name }) : t("proposal.from_anon");
    const status = p.current
      ? t("proposal.change", { current: p.current.target })
      : t("proposal.new");
    const warn = p.warn?.length ? ` <span class="pill pill-warn" title="${esc(p.warn.map((w) => t("warn." + w)).join(" "))}">${esc(t("glossary.warn_pill"))}</span>` : "";
    const reason = p.note ? `<p class="proposal-reason"><span class="muted small">${esc(t("proposal.reason"))}</span> ${esc(p.note)}</p>` : "";
    const where = multiSet() ? ` · ${esc(t("sets.in_set", { set: setName(p.set_id) }))}` : "";
    if (p.kind === "remove") {
      const current = p.current
        ? `<span class="term-pair"><mark class="term">${esc(p.current.source)}</mark><span class="arrow">→</span>${esc(p.current.target)}</span>`
        : `<mark class="term">${esc(p.source)}</mark> <span class="muted small">${esc(t("proposal.remove_gone"))}</span>`;
      return `<li data-pid="${p.id}" class="proposal-remove">
        <div class="proposal-main">
          <div class="proposal-head"><span class="pill pill-remove">${esc(t("proposal.remove_pill"))}</span>${current}${warn}</div>
          <p class="small muted">${esc(langName(p.target_lang))}${where} · ${esc(who)} · ${esc(timeAgo(p.ts))}</p>
          ${reason}
        </div>
        <div class="proposal-actions">
          <button class="btn btn-danger" type="button" data-approve="${p.id}">${esc(t("proposal.approve_remove"))}</button>
          <button class="btn-quiet btn" type="button" data-reject="${p.id}">${esc(t("proposal.reject"))}</button>
        </div>
      </li>`;
    }
    const noteValue = p.current ? p.current.note : p.note;
    return `<li data-pid="${p.id}">
      <div class="proposal-main">
        <div class="term-pair"><mark class="term">${esc(p.source)}</mark>${warn}<span class="arrow" aria-label="${esc(t("terms.becomes"))}">→</span><input type="text" class="lang-text proposal-target" value="${esc(p.target)}" data-p-target aria-label="${esc(t("glossary.col_translation"))}"></div>
        <p class="small muted">${esc(status)} · ${esc(langName(p.target_lang))}${where} · ${esc(who)} · ${esc(timeAgo(p.ts))}</p>
        ${reason}
        <label class="field proposal-note"><span class="sr-only">${esc(t("proposal.note_label"))}</span><input type="text" data-p-note value="${esc(noteValue)}" maxlength="300" placeholder="${esc(t("proposal.note_label"))}"></label>
      </div>
      <div class="proposal-actions">
        <button class="btn btn-primary" type="button" data-approve="${p.id}">${esc(t("proposal.approve"))}</button>
        <button class="btn-quiet btn" type="button" data-reject="${p.id}">${esc(t("proposal.reject"))}</button>
      </div>
    </li>`;
  }).join("");
}

$("#proposal-list").addEventListener("click", async (e) => {
  const approve = e.target.closest("[data-approve]");
  const reject = e.target.closest("[data-reject]");
  if (!approve && !reject) return;
  const li = e.target.closest("li");
  const p = state.proposals.find((x) => x.id == li.dataset.pid);
  try {
    if (approve && p.kind === "remove") {
      await api(`/api/proposals/${p.id}/approve`, { method: "POST", body: {} });
      toast(t("proposal.removed", { term: p.source }));  // undoable from the change history
    } else if (approve) {
      const body = { target: $("[data-p-target]", li).value, note: $("[data-p-note]", li).value };
      if (!(await saveWithCheck(`/api/proposals/${p.id}/approve`, "POST", body))) return;
      toast(t("proposal.approved", { term: p.source }));
    } else {
      await api(`/api/proposals/${p.id}/reject`, { method: "POST" });
      toast(t("proposal.rejected", { term: p.source }));
    }
    await Promise.all([refreshProposals(), glossaryChanged()]);
  } catch (err) { toast(err.message, true); }
});

/* ---------- change history ---------- */
async function loadHistory() {
  const { groups } = await api("/api/glossary/history");
  state.history = groups;
  renderHistory();
}

function historyChanges(prev, next) {
  const parts = [];
  if (prev.source !== next.source) parts.push(`<del>${esc(prev.source)}</del> → <ins>${esc(next.source)}</ins>`);
  if (prev.target !== next.target) parts.push(`<del class="lang-text">${esc(prev.target)}</del> → <ins class="lang-text">${esc(next.target)}</ins>`);
  if (prev.target_lang !== next.target_lang) parts.push(`${esc(langName(prev.target_lang))} → ${esc(langName(next.target_lang))}`);
  if (prev.note !== next.note) parts.push(esc(next.note ? t("history.note_set", { note: next.note }) : t("history.note_cleared")));
  if (prev.case_sensitive !== next.case_sensitive) parts.push(esc(t(next.case_sensitive ? "history.case_on" : "history.case_off")));
  if (prev.conditional !== next.conditional) parts.push(esc(t(next.conditional ? "history.cond_on" : "history.cond_off")));
  if (prev.set_id !== next.set_id) parts.push(`${esc(setName(prev.set_id))} → ${esc(setName(next.set_id))}`);
  return parts.join(" · ");
}
/** True when the only change is the on/off switch. */
function onlySwitched(prev, next) {
  return prev && next && prev.enabled !== next.enabled &&
    ["source", "target", "target_lang", "note", "case_sensitive", "conditional", "set_id"].every((k) => prev[k] === next[k]);
}

function historyLine(e) {
  const term = (e.next || e.prev).source;
  const pair = (s) => `<span class="term-pair"><mark class="term">${esc(s.source)}</mark><span class="arrow">→</span>${esc(s.target)}</span>`;
  if (e.action === "add") return [t("history.added", { term }), pair(e.next)];
  if (e.action === "delete") return [t("history.deleted", { term }), pair(e.prev)];
  if (onlySwitched(e.prev, e.next)) return [t(e.next.enabled ? "history.turned_on" : "history.turned_off", { term }), pair(e.next)];
  if (e.action === "edit") return [t("history.edited", { term }), historyChanges(e.prev, e.next)];
  // undo: describe what it restored
  if (!e.next) return [t("history.undo_removed", { term }), pair(e.prev)];
  if (!e.prev) return [t("history.undo_restored", { term }), pair(e.next)];
  return [t("history.undo_reverted", { term }), historyChanges(e.prev, e.next)];
}

function renderHistory() {
  const groups = state.history;
  const shown = state.historyAll ? groups : groups.slice(0, 8);
  $("#history-more").hidden = groups.length <= 8;
  $("#history-more").textContent = state.historyAll ? t("history.show_less") : t("history.show_more");
  if (!groups.length) {
    $("#history-list").innerHTML = `<p class="muted">${esc(t("history.empty"))}</p>`;
    return;
  }
  $("#history-list").innerHTML = `<ul class="history-list">${shown.map((g) => {
    let title, detail;
    if (g.count > 1) {
      const key = { import: "history.imported", bulk: "history.bulk", undo: "history.undid_many" }[g.kind] || "history.changed_many";
      title = tn(key, g.count);
      const names = g.entries.map((e) => (e.next || e.prev).source);
      detail = esc(names.join(", ") + (g.count > names.length ? ", …" : ""));
    } else {
      [title, detail] = historyLine(g.entries[0]);
    }
    const origin = g.entries[0].origin.startsWith("proposal:")
      ? (g.entries[0].origin.slice(9) ? t("history.via_proposal", { name: g.entries[0].origin.slice(9) }) : t("history.via_proposal_anon"))
      : "";
    const meta = [timeAgo(g.ts), origin].filter(Boolean).map(esc).join(" · ");
    const action = g.undone
      ? `<span class="pill">${esc(t("history.undone"))}</span>`
      : `<button class="btn btn-quiet" type="button" data-undo="${g.id}">${esc(t("history.undo"))}</button>`;
    return `<li${g.undone ? ' class="undone"' : ""}><div class="history-main"><p><strong>${esc(title)}</strong> <span class="muted small">${meta}</span></p>${detail ? `<p class="history-detail">${detail}</p>` : ""}</div><div>${action}</div></li>`;
  }).join("")}</ul>`;
}

$("#history-more").addEventListener("click", () => { state.historyAll = !state.historyAll; renderHistory(); });

$("#history-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-undo]");
  if (!btn) return;
  try {
    const r = await api(`/api/glossary/history/${btn.dataset.undo}/undo`, { method: "POST" });
    toast(tn("history.undone_toast", r.undone));
    glossaryChanged();
  } catch (err) { toast(err.message, true); }
});

/* ---------- AI suggestions from a sample ---------- */
$("#suggest-btn").addEventListener("click", (e) => busy(e.currentTarget, t("suggest.busy"), async () => {
  $("#suggest-status").textContent = "";
  try {
    const { terms } = await api("/api/glossary/suggest", { method: "POST", body: { text: $("#suggest-text").value } });
    state.aiSuggestions = terms;
    $("#suggest-results").hidden = !terms.length;
    $("#suggest-status").textContent = terms.length ? tn("suggest.found", terms.length) : t("suggest.none");
    $("#suggest-list").innerHTML = terms.map((x, i) => `<li><label><input type="checkbox" data-sugg="${i}"${x.exists ? "" : " checked"}>
      <span class="term-pair"><mark class="term">${esc(x.source)}</mark><span class="arrow">→</span><input type="text" class="lang-text" value="${esc(x.target)}" data-sugg-target="${i}" style="width:auto;display:inline-block;padding:4px 8px" aria-label="${esc(t("glossary.col_translation"))}"></span>
      <span class="muted small">${x.exists ? esc(t("suggest.exists")) : esc(x.note)}</span></label></li>`).join("");
  } catch (err) { toast(err.message, true); }
}));

$("#suggest-add").addEventListener("click", async () => {
  const picked = $$("[data-sugg]").filter((c) => c.checked).map((c) => {
    const x = state.aiSuggestions[c.dataset.sugg];
    return { ...x, target: $(`[data-sugg-target="${c.dataset.sugg}"]`).value };
  });
  if (!picked.length) return toast(t("suggest.tick_one"), true);
  try {
    const res = await api("/api/glossary/bulk", { method: "POST", body: { terms: picked, set_id: $("#add-term select[name=set_id]").value || 1 } });
    toast(tn("suggest.added", res.added));
    $("#suggest-results").hidden = true;
    $("#suggest-status").textContent = "";
    glossaryChanged();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- glossary sets and links ---------------- */
async function loadSetsView() {
  await loadSets();
  renderSets();
  renderLinks();
}

function renderSets() {
  const movable = state.sets.filter((x) => x.id !== 1);
  $("#set-list").innerHTML = state.sets.map((x) => {
    const i = movable.findIndex((m) => m.id === x.id);
    const general = x.id === 1;
    return `<li data-set="${x.id}">
      <div class="set-main"><strong>${esc(setName(x.id))}</strong> <span class="muted small">${esc(tn("glossary.count", x.terms))}</span>${general ? ` <span class="pill">${esc(t("sets.general_pill"))}</span>` : ""}</div>
      <div class="set-actions">
        ${general ? "" : `<button class="btn btn-quiet" type="button" data-move="-1" ${i <= 0 ? "disabled" : ""} aria-label="${esc(t("sets.up"))}" title="${esc(t("sets.up"))}">↑</button>
        <button class="btn btn-quiet" type="button" data-move="1" ${i >= movable.length - 1 ? "disabled" : ""} aria-label="${esc(t("sets.down"))}" title="${esc(t("sets.down"))}">↓</button>`}
        <a class="btn btn-quiet" href="#glossary" data-show-set="${x.id}">${esc(t("sets.show_terms"))}</a>
        <button class="btn btn-quiet" type="button" data-rename>${esc(t("sets.rename"))}</button>
        ${general ? "" : `<button class="btn btn-quiet" type="button" data-delete-set>${esc(t("glossary.delete"))}</button>`}
      </div>
    </li>`;
  }).join("");
}

function renderLinks() {
  $("#link-list").innerHTML = state.links.map((l) => `<div class="panel link-card" data-link="${l.id}">
    <div class="link-head">
      <h3>${esc(linkName(l))}</h3>
      <div class="row">
        ${l.default ? `<span class="pill">${esc(t("links.default_pill"))}</span>` : `<button class="btn btn-quiet" type="button" data-rename-link>${esc(t("sets.rename"))}</button><button class="btn btn-quiet" type="button" data-delete-link>${esc(t("glossary.delete"))}</button>`}
      </div>
    </div>
    <p class="small muted">${esc(t(l.default ? "links.default_help" : "links.uses"))}</p>
    <div class="link-sets">${state.sets.map((x) => `<label class="check"><input type="checkbox" data-link-set="${x.id}"${l.set_ids.includes(x.id) ? " checked" : ""}> ${esc(setName(x.id))}</label>`).join("")}</div>
    <div class="link-addr">
      <span class="small muted">${esc(t("links.api_address"))}</span>
      <div class="copy-field"><code id="link-api-${l.id}">${esc(l.openai)}</code><button class="btn" type="button" data-copy="link-api-${l.id}">${esc(t("connect.copy"))}</button></div>
      <span class="small muted">${esc(t("links.member_page"))}</span>
      <div class="copy-field"><code id="link-member-${l.id}">${esc(l.suggest_url)}</code><button class="btn" type="button" data-copy="link-member-${l.id}">${esc(t("connect.copy"))}</button></div>
    </div>
    <div class="row" style="margin-top:12px"><button class="btn btn-primary" type="button" data-copy-invite="${l.id}">${esc(t("links.copy_invite"))}</button></div>
  </div>`).join("");
}

$("#set-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-set]");
  if (!li) return;
  const id = Number(li.dataset.set);
  const x = state.sets.find((s) => s.id === id);
  try {
    if (e.target.closest("[data-move]")) {
      await api(`/api/sets/${id}/move`, { method: "POST", body: { direction: Number(e.target.closest("[data-move]").dataset.move) } });
    } else if (e.target.closest("[data-rename]")) {
      const name = prompt(t("sets.rename_prompt"), setName(id));
      if (!name || !name.trim()) return;
      await api(`/api/sets/${id}`, { method: "PUT", body: { name } });
    } else if (e.target.closest("[data-delete-set]")) {
      if (!confirm(t("sets.confirm_delete", { set: setName(id) }))) return;
      await api(`/api/sets/${id}`, { method: "DELETE" });
      toast(t("sets.deleted", { set: x.name }));
    } else if (e.target.closest("[data-show-set]")) {
      $("#glossary-set-filter").value = id;  // the glossary view opens filtered to this set
      return;
    } else return;
    loadSetsView();
  } catch (err) { toast(err.message, true); }
});

$("#add-set").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/sets", { method: "POST", body: { name: f.name.value } });
    toast(t("sets.added", { set: f.name.value.trim() }));
    f.reset();
    loadSetsView();
  } catch (err) { toast(err.message, true); }
});

$("#link-list").addEventListener("change", async (e) => {
  const box = e.target.closest("[data-link-set]");
  if (!box) return;
  const card = box.closest("[data-link]");
  const ids = $$("[data-link-set]", card).filter((c) => c.checked).map((c) => Number(c.dataset.linkSet));
  try {
    await api(`/api/links/${card.dataset.link}`, { method: "PUT", body: { set_ids: ids } });
    toast(t("links.saved"));
    await loadSets();
    renderLinks();
  } catch (err) { box.checked = !box.checked; toast(err.message, true); }
});

$("#link-list").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-link]");
  if (!card) return;
  const link = state.links.find((l) => l.id == card.dataset.link);
  try {
    if (e.target.closest("[data-copy-invite]")) {
      const key = state.overview?.team_key || (await api("/api/overview")).team_key;
      copyText(t("invite.text", { url: link.openai, key }) + "\n\n" + t("invite.member", { link: link.suggest_url }), t("toast.invite_copied"));
      return;
    }
    if (e.target.closest("[data-rename-link]")) {
      const name = prompt(t("links.rename_prompt"), link.name);
      if (!name || !name.trim()) return;
      await api(`/api/links/${link.id}`, { method: "PUT", body: { name } });
    } else if (e.target.closest("[data-delete-link]")) {
      if (!confirm(t("links.confirm_delete", { link: link.name }))) return;
      await api(`/api/links/${link.id}`, { method: "DELETE" });
    } else return;
    await loadSets();
    renderLinks();
  } catch (err) { toast(err.message, true); }
});

$("#add-link").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/links", { method: "POST", body: { name: f.name.value, set_ids: [1] } });
    toast(t("links.added", { link: f.name.value.trim() }));
    f.reset();
    await loadSets();
    renderLinks();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- try it ---------------- */
let lastTry = null;  // kept so a language switch can redraw the result in the new language

function linkName(link) {
  return link.default ? t("links.default") : link.name;
}

async function loadTryView() {
  await loadSets();
  const sel = $("#try-link");
  const keep = Number(sel.value || 1);
  sel.innerHTML = state.links.map((l) => `<option value="${l.id}"${l.id === keep ? " selected" : ""}>${esc(t("try.through", { link: linkName(l) }))}</option>`).join("");
  sel.closest("label").hidden = state.links.length < 2;
  renderTry();
}

function renderTry() {
  if (!lastTry) {
    $("#try-output").innerHTML = `<p class="result-empty">${esc(t("try.empty"))}</p>`;
    return;
  }
  if (lastTry.error) {
    $("#try-output").innerHTML = `<p class="result-empty status-err">${esc(errorText(lastTry.error))}</p>${rawDetails(lastTry.error.raw)}`;
    return;
  }
  const r = lastTry.result;
  const items = r.items.filter((i) => !i.skipped);
  const reused = items.filter((i) => i.cached || i.direct).length;
  const failed = items.filter((i) => i.error_code).length;
  const summary = [
    tn("try.done", items.length, { time: seconds(r.ms) }),
    t("try.calls", { n: fmt(r.llm_calls) }),
    reused ? t("try.reused", { n: fmt(reused) }) : t("try.no_reuse"),
    failed ? tn("try.failed", failed) + " " + errorText({ code: r.error_code }) : "",
  ].filter(Boolean).join(" ");
  $("#try-output").innerHTML = `<p class="result-summary">${esc(summary)}</p>` + (failed ? rawDetails(r.error_raw) : "") + items.map((i) => {
    const must = i.terms.filter((x) => !x.conditional);
    const cond = i.terms.length - must.length;
    const applied = must.filter((x) => x.applied).length;
    const meta = [
      i.error_code ? `<span class="pill pill-failed">${esc(t("try.badge_failed"))}</span>` : "",
      i.cached ? `<span class="pill pill-memory">${esc(t("try.badge_memory"))}</span>` : "",
      i.direct ? `<span class="pill pill-memory">${esc(t("try.badge_glossary"))}</span>` : "",
      must.length && !i.direct ? `<span class="pill">${esc(must.length === 1 && I18N[LANG]["try.badge_terms_one"] ? t("try.badge_terms_one", { applied: fmt(applied), total: fmt(must.length) }) : t("try.badge_terms", { applied: fmt(applied), total: fmt(must.length) }))}</span>` : "",
      cond ? `<span class="pill pill-cond">${esc(tn("try.badge_cond", cond))}</span>` : "",
    ].join("");
    const cls = i.error_code ? " failed" : (i.cached || i.direct ? " cached" : "");
    return `<div class="para${cls}"><p class="src">${highlight(i.source, i.terms, "source")}</p>${i.error_code ? "" : `<p class="tgt">${highlight(i.text, i.terms.filter((x) => x.applied), "target")}</p>`}${meta ? `<div class="meta">${meta}</div>` : ""}</div>`;
  }).join("");
}

$("#try-sample").addEventListener("click", () => { $("#try-text").value = SAMPLE_TEXT; $("#try-text").focus(); });

$("#try-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const btn = $("button[type=submit]", f);
  busy(btn, t("try.busy"), async () => {
    try {
      lastTry = { result: await api("/api/translate", { method: "POST", body: { text: f.text.value, target_lang: f.target_lang.value, link_id: f.link_id.value || 1 } }) };
    } catch (err) {
      lastTry = { error: err };
    }
    renderTry();
  });
});

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  LANG_FALLBACK = s.languages;
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
  $("#provider").innerHTML = Object.entries(s.presets).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("") + `<option value="custom">${esc(t("settings.provider_custom"))}</option>`;
  f.provider.value = guessProvider(s);
  f.llm_base_url.value = s.llm_base_url;
  f.llm_model.value = s.llm_model;
  f.llm_api_key.value = "";
  f.llm_api_key.placeholder = s.has_api_key ? t("settings.key_saved_ph", { masked: s.llm_api_key_masked }) : t("settings.key_ph");
  $("#api-key-help").textContent = s.has_api_key ? t("settings.key_keep") : t("settings.key_required");
  f.max_rpm.value = s.max_rpm;
  f.default_target_lang.innerHTML = langOptions(s.default_target_lang);
  f.style_guide.value = s.style_guide;
  f.temperature.value = s.temperature;
  f.price_per_m_tokens.value = s.price_per_m_tokens;
  f.team_name.value = s.team_name;
  $("#test-result").textContent = "";
  const st = await api("/api/stats");
  $("#memory-size").textContent = tn("settings.memory_desc", st.cache_entries);
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
  return Object.fromEntries(["provider", "llm_base_url", "llm_model", "llm_api_key", "max_rpm", "default_target_lang", "style_guide", "temperature", "price_per_m_tokens", "team_name"].map((k) => [k, f[k].value]));
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: settingsBody() });
    $("#team-name").textContent = $("#settings-form").team_name.value;
    toast(t("settings.saved"));
    renderSettings();
  } catch (err) { toast(err.message, true); }
});

$("#test-btn").addEventListener("click", (e) => busy(e.currentTarget, t("settings.testing"), async () => {
  const out = $("#test-result");
  out.className = "test-result muted";
  out.textContent = "";
  try {
    await api("/api/settings", { method: "PUT", body: settingsBody() });
    const r = await api("/api/settings/test", { method: "POST" });
    const msg = t("settings.test_ok", { model: r.model, time: seconds(r.ms), reply: r.reply });
    await renderSettings(); // shows the newly saved key as masked
    out.className = "test-result status-ok";
    out.textContent = msg;
  } catch (err) {
    out.className = "test-result status-err";
    out.innerHTML = esc(err.message) + rawDetails(err.raw);
  }
}));

$("#rotate-btn").addEventListener("click", async () => {
  if (!confirm(t("settings.rotate_confirm"))) return;
  try {
    await api("/api/team-key/rotate", { method: "POST" });
    toast(t("settings.rotated"));
  } catch (err) { toast(err.message, true); }
});

$("#clear-btn").addEventListener("click", async () => {
  if (!confirm(t("settings.clear_confirm"))) return;
  try {
    const r = await api("/api/cache/clear", { method: "POST" });
    toast(tn("settings.cleared", r.cleared));
    renderSettings();
  } catch (err) { toast(err.message, true); }
});

boot().catch((err) => toast(err.message, true));
