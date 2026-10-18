"use strict";
/* Shared by the dashboard (app.js) and the teammate page (member.js):
   DOM helpers, interface translations and locale-aware formatting. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const UI_LANGS = ["en", "zh-CN", "zh-TW", "ja", "ko", "vi"];
const LANG_STORE = "glossa.lang";
let I18N = { _meta: { names: { en: "English" } }, en: {} };
let LANG = "en";
let LANG_FALLBACK = {};  // target-language names from the server, used when Intl.DisplayNames is missing

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
function hasKey(key) {
  return Boolean(I18N[LANG]?.[key] || I18N.en[key]);
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
  return LANG_FALLBACK[code] || code;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Wrap glossary terms in <mark>, escaping everything else. Source side also matches plural endings. */
function highlight(text, terms, side) {
  const list = (terms || [])
    .map((term) => ({ word: side === "source" ? term.source : term.target, applied: term.applied !== false, conditional: Boolean(term.conditional) }))
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
    const missed = side === "source" && hit && !hit.applied && !hit.conditional;
    const cls = "term" + (missed ? " missed" : "") + (hit && hit.conditional ? " cond" : "");
    const title = missed ? t("highlight.missed") : (hit && hit.conditional ? t("highlight.cond") : "");
    out += `<mark class="${cls}"${title ? ` title="${esc(title)}"` : ""}>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + esc(text.slice(last));
}

function applyStatic() {
  document.documentElement.lang = LANG;
  $$("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  $$("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml)));
  $$("[data-i18n-ph]").forEach((el) => (el.placeholder = t(el.dataset.i18nPh)));
  $$("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
  $$("[data-i18n-title]").forEach((el) => el.setAttribute("title", t(el.dataset.i18nTitle)));
  $$(".lang-picker").forEach((sel) => {
    sel.innerHTML = UI_LANGS.map((l) => `<option value="${l}" lang="${l}"${l === LANG ? " selected" : ""}>${esc(I18N._meta.names[l] || l)}</option>`).join("");
  });
}

async function loadI18n() {
  try {
    const res = await fetch("/static/i18n.json?v=0.3.1");
    I18N = await res.json();
  } catch { /* keep the built-in English fallback */ }
  const saved = storedLang();
  LANG = UI_LANGS.includes(saved) ? saved : (I18N._meta?.default || "en");
  applyStatic();
}

/** Each page defines onLangChange() to redraw what it rendered itself. */
function setLang(lang) {
  if (!UI_LANGS.includes(lang) || lang === LANG) return;
  LANG = lang;
  storeLang(lang);
  applyStatic();
  if (typeof onLangChange === "function") onLangChange();
}
document.addEventListener("change", (e) => {
  if (e.target.matches(".lang-picker")) setLang(e.target.value);
});

/** A localized message for an API error; the provider's raw text is kept separately as `raw`. */
function errorText(err) {
  if (err.code && hasKey(`err.${err.code}`)) return t(`err.${err.code}`, err.params || {});
  if (err.status) return t("err.generic", { status: err.status });
  return t("err.network");
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
