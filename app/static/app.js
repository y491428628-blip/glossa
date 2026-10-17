"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { settings: null, languages: {}, presets: {}, overview: null, glossary: [], suggestions: [], view: null };

const SAMPLE_TEXT = [
  "The census date for Term 3 is Sunday of Week 4. If you drop a course after the census date, you will still be charged fees.",
  "Your first tutorial is in Week 2. Check your class timetable in myUNSW before then.",
  "Special consideration applications must be lodged within three working days of the assessment.",
  "Members can collect their society hoodie at the welcome night in the Roundhouse.",
].join("\n");

/* ---------------- i18n ---------------- */
const UI_LANGS = ["en", "zh-CN", "zh-TW", "ja", "ko", "vi"];
const LANG_STORE = "glossa.lang";
let I18N = { _meta: { names: { en: "English" } }, en: {} };
let LANG = "en";

function storedLang() {
  try { return localStorage.getItem(LANG_STORE); } catch { return null; }
}
function storeLang(lang) {
  try { localStorage.setItem(LANG_STORE, lang); } catch { /* private mode: keep for this visit only */ }
}

/** Translate a key; {name} placeholders are filled from params. */
function t(key, params = {}) {
  const table = I18N[LANG] || {};
  const s = table[key] ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? params[k] : `{${k}}`));
}
/** Like t(), but picks the "_one" variant when count is 1 and the language has one. */
function tn(key, count, params = {}) {
  const table = I18N[LANG] || {};
  const k = count === 1 && table[key + "_one"] ? key + "_one" : key;
  return t(k, { n: fmt(count), ...params });
}

const fmt = (n) => new Intl.NumberFormat(LANG).format(n || 0);
const pct = (x) => new Intl.NumberFormat(LANG, { maximumFractionDigits: 0 }).format(Math.round(x * 100));
const usd = (x) => new Intl.NumberFormat(LANG, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(x);
function seconds(ms) {
  try {
    return new Intl.NumberFormat(LANG, { style: "unit", unit: "second", unitDisplay: "short", maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(ms / 1000);
  } catch { return `${(ms / 1000).toFixed(1)} s`; }
}
function shortDate(day) {
  return new Date(day + "T00:00:00").toLocaleDateString(LANG, { month: "short", day: "numeric" });
}
function timeAgo(ts) {
  const s = Math.round(ts - Date.now() / 1000);
  const rtf = new Intl.RelativeTimeFormat(LANG, { numeric: "auto" });
  if (Math.abs(s) < 60) return rtf.format(s, "second");
  if (Math.abs(s) < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (Math.abs(s) < 86400) return rtf.format(Math.round(s / 3600), "hour");
  return new Date(ts * 1000).toLocaleDateString(LANG);
}
/** Target-language names in the interface language, e.g. zh-CN shown as 简体中文 / Simplified Chinese / 中国語（簡体字）. */
function langName(code) {
  const tag = { "zh-CN": "zh-Hans", "zh-TW": "zh-Hant" }[code] || code;
  try {
    const name = new Intl.DisplayNames([LANG], { type: "language" }).of(tag);
    if (name && name !== tag) return name;
  } catch { /* older browser */ }
  return state.languages[code] || code;
}

function applyStatic() {
  document.documentElement.lang = LANG;
  $$("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  $$("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml)));
  $$("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  $$("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
  $$(".lang-picker").forEach((sel) => {
    sel.innerHTML = UI_LANGS.map((l) => `<option value="${l}" lang="${l}"${l === LANG ? " selected" : ""}>${esc(I18N._meta.names[l] || l)}</option>`).join("");
  });
}

async function loadI18n() {
  try {
    const res = await fetch("/static/i18n.json?v=0.2.0");
    I18N = await res.json();
  } catch { /* keep the built-in English fallback */ }
  const saved = storedLang();
  LANG = UI_LANGS.includes(saved) ? saved : (I18N._meta?.default || "en");
  applyStatic();
}

function setLang(lang) {
  if (!UI_LANGS.includes(lang) || lang === LANG) return;
  LANG = lang;
  storeLang(lang);
  applyStatic();
  fillLangSelects();
  if (!$("#app").hidden) route(true);
}
document.addEventListener("change", (e) => {
  if (e.target.matches(".lang-picker")) setLang(e.target.value);
});

/* ---------------- helpers ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** A localized message for an API error; the provider's raw text is kept separately as `raw`. */
function errorText(err) {
  if (err.code && (I18N[LANG]?.[`err.${err.code}`] || I18N.en[`err.${err.code}`])) return t(`err.${err.code}`, err.params || {});
  if (err.status) return t("err.generic", { status: err.status });
  return t("err.network");
}

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

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 2400);
}

async function busy(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  if (label) btn.textContent = label;
  try { return await fn(); } finally { btn.disabled = false; btn.textContent = original; }
}

async function copyText(text, msg) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast(msg || t("toast.copied"));
}

