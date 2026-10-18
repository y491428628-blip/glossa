"use strict";
/* Teammate page: browse the team glossary and suggest terms for the admin to review.
   Uses only the team key (from the link's #key=… or typed in), never the admin password. */

const KEY_STORE = "glossa.team_key";
const NAME_STORE = "glossa.name";
let teamKey = "";
let data = null;  // {team_name, default_target_lang, languages, terms}
let lastSent = "";  // kept so the confirmation can be redrawn in another language

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
  const sel = $("#propose-form select[name=target_lang]");
  const keep = sel.value || data.default_target_lang;
  sel.innerHTML = Object.keys(LANG_FALLBACK)
    .map((code) => `<option value="${esc(code)}"${code === keep ? " selected" : ""}>${esc(langName(code))}</option>`).join("");
}

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
      <td class="actions"><button class="btn btn-quiet" type="button" data-fix="${data.terms.indexOf(x)}">${esc(t("member.fix"))}</button></td>
    </tr>`).join("")
  }</tbody></table>`;
}

function onLangChange() {
  if (!data) return;
  fillSelect();
  renderList();
  $("#propose-status").textContent = lastSent ? t("member.sent", { term: lastSent }) : "";
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

$("#member-list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-fix]");
  if (!btn) return;
  const x = data.terms[btn.dataset.fix];
  const f = $("#propose-form");
  f.source.value = x.source;
  f.target.value = "";
  f.target_lang.value = x.target_lang;
  f.note.value = "";
  lastSent = "";
  $("#propose-status").textContent = "";
  f.target.placeholder = t("member.fix_ph", { current: x.target });
  f.scrollIntoView({ behavior: "smooth", block: "start" });
  f.target.focus({ preventScroll: true });
});

$("#propose-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const err = $(".form-error", f);
  err.textContent = "";
  try {
    await teamApi("/api/team/proposals", {
      method: "POST",
      body: { source: f.source.value, target: f.target.value, target_lang: f.target_lang.value, note: f.note.value, name: f.name.value },
    });
    remember(NAME_STORE, f.name.value.trim());
    lastSent = f.source.value.trim();
    $("#propose-status").textContent = t("member.sent", { term: lastSent });
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
