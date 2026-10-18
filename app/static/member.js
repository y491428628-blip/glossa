"use strict";
/* Teammate page: browse the team glossary and suggest terms for the admin to review.
   Uses only the team key (from the link's #key=… or typed in), never the admin password. */

const KEY_STORE = "glossa.team_key";
const NAME_STORE = "glossa.name";
let teamKey = "";
let data = null;  // {team_name, default_target_lang, languages, terms}
let lastSent = null;  // {term, kind}, kept so the confirmation can be redrawn in another language
let removing = null;  // the term being suggested for removal, or null for a normal suggestion

/** The form either suggests a term (add or change) or asks for an existing term to be removed. */
function setFormMode(term) {
  removing = term;
  const f = $("#propose-form");
  $("#target-field").hidden = Boolean(term);
  $("#lang-field").hidden = Boolean(term);
  $("#remove-help").hidden = !term;
  $("#remove-cancel").hidden = !term;
  f.target.required = !term;
  f.source.readOnly = Boolean(term);
  renderFormText();
  if (term) {
    f.source.value = term.source;
    f.target_lang.value = term.target_lang;
  }
}
function renderFormText() {
  $("#form-title").textContent = removing ? t("member.remove_title", { term: removing.source }) : t("member.form_title");
  $("#propose-submit").textContent = removing ? t("member.remove_submit") : t("member.submit");
  $("#propose-status").textContent = lastSent
    ? t(lastSent.kind === "remove" ? "member.remove_sent" : "member.sent", { term: lastSent.term })
    : "";
}

function remember(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }
function recall(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function forget(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } }

/** The setup link carries the key after #, which browsers never send to servers. Take it and tidy the address bar. */
function keyFromLink() {
  const m = location.hash.match(/key=([^&]+)/);
  if (!m) return "";
  history.replaceState(null, "", location.pathname);
  return decodeURIComponent(m[1]).trim();
}

async function teamApi(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { Authorization: `Bearer ${teamKey}` } };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try { res = await fetch(path, init); } catch {
    const e = new Error(t("err.network")); e.code = "network"; throw e;
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const e = new Error("");
    e.status = res.status; e.code = body?.code; e.params = body?.params;
    e.message = errorText(e);
    throw e;
  }
  return body;
}

function showGate(message = "") {
  $("#member-app").hidden = true;
  $("#key-gate").hidden = false;
  $("#key-form .form-error").textContent = message;
  $("#key-form input[name=key]").focus();
}

async function loadTeam() {
  data = await teamApi("/api/team/glossary");
  remember(KEY_STORE, teamKey);
  LANG_FALLBACK = data.languages;
  $("#key-gate").hidden = true;
  $("#member-app").hidden = false;
  $("#team-name").textContent = data.team_name || "";
  fillSelect();
  renderList();
}

function fillSelect() {
  $$(".lang-select").forEach((sel) => {
    const keep = sel.value || data.default_target_lang;
    sel.innerHTML = Object.keys(LANG_FALLBACK)
      .map((code) => `<option value="${esc(code)}"${code === keep ? " selected" : ""}>${esc(langName(code))}</option>`).join("");
  });
}

/* ---------- try a translation ---------- */
let lastTry = null;  // {result} or {error}, kept so a language switch can redraw it

function renderTry() {
  const out = $("#member-try-output");
  if (!lastTry) {
    out.innerHTML = `<p class="result-empty">${esc(t("member.try_empty"))}</p>`;
    return;
  }
  if (lastTry.error) {
    out.innerHTML = `<p class="result-empty status-err">${esc(errorText(lastTry.error))}</p>`;
    return;
  }
  const r = lastTry.result;
  const items = r.items.filter((i) => !i.skipped);
  const failed = items.filter((i) => i.error_code).length;
  const summary = [
    tn("try.done", items.length, { time: seconds(r.ms) }),
    failed ? tn("try.failed", failed) + " " + errorText({ code: r.error_code }) : "",
  ].filter(Boolean).join(" ");
  out.innerHTML = `<p class="result-summary">${esc(summary)}</p>` + items.map((i) => {
    // Each glossary term used here can be questioned straight away.
    const chips = i.terms.map((x) => {
      const idx = data.terms.findIndex((g) => g.source === x.source);
      return `<li><span class="term-pair"><mark class="term${x.conditional ? " cond" : ""}">${esc(x.source)}</mark><span class="arrow">→</span>${esc(x.target)}</span>${idx >= 0 ? ` <button class="btn btn-quiet" type="button" data-fix="${idx}">${esc(t("member.fix"))}</button>` : ""}</li>`;
    }).join("");
    return `<div class="para${i.error_code ? " failed" : (i.cached || i.direct ? " cached" : "")}"><p class="src">${highlight(i.source, i.terms, "source")}</p>${i.error_code ? "" : `<p class="tgt">${highlight(i.text, i.terms.filter((x) => x.applied), "target")}</p>`}${chips ? `<ul class="try-terms">${chips}</ul>` : ""}</div>`;
  }).join("");
}