/** Wrap glossary terms in <mark>, escaping everything else. Source side also matches plural endings. */
function highlight(text, terms, side) {
  const list = (terms || [])
    .map((term) => ({ word: side === "source" ? term.source : term.target, applied: term.applied !== false }))
    .filter((x) => x.word)
    .sort((a, b) => b.word.length - a.word.length);
  if (!list.length) return esc(text);
  const toPattern = (w) => {
    const escaped = w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/[\s\-]+/g, "[\\s\\-]+");
    return side === "source" && /[A-Za-z]$/.test(w) ? escaped + "(?:s|es|'s)?" : escaped;
  };
  const pattern = new RegExp(list.map((x) => toPattern(x.word)).join("|"), "gi");
  let out = "", last = 0, m;
  while ((m = pattern.exec(text))) {
    if (!m[0]) { pattern.lastIndex++; continue; }
    const norm = (s) => s.toLowerCase().replace(/[\s\-]+/g, " ");
    const hit = list.find((x) => norm(m[0]).startsWith(norm(x.word)));
    out += esc(text.slice(last, m.index));
    const missed = side === "source" && hit && !hit.applied;
    out += `<mark class="term${missed ? " missed" : ""}"${missed ? ` title="${esc(t("highlight.missed"))}"` : ""}>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + esc(text.slice(last));
}

function langOptions(selected) {
  return Object.keys(state.languages)
    .map((code) => `<option value="${esc(code)}"${code === selected ? " selected" : ""}>${esc(langName(code))}</option>`)
    .join("");
}
function fillLangSelects() {
  if (!Object.keys(state.languages).length) return;
  const def = state.settings?.default_target_lang || "zh-CN";
  $$(".lang-select").forEach((sel) => { const keep = sel.value; sel.innerHTML = langOptions(keep || def); });
}

function rawDetails(raw) {
  return raw ? `<details class="err-details"><summary>${esc(t("err.details"))}</summary><pre>${esc(raw)}</pre></details>` : "";
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
const VIEWS = { overview: loadOverview, glossary: loadGlossary, try: renderTry, settings: renderSettings };
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

/* ---------------- overview ---------------- */
async function loadOverview() {
  const o = await api("/api/overview");
  state.overview = o;
  const s = o.stats;
  $("#setup-notice").hidden = o.configured;
  $("#ep-openai").textContent = o.endpoints.openai;
  $("#ep-custom").textContent = o.endpoints.custom;
  $("#team-key").textContent = o.team_key;

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
  copyText(t("invite.text", { url: o.endpoints.openai, key: o.team_key }), t("toast.invite_copied"));
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
  $("#glossary-count").textContent = q ? t("glossary.count_matching", { n: fmt(terms.length) }) : tn("glossary.count", terms.length);
  if (!terms.length) {
    $("#glossary-list").innerHTML = q
      ? `<div class="empty"><h3>${esc(t("glossary.no_match_title", { q }))}</h3><p>${esc(t("glossary.no_match_body"))}</p></div>`
      : `<div class="empty"><h3>${esc(t("glossary.empty_title"))}</h3><p>${esc(t("glossary.empty_body"))}</p><p style="margin-top:16px"><button class="btn" type="button" id="add-examples">${esc(t("glossary.add_examples"))}</button></p></div>`;
    $("#add-examples")?.addEventListener("click", async () => {
      const examples = [
        { source: "census date", target: "退课截止日", note: t("example.census_note") },
        { source: "special consideration", target: "特殊考虑申请", note: t("example.special_note") },
        { source: "tutorial", target: "辅导课", note: "" },
      ];
      await api("/api/glossary/bulk", { method: "POST", body: { terms: examples.map((x) => ({ ...x, target_lang: "zh-CN" })) } });
      toast(t("glossary.examples_added"));
      loadGlossary();
    });
    return;
  }
  $("#glossary-list").innerHTML = `<table class="glossary-table"><thead><tr><th>${esc(t("glossary.col_term"))}</th><th>${esc(t("glossary.col_translation"))}</th><th>${esc(t("glossary.col_language"))}</th><th>${esc(t("glossary.col_note"))}</th><th class="num">${esc(t("glossary.col_used"))}</th><th><span class="sr-only">${esc(t("glossary.col_actions"))}</span></th></tr></thead><tbody>${
    terms.map((x) => `<tr data-id="${x.id}">
      <td class="src"><mark class="term">${esc(x.source)}</mark>${x.case_sensitive ? ` <span class="pill" title="${esc(t("glossary.case_title"))}">Aa</span>` : ""}</td>
      <td class="tgt">${esc(x.target)}</td>
      <td class="muted">${esc(langName(x.target_lang))}</td>
      <td class="muted small">${esc(x.note)}</td>
      <td class="num muted">${fmt(x.hit_count)}</td>
      <td class="actions"><button class="btn-quiet btn" type="button" data-edit="${x.id}">${esc(t("glossary.edit"))}</button><button class="btn-quiet btn" type="button" data-del="${x.id}">${esc(t("glossary.delete"))}</button></td>
    </tr>`).join("")
  }</tbody></table>`;
}

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
      loadGlossary();
    } else if (edit) {
      const x = state.glossary.find((g) => g.id == edit.dataset.edit);
      const tr = edit.closest("tr");
      tr.classList.add("editing");
      tr.innerHTML = `<td><input type="text" class="lang-text" name="source" value="${esc(x.source)}" aria-label="${esc(t("glossary.col_term"))}"></td>
        <td><input type="text" class="lang-text" name="target" value="${esc(x.target)}" aria-label="${esc(t("glossary.col_translation"))}"></td>
        <td><select name="target_lang" aria-label="${esc(t("glossary.col_language"))}">${langOptions(x.target_lang)}</select></td>
        <td><input type="text" name="note" value="${esc(x.note)}" aria-label="${esc(t("glossary.col_note"))}"><label class="check small" style="margin-top:6px"><input type="checkbox" name="case_sensitive"${x.case_sensitive ? " checked" : ""}> ${esc(t("glossary.case_label"))}</label></td>
        <td></td>
        <td class="actions"><button class="btn btn-primary" type="button" data-save="${x.id}">${esc(t("glossary.save"))}</button> <button class="btn-quiet btn" type="button" data-cancel>${esc(t("glossary.cancel"))}</button></td>`;
      $("input[name=target]", tr).focus();
    } else if (save) {
      const tr = save.closest("tr");
      const body = {
        source: $("[name=source]", tr).value, target: $("[name=target]", tr).value,
        target_lang: $("[name=target_lang]", tr).value, note: $("[name=note]", tr).value,
        case_sensitive: $("[name=case_sensitive]", tr).checked,
      };
      await api(`/api/glossary/${save.dataset.save}`, { method: "PUT", body });
      toast(t("glossary.saved"));
      loadGlossary();
    } else if (cancel) {
      renderGlossary($("#glossary-search").value.trim());
    }
  } catch (err) { toast(err.message, true); }
});

$("#add-term").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  try {
    await api("/api/glossary", { method: "POST", body: { source: f.source.value, target: f.target.value, target_lang: f.target_lang.value, note: f.note.value } });
    toast(t("glossary.added", { term: f.source.value.trim() }));
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
    toast([tn("glossary.imported", res.added), res.skipped ? tn("glossary.skipped", res.skipped) : ""].join(" ").trim());
    loadGlossary();
  } catch (err) { toast(err.message, true); }
  e.target.value = "";
});

$("#suggest-btn").addEventListener("click", (e) => busy(e.currentTarget, t("suggest.busy"), async () => {
  $("#suggest-status").textContent = "";
  try {
    const { terms } = await api("/api/glossary/suggest", { method: "POST", body: { text: $("#suggest-text").value } });
    state.suggestions = terms;
    $("#suggest-results").hidden = !terms.length;
    $("#suggest-status").textContent = terms.length ? tn("suggest.found", terms.length) : t("suggest.none");
    $("#suggest-list").innerHTML = terms.map((x, i) => `<li><label><input type="checkbox" data-sugg="${i}"${x.exists ? "" : " checked"}>
      <span class="term-pair"><mark class="term">${esc(x.source)}</mark><span class="arrow">→</span><input type="text" class="lang-text" value="${esc(x.target)}" data-sugg-target="${i}" style="width:auto;display:inline-block;padding:4px 8px" aria-label="${esc(t("glossary.col_translation"))}"></span>
      <span class="muted small">${x.exists ? esc(t("suggest.exists")) : esc(x.note)}</span></label></li>`).join("");
  } catch (err) { toast(err.message, true); }
}));

$("#suggest-add").addEventListener("click", async () => {
  const picked = $$("[data-sugg]").filter((c) => c.checked).map((c) => {
    const x = state.suggestions[c.dataset.sugg];
    return { ...x, target: $(`[data-sugg-target="${c.dataset.sugg}"]`).value };
  });
  if (!picked.length) return toast(t("suggest.tick_one"), true);
  try {
    const res = await api("/api/glossary/bulk", { method: "POST", body: { terms: picked } });
    toast(tn("suggest.added", res.added));
    $("#suggest-results").hidden = true;
    $("#suggest-status").textContent = "";
    loadGlossary();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- try it ---------------- */
let lastTry = null;  // kept so a language switch can redraw the result in the new language

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
    const applied = i.terms.filter((x) => x.applied).length;
    const meta = [
      i.error_code ? `<span class="pill pill-failed">${esc(t("try.badge_failed"))}</span>` : "",
      i.cached ? `<span class="pill pill-memory">${esc(t("try.badge_memory"))}</span>` : "",
      i.direct ? `<span class="pill pill-memory">${esc(t("try.badge_glossary"))}</span>` : "",
      i.terms.length && !i.direct ? `<span class="pill">${esc(i.terms.length === 1 && I18N[LANG]["try.badge_terms_one"] ? t("try.badge_terms_one", { applied: fmt(applied), total: fmt(i.terms.length) }) : t("try.badge_terms", { applied: fmt(applied), total: fmt(i.terms.length) }))}</span>` : "",
    ].join("");
    const cls = i.error_code ? " failed" : (i.cached || i.direct ? " cached" : "");
    return `<div class="para${cls}"><p class="src">${highlight(i.source, i.terms, "source")}</p>${i.error_code ? "" : `<p class="tgt">${highlight(i.text, i.terms, "target")}</p>`}${meta ? `<div class="meta">${meta}</div>` : ""}</div>`;
  }).join("");
}

$("#try-sample").addEventListener("click", () => { $("#try-text").value = SAMPLE_TEXT; $("#try-text").focus(); });

$("#try-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const btn = $("button[type=submit]", f);
  busy(btn, t("try.busy"), async () => {
    try {
      lastTry = { result: await api("/api/translate", { method: "POST", body: { text: f.text.value, target_lang: f.target_lang.value } }) };
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