$("#member-try-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const btn = $("button[type=submit]", f);
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = t("try.busy");
  try {
    lastTry = { result: await teamApi("/api/team/translate", { method: "POST", body: { text: f.text.value, target_lang: f.target_lang.value } }) };
  } catch (err) {
    if (err.code === "wrong_team_key") { forget(KEY_STORE); showGate(err.message); }
    lastTry = { error: err };
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
  renderTry();
});

function renderList() {
  const q = $("#member-search").value.trim().toLowerCase();
  const terms = data.terms.filter((x) => !q || [x.source, x.target, x.note].some((v) => v.toLowerCase().includes(q)));
  $("#member-count").textContent = q ? t("glossary.count_matching", { n: fmt(terms.length) }) : tn("glossary.count", terms.length);
  if (!terms.length) {
    $("#member-list").innerHTML = `<p class="muted">${esc(q ? t("glossary.no_match_title", { q }) : t("member.empty"))}</p>`;
    return;
  }
  $("#member-list").innerHTML = `<table class="glossary-table member-table"><thead><tr><th>${esc(t("glossary.col_term"))}</th><th>${esc(t("glossary.col_translation"))}</th><th>${esc(t("glossary.col_note"))}</th><th><span class="sr-only">${esc(t("glossary.col_actions"))}</span></th></tr></thead><tbody>${
    terms.map((x, i) => `<tr>
      <td class="src"><mark class="term${x.conditional ? " cond" : ""}">${esc(x.source)}</mark>${x.conditional ? ` <span class="pill pill-cond" title="${esc(t("glossary.cond_title"))}">${esc(t("glossary.cond_pill"))}</span>` : ""}</td>
      <td class="tgt">${esc(x.target)}<span class="muted small lang-under">${esc(langName(x.target_lang))}</span></td>
      <td class="muted small">${esc(x.note)}</td>
      <td class="actions"><button class="btn btn-quiet" type="button" data-fix="${data.terms.indexOf(x)}">${esc(t("member.fix"))}</button><button class="btn btn-quiet" type="button" data-remove="${data.terms.indexOf(x)}">${esc(t("member.remove"))}</button></td>
    </tr>`).join("")
  }</tbody></table>`;
}

function onLangChange() {
  if (!data) return;
  fillSelect();
  renderList();
  renderFormText();
  renderTry();
}

$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  teamKey = e.currentTarget.key.value.trim();
  try {
    await loadTeam();
  } catch (err) {
    showGate(err.message);
  }
});

$("#member-search").addEventListener("input", renderList);

/** "Suggest a change" and "Suggest removing" buttons, in the term list and under try-it results. */
document.addEventListener("click", (e) => {
  const fix = e.target.closest("[data-fix]");
  const remove = e.target.closest("[data-remove]");
  if (!fix && !remove) return;
  const x = data.terms[fix ? fix.dataset.fix : remove.dataset.remove];
  const f = $("#propose-form");
  lastSent = null;
  $(".form-error", f).textContent = "";
  f.note.value = "";
  if (remove) {
    setFormMode(x);
  } else {
    setFormMode(null);
    f.source.value = x.source;
    f.target.value = "";
    f.target_lang.value = x.target_lang;
    f.target.placeholder = t("member.fix_ph", { current: x.target });
  }
  f.scrollIntoView({ behavior: "smooth", block: "start" });
  (remove ? f.note : f.target).focus({ preventScroll: true });
});

$("#remove-cancel").addEventListener("click", () => {
  const f = $("#propose-form");
  setFormMode(null);
  f.source.value = ""; f.note.value = "";
  f.source.focus();
});

$("#propose-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const err = $(".form-error", f);
  err.textContent = "";
  const body = removing
    ? { kind: "remove", source: removing.source, target_lang: removing.target_lang, note: f.note.value, name: f.name.value }
    : { source: f.source.value, target: f.target.value, target_lang: f.target_lang.value, note: f.note.value, name: f.name.value };
  try {
    await teamApi("/api/team/proposals", { method: "POST", body });
    remember(NAME_STORE, f.name.value.trim());
    lastSent = { term: body.source.trim(), kind: removing ? "remove" : "term" };
    setFormMode(null);  // also redraws the confirmation
    f.source.value = ""; f.target.value = ""; f.note.value = "";
    f.target.placeholder = t("glossary.target_ph");
    f.source.focus();
  } catch (e2) {
    if (e2.code === "wrong_team_key") { forget(KEY_STORE); return showGate(e2.message); }
    err.textContent = e2.message;
  }
});

async function boot() {
  await loadI18n();
  setFormMode(null);
  $("#propose-form").name.value = recall(NAME_STORE);
  teamKey = keyFromLink() || recall(KEY_STORE);
  if (!teamKey) return showGate();
  try {
    await loadTeam();
  } catch (err) {
    if (err.code === "wrong_team_key") forget(KEY_STORE);
    showGate(err.message);
  }
}

boot();
