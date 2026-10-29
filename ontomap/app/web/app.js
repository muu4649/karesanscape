"use strict";
// OntoMap の画面。サーバー（FastAPI）の /api を呼び、地図は SVG で描く。
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const norm = s => String(s ?? "").normalize("NFKC").toLowerCase();
const NS = "http://www.w3.org/2000/svg";
const COLORS = ["var(--c1)", "var(--c2)", "var(--c3)"];
const METHOD_HINT = {
  emb: "意味の類似性: 文章の内容がどれくらい似ているかで並べます。話題の近い文書がまとまります。",
  rel: "技術関係の類似性: 課題・解決手段・用途の関係構造がどれくらい似ているかで並べます。用途が違っても、課題と解決手段が近い文書が隣に来ます。",
  hyb: "併用: 2 つの近さを、それぞれ順位にそろえてから半分ずつ合わせた近さです（値をそのまま足してはいません）。",
};
const METHOD_SHORT = {emb: "意味", rel: "技術関係", hyb: "併用"};
const METHOD_LONG = {emb: "意味の類似性", rel: "技術関係の類似性", hyb: "併用（意味＋技術関係）"};
const reduceMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// 近さの測り方の説明。配置が島に分かれたときは、島どうしの距離に意味がないことを添える
function methodHint(m) { return METHOD_HINT[m]; }
// 測り方を変えたときの表示（切り替えの印・島の注意）
function updateMethodUI(m) {
  $("#methodSeg").querySelectorAll("button").forEach(b => b.setAttribute("aria-checked", b.dataset.m === m));
  const n = (S.map && S.map.methods[m] && S.map.methods[m].islands) || 1;
  const note = $("#mapNote");
  note.hidden = n <= 1;
  note.textContent = n > 1 ? `この配置は、近い文書のつながりが ${n} つの島に分かれたため、島ごとに並べています。島どうしの位置や距離は直接比較できません。` : "";
}

async function api(path, opt = {}) {
  const r = await fetch(path, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const S = {
  map: null, pid: null, method: "emb", match: null, sel: null, nbSel: null, tab: null, rtab: "overview", rmode: "doc", bridges: null,
  colorBy: "", groups: [], overlay: null, vb: null, hideGeneric: true, hideWeakUse: false, upload: null,
};

// ---------- 画面の切り替え ----------
function show(view) {
  for (const v of ["viewHome", "viewImport", "viewMap", "viewStats", "viewOnto", "viewCommon", "viewCommonNew"]) $("#" + v).hidden = v !== view;
  // 左のメニュー: 今の画面に印を付け、地図を見ているときは畳む
  const h = location.hash || "#/";
  const cur = h === "#/" ? "home" : h === "#/new" ? "new" : h === "#/common-new" ? "common-new" : h.startsWith("#/common") ? "common" : "";
  document.querySelectorAll("#sideNav a").forEach(a => (a.dataset.nav === cur ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  document.body.classList.toggle("side-collapsed", view === "viewMap" ? true : sideCollapsed);
}
function route() {
  const h = location.hash || "#/";
  if (h.startsWith("#/map/")) { const [id, qs] = h.slice(6).split("?"); openMap(id, new URLSearchParams(qs || "")); }
  else if (h.startsWith("#/stats/")) { const [id, qs] = h.slice(8).split("?"); openStats(id, new URLSearchParams(qs || "")); }
  else if (h.startsWith("#/onto/")) openOnto(h.slice(7).split("?")[0]);
  else if (h === "#/common") openCommonList();
  else if (h === "#/common-new") openWizard();
  else if (h.startsWith("#/common/")) openCommon(h.slice(9).split("?")[0], new URLSearchParams(h.split("?")[1] || ""));
  else if (h === "#/new") openImport();
  else openHome();
}
window.addEventListener("hashchange", route);
let sideCollapsed = false;
try { sideCollapsed = localStorage.getItem("ontomap.side") === "1"; } catch (_) {}
$("#sideToggle").onclick = () => {
  sideCollapsed = !document.body.classList.contains("side-collapsed");
  document.body.classList.toggle("side-collapsed", sideCollapsed);
  try { localStorage.setItem("ontomap.side", sideCollapsed ? "1" : "0"); } catch (_) {}
};
$("#goHome").onclick = () => { location.hash = "#/"; };

// ---------- 一覧 ----------
let pollTimer = null;
async function openHome() {
  show("viewHome");
  $("#crumb").textContent = "";
  $("#topActions").innerHTML = "";
  const [list, onts] = await Promise.all([api("/api/projects"), api("/api/ontologies").catch(() => [])]);
  const olab = Object.fromEntries(onts.map(o => [o.id, o.label]));
  const box = $("#projectList");
  if (!list.length) {
    box.innerHTML = `<div class="empty"><h3>まだマップがありません</h3><p>特許データを取り込み、技術のまとまりや類似する技術を地図にできます。</p><a class="btn primary" href="#/new">最初のマップを作る</a></div>`;
    return;
  }
  box.innerHTML = list.map(p => {
    const st = p.status || {};
    const state = st.stage === "done" ? `<span class="tag">完成</span>` : st.stage === "error" ? `<span class="tag warn">失敗</span>` : `<span class="tag">計算中 ${Math.round((st.progress || 0) * 100)}%</span>`;
    return `<div class="card"><h3>${esc(p.name)}</h3>
      <div class="meta">${esc(p.n_docs)} 件 · ${esc(p.created)} · ${esc(p.source_file || "")}</div>
      <div class="meta" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(olab[p.ontology] || p.ontology || "")}">共通辞書: ${esc(olab[p.ontology] || p.ontology || "—")}</div>
      <div>${state}</div>
      <div class="row"><button class="btn primary small" data-open="${esc(p.id)}" ${st.stage === "done" ? "" : "disabled"}>開く</button>
      ${st.stage === "error" ? `<button class="btn small" data-rerun="${esc(p.id)}">計算し直す</button>` : ""}
      <button class="btn ghost small danger" data-del="${esc(p.id)}">削除</button></div></div>`;
  }).join("");
  box.querySelectorAll("[data-open]").forEach(b => b.onclick = e => { e.stopPropagation(); location.hash = "#/map/" + b.dataset.open; });
  // カード全体を押しても開く（完成したマップだけ。ボタンの上は除く）
  box.querySelectorAll(".card").forEach(c => { const b = c.querySelector("[data-open]"); if (b && !b.disabled) { c.style.cursor = "pointer"; c.onclick = e => { if (!e.target.closest("button")) b.click(); }; } });
  box.querySelectorAll("[data-rerun]").forEach(b => b.onclick = async () => { await api(`/api/projects/${b.dataset.rerun}/rerun`, {method: "POST"}); openHome(); });
  box.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("このマップを削除します。取り込んだ一覧と計算結果も消えます。よろしいですか？")) return;
    await api(`/api/projects/${b.dataset.del}`, {method: "DELETE"}); openHome();
  });
  clearTimeout(pollTimer);
  if (list.some(p => p.status && !["done", "error"].includes(p.status.stage))) pollTimer = setTimeout(() => { if (!$("#viewHome").hidden) openHome(); }, 3000);
}
$("#newMap").onclick = () => { location.hash = "#/new"; };

// ---------- 取り込み ----------
function steps(n) { ["#st1", "#st2", "#st3"].forEach((s, i) => $(s).classList.toggle("on", i === n - 1)); }
function openImport() {
  show("viewImport");
  $("#crumb").textContent = "新しいマップ";
  $("#topActions").innerHTML = "";
  steps(1);
  $("#dropZone").hidden = false; $("#mapping").hidden = true; $("#progress").hidden = true;
  $("#importError").textContent = ""; $("#fileInput").value = "";
}
const dz = $("#dropZone");
dz.onclick = () => $("#fileInput").click();
dz.onkeydown = e => { if (e.key === "Enter" || e.key === " ") $("#fileInput").click(); };
dz.ondragover = e => { e.preventDefault(); dz.classList.add("over"); };
dz.ondragleave = () => dz.classList.remove("over");
dz.ondrop = e => { e.preventDefault(); dz.classList.remove("over"); if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); };
$("#fileInput").onchange = e => { if (e.target.files[0]) uploadFile(e.target.files[0]); };

async function uploadFile(f) {
  const fd = new FormData(); fd.append("file", f);
  dz.innerHTML = `<p>読み込んでいます…</p>`;
  try {
    const pv = await api("/api/uploads", {method: "POST", body: fd});
    S.upload = pv;
    await renderMapping(pv);
  } catch (e) {
    dz.innerHTML = `<p class="error">${esc(e.message)}</p><p>別のファイルを選ぶには、ここを押してください。</p><input type="file" id="fileInput" accept=".csv,.tsv,.txt,.xlsx,.xlsm" hidden>`;
    $("#fileInput").onchange = ev => { if (ev.target.files[0]) uploadFile(ev.target.files[0]); };
  }
}
async function renderMapping(pv) {
  steps(2);
  dz.hidden = true; $("#mapping").hidden = false;
  $("#mapName").value = pv.filename.replace(/\.[^.]+$/, "");
  $("#fileInfo").textContent = `${pv.filename}：${pv.n_rows} 行、${pv.columns.length} 列。列名から推定して対応づけました。違っていれば選び直してください。`;
  const opts = c => `<option value="">（使わない）</option>` + pv.columns.map(x => `<option ${x === c ? "selected" : ""}>${esc(x)}</option>`).join("");
  $("#mapFields").innerHTML = pv.fields.map(f => `<label class="f"><span>${esc(f.label)}${f.required ? "<em>*</em>" : ""}</span><select data-k="${f.key}">${opts(pv.mapping[f.key] || "")}</select></label>`).join("");
  const cols = pv.columns.slice(0, 12);
  $("#previewTable").innerHTML = `<tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr>` + pv.rows.map(r => `<tr>${cols.map(c => `<td title="${esc(r[c])}">${esc(String(r[c]).slice(0, 80))}</td>`).join("")}</tr>`).join("");
  await loadOntologies();
}
async function loadOntologies(selectId) {
  const list = await api("/api/ontologies");
  $("#ontoSelect").innerHTML = list.map(o => `<option value="${esc(o.id)}" ${o.id === selectId ? "selected" : ""}>${esc(o.label)}</option>`).join("");
}
$("#ontoFile").onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try { const o = await api("/api/ontologies", {method: "POST", body: fd}); await loadOntologies(o.id); $("#importError").textContent = ""; }
  catch (err) { $("#importError").textContent = err.message; }
};
$("#cancelImport").onclick = () => { location.hash = "#/"; };
$("#startBuild").onclick = async () => {
  const mapping = {};
  document.querySelectorAll("#mapFields select").forEach(s => { if (s.value) mapping[s.dataset.k] = s.value; });
  try {
    const r = await api("/api/projects", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: $("#mapName").value, upload_id: S.upload.upload_id, mapping, ontology: $("#ontoSelect").value})});
    steps(3); $("#mapping").hidden = true; $("#progress").hidden = false;
    pollBuild(r.id);
  } catch (e) { $("#importError").textContent = e.message; }
};
async function pollBuild(pid) {
  const st = await api(`/api/projects/${pid}/status`);
  $("#progBar").style.width = Math.round((st.progress || 0) * 100) + "%";
  $("#progMsg").textContent = st.message || "";
  if (st.stage === "done") { location.hash = "#/map/" + pid; return; }
  if (st.stage === "error") { $("#progTitle").textContent = "地図を作れませんでした"; $("#progError").textContent = st.error || ""; return; }
  setTimeout(() => pollBuild(pid), 2000);
}

// ---------- 地図 ----------
const svg = $("#map");
const LAYERS = {};
for (const k of ["contour", "overlay", "bridge", "nb", "dots", "label", "traj", "sel"]) {
  const g = document.createElementNS(NS, "g"); g.dataset.layer = k; svg.appendChild(g); LAYERS[k] = g;
}
// 座標は 1,000 倍の単位で描く（小さすぎる数値は Chrome の描画で消えるため）
const K = 1000;
const FULL = {x: -1.15 * K, y: -1.15 * K, w: 2.3 * K, h: 2.3 * K};
function setVB(vb) { S.vb = vb; svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); }
const px = () => S.vb.w / Math.max(300, Math.min(svg.clientWidth, svg.clientHeight));  // 1 画面ピクセルあたりの座標

async function openMap(pid, params) {
  show("viewMap");
  if (S.pid !== pid || !S.map) {
    S.map = null;
    const m = await api(`/api/projects/${pid}/map`);
    S.map = m; S.pid = pid; S.method = "emb"; S.sel = null; S.nbSel = null; S.groups = []; S.colorBy = ""; S.overlay = null;
    S.rmode = "doc"; S.rtab = "overview"; S.bridges = null; $("#infoPop").hidden = true;
    initControls();
    setVB({...FULL});
  }
  $("#crumb").textContent = S.map.name;
  $("#topActions").innerHTML = "";
  $("#mtName").textContent = S.map.name;
  $("#tDict").onclick = () => { location.hash = "#/onto/" + pid; };
  $("#tStats").onclick = () => { location.hash = "#/stats/" + pid; };
  $("#tLeft").onclick = () => setPanel("left", $("#viewMap").classList.contains("left-closed"));
  $("#tRight").onclick = () => setPanel("right", $("#viewMap").classList.contains("right-closed"));
  $("#tBridges").onclick = () => { S.rmode = S.rmode === "doc" ? "bridges" : "doc"; if (S.rmode === "bridges") setPanel("right", true); renderSide(); };
  $("#tInfo").onclick = () => { const p = $("#infoPop"); p.hidden = !p.hidden; if (!p.hidden) renderInfo(); };
  applyFilter(); renderAll(); renderSide();
  if (params && [...params.keys()].length) applyParams(params);
}

// 左右の欄の開閉。地図が狭くなりすぎない（最小 720px）よう、狭い画面では片方を畳む
function setPanel(which, open) {
  const v = $("#viewMap");
  v.classList.toggle(which + "-closed", !open);
  $(which === "left" ? "#tLeft" : "#tRight").setAttribute("aria-pressed", open);
  if (open && window.innerWidth < 1440 && which === "right" && !v.classList.contains("left-closed") && v.querySelector(".stage").clientWidth < 720) v.classList.add("left-closed");
  requestAnimationFrame(rescale);
}
// マップの情報（件数・辞書の版・用途の判定元・測り方の説明）
function renderInfo() {
  const m = S.map, L = m.ontology_layers || {};
  $("#infoPop").innerHTML = `<dl><dt>特許の件数</dt><dd>${m.n_docs} 件</dd><dt>共通辞書</dt><dd>${esc(ontoLabel(L))}</dd>
    <dt>用途の判定</dt><dd>${esc(m.usage_source)} から</dd>${(m.facets.modules || []).length ? `<dt>分野別辞書</dt><dd>${m.facets.modules.map(([g, n]) => `${esc(g)} ${n} 件`).join("、")}</dd>` : ""}
    <dt>近さの測り方</dt><dd>${esc(METHOD_HINT[S.method])}</dd></dl>
    <p class="hint">地図は次元削減で描いています。二次元上の距離は、元の類似度を完全には再現しません。異なる測り方の値は同じ尺度ではありません。</p>`;
}

// URL の指定（例 #/map/<id>?m=rel&sel=D0116&nb=D0542&bridges=1&color=applicant&groups=京都大学）を画面に反映する
function applyParams(p) {
  const id2i = id => S.map.docs.findIndex(d => d.id === id);
  if (p.get("bridges") === "1") $("#showBridges").checked = true;
  if (p.get("overlay") === "1") $("#showOverlay").checked = true;
  if (p.get("color")) { $("#colorBy").value = p.get("color"); S.colorBy = p.get("color"); S.groups = (p.get("groups") || "").split(",").filter(Boolean).slice(0, 3); renderColorPick(); }
  if (p.get("q")) { $("#q").value = p.get("q"); applyFilter(); }
  const m = p.get("m");
  const after = () => {
    refreshOverlay(); renderAll();
    // 例: ?panel=bridges（異用途の類似技術の一覧）、?cmp=D0001,D0002（2 件の比較）、?rtab=near（右の欄のタブ）
    if (p.get("cmp")) { const [a, b] = p.get("cmp").split(",").map(id2i); if (a >= 0 && b >= 0) { S.sel = a; S.nbSel = b; S.rmode = "compare"; renderNeighborsLines(); renderSel(); renderSide(); return; } }
    if (p.get("panel") === "bridges") { S.rmode = "bridges"; renderSide(); return; }
    if (p.get("rtab")) S.rtab = p.get("rtab");
    const i = p.get("sel") ? id2i(p.get("sel")) : -1, j = p.get("nb") ? id2i(p.get("nb")) : -1;
    if (i >= 0) { if (p.get("tab")) S.tab = p.get("tab"); select(i, p.get("zoom") === "1", j >= 0 ? j : undefined); if (p.get("tab") || p.get("rtab")) { if (p.get("tab")) S.tab = p.get("tab"); if (p.get("rtab")) S.rtab = p.get("rtab"); renderSide(); } }
    else renderSide();
  };
  if (m && m !== S.method && S.map.methods[m]) { S.method = m; updateMethodUI(m); }
  after();
}

function initControls() {
  const m = S.map;
  $("#methodSeg").innerHTML = Object.keys(m.methods).map(k => `<button type="button" role="radio" data-m="${k}" aria-checked="${k === S.method}" title="${esc(METHOD_HINT[k])}">${esc(METHOD_SHORT[k] || k)}</button>`).join("");
  $("#methodSeg").querySelectorAll("button").forEach(b => b.onclick = () => switchMethod(b.dataset.m));
  updateMethodUI(S.method);
  const mods = m.facets.modules || [];
  $("#moduleSel").hidden = !mods.length;
  $("#moduleSel").innerHTML = `<option value="">分野別辞書（すべて）</option>` + mods.map(([g, n]) => `<option value="${esc(g)}">${esc(g)}（${n}）</option>`).join("");
  $("#usageSel").innerHTML = `<option value="">用途（すべて）</option>` + m.facets.usages.map(([u, n]) => `<option value="${esc(u)}">${esc(u)}（${n}）</option>`).join("");
  $("#appList").innerHTML = m.facets.applicants.map(([a]) => `<option value="${esc(a)}">`).join("");
  for (const id of ["#q", "#yFrom", "#yTo", "#ipcQ", "#usageSel", "#appQ", "#moduleSel"]) { $(id).value = ""; $(id).oninput = () => { applyFilter(); renderDots(); renderBridges(); if (S.sel == null) renderSide(); }; }
  if (m.facets.years) { $("#yFrom").placeholder = `出願年 ${m.facets.years[0]} から`; $("#yTo").placeholder = `${m.facets.years[1]} まで`; }
  $("#colorModule").hidden = !(S.map.facets.modules || []).length;
  $("#colorBy").value = ""; $("#colorBy").onchange = e => { S.colorBy = e.target.value; S.groups = []; renderColorPick(); refreshOverlay(); renderDots(); renderLegend(); };
  renderColorPick();
  for (const id of ["#showContour", "#showLabels", "#showBridges", "#showOverlay"]) $(id).onchange = () => { refreshOverlay(); renderAll(); };
}
$("#clearFilter").onclick = () => { for (const id of ["#q", "#yFrom", "#yTo", "#ipcQ", "#usageSel", "#appQ", "#moduleSel"]) $(id).value = ""; applyFilter(); renderDots(); renderBridges(); if (S.sel == null) renderSide(); };

function renderColorPick() {
  const m = S.map, box = $("#colorPick");
  if (!S.colorBy) { box.innerHTML = ""; return; }
  const items = S.colorBy === "usage" ? m.facets.usages.slice(0, 14) : S.colorBy === "module" ? (m.facets.modules || []) : m.facets.applicants.slice(0, 16);
  box.innerHTML = items.map(([g, n]) => {
    const k = S.groups.indexOf(g);
    return `<button type="button" class="chip" aria-pressed="${k >= 0}" data-g="${esc(g)}"><i style="background:${k >= 0 ? COLORS[k] : ""}"></i>${esc(g)}（${n}）</button>`;
  }).join("");
  box.querySelectorAll(".chip").forEach(c => c.onclick = () => {
    const g = c.dataset.g, k = S.groups.indexOf(g);
    if (k >= 0) S.groups.splice(k, 1); else if (S.groups.length < 3) S.groups.push(g); else return;
    renderColorPick(); refreshOverlay(); renderDots(); renderLegend();
  });
}
function groupOf(d) {
  if (!S.colorBy || !S.groups.length) return -1;
  const vals = S.colorBy === "usage" ? d[S.map.usage_key] : S.colorBy === "module" ? (d.modules || []) : d.applicants;
  return S.groups.findIndex(g => vals.includes(g));
}

function applyFilter() {
  const q = norm($("#q").value).trim(), y0 = +$("#yFrom").value || null, y1 = +$("#yTo").value || null;
  const ipc = norm($("#ipcQ").value).replace(/\s/g, ""), use = $("#usageSel").value, app = norm($("#appQ").value).trim(), mod = $("#moduleSel").value;
  const active = q || y0 || y1 || ipc || use || app || mod;
  const set = new Set();
  S.map.docs.forEach((d, i) => {
    if (q && !norm([d.title, d.snippet, d.applicants.join(" "), d.ipc, d.pub].join(" ")).includes(q)) return;
    if (y0 && !(d.year && d.year >= y0)) return;
    if (y1 && !(d.year && d.year <= y1)) return;
    if (ipc && !norm(d.ipc).replace(/\s/g, "").startsWith(ipc)) return;
    if (use && !d[S.map.usage_key].includes(use)) return;
    if (app && !d.applicants.some(a => norm(a).includes(app))) return;
    if (mod && !(d.modules || []).includes(mod)) return;
    set.add(i);
  });
  S.match = active ? set : null;
  $("#hitCount").textContent = active ? `該当 ${set.size} 件 / ${S.map.n_docs} 件` : `全 ${S.map.n_docs} 件`;
  $("#mtCount").textContent = active ? `表示 ${set.size} / ${S.map.n_docs} 件` : `${S.map.n_docs} 件`;
  const idx = active ? [...set] : S.map.docs.map((_, i) => i);
  // 地図の点と同じ文書を、一覧からも選べる（キーボード・読み上げでも使える）
  $("#hitList").innerHTML = idx.slice(0, 500).map(i => { const d = S.map.docs[i];
    return `<li data-i="${i}" tabindex="0" class="${i === S.sel ? "on" : ""}">${esc(d.title)}<div class="sub">${esc(d.applicants.slice(0, 2).join("・"))} ${d.year || ""}</div></li>`; }).join("") +
    (idx.length > 500 ? `<li class="sub" style="cursor:default">ほか ${idx.length - 500} 件（絞り込むと出ます）</li>` : "");
  $("#hitList").querySelectorAll("li[data-i]").forEach(li => {
    li.onclick = () => select(+li.dataset.i, true);
    li.onkeydown = e => { if (e.key === "Enter") select(+li.dataset.i, true); };
  });
}

function xy(i, P) { const p = (P || S.map.methods[S.method].xy)[i]; return [p[0] * K, -p[1] * K]; }
const pathD = pts => pts.map((p, k) => (k ? "L" : "M") + (p[0] * K).toFixed(1) + "," + (-p[1] * K).toFixed(1)).join("");
// SVG の属性には CSS 変数（var(--…)）が効かないので、変数を使う色はスタイルとして設定する
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) {
    const v = attrs[k];
    if (typeof v === "string" && v.includes("var(") && (k === "fill" || k === "stroke")) e.style.setProperty(k, v);
    else e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
}

function renderAll() { renderContours(); renderOverlay(); renderBridges(); renderNeighborsLines(); renderDots(); renderLabels(); renderLegend(); }

function renderContours() {
  const g = LAYERS.contour; g.innerHTML = "";
  if (!$("#showContour").checked) return;
  for (const c of S.map.methods[S.method].contours) {
    const d = c.paths.map(pathD).join("");
    el("path", {d, fill: "none", stroke: "var(--contour)", "stroke-width": (0.7 + 0.6 * c.level).toFixed(2), opacity: (0.35 + 0.6 * c.level).toFixed(2), "vector-effect": "non-scaling-stroke"}, g);
  }
}
function renderDots(P) {
  const g = LAYERS.dots; g.innerHTML = "";
  const r = 3.4 * px(), M = S.match;
  S.map.docs.forEach((d, i) => {
    const [x, y] = xy(i, P), k = groupOf(d), on = !M || M.has(i);
    const fill = k >= 0 ? COLORS[k] : on && M ? "var(--ink)" : "var(--dot)";
    const c = el("circle", {cx: x.toFixed(1), cy: y.toFixed(1), r: (k >= 0 || (M && on) ? r * 1.25 : r).toFixed(2), fill: on ? fill : "var(--dot-dim)", stroke: "#fff", "stroke-width": 0.8, "vector-effect": "non-scaling-stroke", opacity: on ? 0.92 : 0.35}, g);
    c.dataset.i = i;
  });
  renderSel(P);
}
function renderLabels() {
  const g = LAYERS.label; g.innerHTML = "";
  if (!$("#showLabels").checked) return;
  const fs = 12.5 * px();
  for (const L of S.map.methods[S.method].labels) {
    const t = el("text", {x: L.x * K, y: -L.y * K - (L.lines.length - 1) * fs * 0.6, "font-size": fs, "text-anchor": "middle", fill: "var(--label)", stroke: "rgba(248,247,244,.92)", "stroke-width": 3.2, "paint-order": "stroke", "vector-effect": "non-scaling-stroke", "font-weight": 600, "pointer-events": "none"}, g);
    L.lines.forEach((line, k) => { const s = el("tspan", {x: L.x * K, dy: k ? fs * 1.2 : 0}, t); s.textContent = line; });
  }
}
function renderLegend() {
  const items = [];
  if (S.groups.length) S.groups.forEach((g, k) => items.push(`<span><i style="background:${COLORS[k]}"></i>${esc(g)}</span>`));
  if ($("#showBridges").checked) items.push(`<span><svg width="22" height="8"><path d="M1 6 Q11 0 21 6" stroke="var(--accent)" fill="none" stroke-width="1.5" stroke-dasharray="3 2"/></svg>異用途の類似ペア（技術関係の類似性で判定）</span>`);
  if ($("#showContour").checked) items.push(`<span><svg width="22" height="8"><path d="M1 4 H21" stroke="var(--contour)" stroke-width="1"/></svg>文書の密度（等高線）</span>`);
  $("#legend").innerHTML = items.join("");
}
function renderBridges() {
  const g = LAYERS.bridge; g.innerHTML = "";
  if (!$("#showBridges").checked) return;
  const M = S.match;
  const list = S.map.bridges.filter(b => !M || (M.has(b.a) && M.has(b.b))).slice(0, 60);
  for (const b of list) {
    const [x1, y1] = xy(b.a), [x2, y2] = xy(b.b);
    const mx = (x1 + x2) / 2 - (y2 - y1) * 0.18, my = (y1 + y2) / 2 + (x2 - x1) * 0.18;
    const p = el("path", {d: `M${x1},${y1} Q${mx},${my} ${x2},${y2}`, fill: "none", stroke: "var(--accent)", "stroke-width": 1.3, "stroke-dasharray": "3 2", opacity: 0.55, "vector-effect": "non-scaling-stroke", style: "cursor:pointer"}, g);
    p.onclick = ev => { ev.stopPropagation(); select(b.a, false, b.b); };
  }
}
function renderNeighborsLines() {
  const g = LAYERS.nb; g.innerHTML = "";
  if (S.sel == null) return;
  const nb = S.map.methods[S.method].neighbors[S.sel] || [];
  const [x1, y1] = xy(S.sel);
  for (const [j] of nb) { const [x2, y2] = xy(j); el("line", {x1, y1, x2, y2, stroke: "var(--ink)", "stroke-width": 0.8, opacity: 0.35, "vector-effect": "non-scaling-stroke"}, g); }
}
function renderSel(P) {
  const g = LAYERS.sel; g.innerHTML = "";
  const ring = (i, color) => { const [x, y] = xy(i, P); el("circle", {cx: x, cy: y, r: 7 * px(), fill: "none", stroke: color, "stroke-width": 2, "vector-effect": "non-scaling-stroke"}, g); };
  if (S.sel != null) ring(S.sel, "var(--ink)");
  if (S.nbSel != null) ring(S.nbSel, "var(--accent)");
}

// 色分けした群の範囲と年の動き
async function refreshOverlay() {
  S.overlay = null;
  if ($("#showOverlay").checked && S.groups.length) {
    const groupBy = S.colorBy === "usage" ? "usage" : S.colorBy === "module" ? "module" : "applicant";
    try { S.overlay = await api(`/api/projects/${S.pid}/overlay?method=${S.method}&group_by=${groupBy}&groups=${encodeURIComponent(S.groups.join("\t"))}`); } catch (_) {}
  }
  renderOverlay();
}
function renderOverlay() {
  const g = LAYERS.overlay, t = LAYERS.traj; g.innerHTML = ""; t.innerHTML = "";
  if (!S.overlay || !$("#showOverlay").checked) return;
  S.overlay.forEach((o, k) => {
    const col = COLORS[S.groups.indexOf(o.group)] || COLORS[k];
    if (o.p50.length) el("path", {d: o.p50.map(pathD).join(""), fill: col, "fill-opacity": 0.14, stroke: "none"}, g);
    if (o.p80.length) el("path", {d: o.p80.map(pathD).join(""), fill: "none", stroke: col, "stroke-width": 1.2, opacity: 0.7, "vector-effect": "non-scaling-stroke"}, g);
    if (o.trajectory) {
      el("path", {d: pathD(o.trajectory), fill: "none", stroke: col, "stroke-width": 2.6, "stroke-linecap": "round", "vector-effect": "non-scaling-stroke"}, t);
      const fs = 11 * px();
      o.points.forEach((p, i) => {
        el("circle", {cx: p[0] * K, cy: -p[1] * K, r: 3.2 * px(), fill: col, stroke: "#fff", "stroke-width": 1, "vector-effect": "non-scaling-stroke"}, t);
        if (i === 0 || i === o.points.length - 1) {
          const tx = el("text", {x: p[0] * K + 5 * px(), y: -p[1] * K - 5 * px(), "font-size": fs, fill: col, "font-weight": 700, stroke: "rgba(248,247,244,.95)", "stroke-width": 3, "paint-order": "stroke", "vector-effect": "non-scaling-stroke"}, t);
          tx.textContent = o.point_labels[i];
        }
      });
    }
  });
}

// 方式の切り替え（点が動くアニメーション）
function switchMethod(m) {
  if (m === S.method) return;
  const A = S.map.methods[S.method].xy, B = S.map.methods[m].xy;
  S.method = m;
  updateMethodUI(m);
  LAYERS.contour.innerHTML = ""; LAYERS.label.innerHTML = ""; LAYERS.bridge.innerHTML = ""; LAYERS.nb.innerHTML = ""; LAYERS.overlay.innerHTML = ""; LAYERS.traj.innerHTML = "";
  const t0 = performance.now(), dur = reduceMotion() ? 0 : 900;
  const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  const step = now => {
    const t = dur ? Math.min(1, (now - t0) / dur) : 1, e = ease(t);
    const P = A.map((a, i) => [a[0] + (B[i][0] - a[0]) * e, a[1] + (B[i][1] - a[1]) * e]);
    renderDots(P);
    if (t < 1) requestAnimationFrame(step); else { refreshOverlay(); renderAll(); renderSide(); }
  };
  requestAnimationFrame(step);
}

// 拡大・移動・点の選択
let drag = null;
svg.addEventListener("wheel", e => {
  e.preventDefault();
  const r = svg.getBoundingClientRect(), f = Math.exp(e.deltaY * 0.0015);
  const pt = toMap(e.clientX - r.left, e.clientY - r.top);
  const w = Math.min(4 * K, Math.max(0.12 * K, S.vb.w * f)), h = w * S.vb.h / S.vb.w;
  setVB({x: pt[0] - (pt[0] - S.vb.x) * w / S.vb.w, y: pt[1] - (pt[1] - S.vb.y) * h / S.vb.h, w, h});
  rescale();
}, {passive: false});
function toMap(cx, cy) {
  const r = svg.getBoundingClientRect(), s = Math.min(r.width, r.height);
  const ox = (r.width - s) / 2, oy = (r.height - s) / 2;
  return [S.vb.x + (cx - ox) / s * S.vb.w, S.vb.y + (cy - oy) / s * S.vb.h];
}
// 押した点は pointerdown で覚える（setPointerCapture のあと、pointerup の target は svg 全体になり、どの点か分からなくなるため）
svg.addEventListener("pointerdown", e => { drag = {x: e.clientX, y: e.clientY, vb: {...S.vb}, moved: false, i: e.target.dataset ? e.target.dataset.i : undefined}; svg.setPointerCapture(e.pointerId); });
svg.addEventListener("pointermove", e => {
  if (drag) {
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; svg.classList.add("drag"); }
    if (drag.moved) { const r = svg.getBoundingClientRect(), s = Math.min(r.width, r.height); setVB({...drag.vb, x: drag.vb.x - dx / s * drag.vb.w, y: drag.vb.y - dy / s * drag.vb.h}); }
    return;
  }
  const i = e.target.dataset && e.target.dataset.i;
  const tip = $("#tip");
  if (i == null) { tip.hidden = true; return; }
  const d = S.map.docs[+i], r = svg.parentElement.getBoundingClientRect();
  tip.innerHTML = `<b>${esc(d.title)}</b>${esc(d.applicants.slice(0, 2).join("・"))} ${d.year || ""}<br><span class="hint">${esc(d[S.map.usage_key].join("・"))}</span>`;
  tip.hidden = false;
  tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 330) + "px"; tip.style.top = (e.clientY - r.top + 14) + "px";
});
svg.addEventListener("pointerup", e => {
  svg.classList.remove("drag");
  const moved = drag && drag.moved, i = drag ? drag.i : undefined; drag = null;
  if (moved) { rescale(); return; }
  if (i != null) select(+i, false); else { S.sel = null; S.nbSel = null; if (S.rmode === "compare") S.rmode = "bridges"; renderNeighborsLines(); renderSel(); renderSide(); }
});
svg.addEventListener("pointerleave", () => { $("#tip").hidden = true; });
$("#zIn").onclick = () => zoomBy(0.7); $("#zOut").onclick = () => zoomBy(1.4); $("#zFit").onclick = () => { setVB({...FULL}); rescale(); };
function zoomBy(f) { const cx = S.vb.x + S.vb.w / 2, cy = S.vb.y + S.vb.h / 2, w = S.vb.w * f; setVB({x: cx - w / 2, y: cy - w / 2, w, h: w}); rescale(); }
function rescale() { renderDots(); renderLabels(); renderOverlay(); }
window.addEventListener("resize", () => { if (S.map && !$("#viewMap").hidden) rescale(); });

function select(i, center, other) {
  S.sel = i; S.nbSel = other ?? null; S.tab = S.method;
  if (S.rmode === "bridges" && other == null) S.rmode = "doc";
  S.rtab = other != null ? "why" : (S.rtab === "why" ? "overview" : S.rtab);
  const v = $("#viewMap");
  if (v.classList.contains("right-closed")) setPanel("right", true);
  if (window.innerWidth < 1440 && !v.classList.contains("left-closed")) setPanel("left", false);
  document.querySelectorAll("#hitList li[data-i]").forEach(li => li.classList.toggle("on", +li.dataset.i === i));
  if (center) { const [x, y] = xy(i), w = Math.min(S.vb.w, 1.2 * K); setVB({x: x - w / 2, y: y - w / 2, w, h: w}); rescale(); }
  renderNeighborsLines(); renderSel(); renderSide();
}

// ---------- 右の欄（概要｜近い特許｜類似の根拠、異用途の類似技術、2 文書の比較） ----------
const ROLE_LAB = {problem: "課題", solution: "解決手段", use: "用途（抽出）"};
async function renderSide() {
  const box = $("#side");
  if (!S.map) return;
  $("#tBridges").setAttribute("aria-pressed", S.rmode !== "doc");
  if (S.rmode === "bridges") return renderBridges2(box);
  if (S.rmode === "compare") return renderCompare(box);
  if (S.sel == null) {
    box.innerHTML = `<div class="side-title">特許を選んでください</div>
      <p class="hint">地図の点か、左の「該当する文書」から特許を選ぶと、概要・近い特許・類似の根拠を表示します。</p>
      <div class="box" style="margin-top:10px"><h3>異用途の類似技術</h3><p class="note">技術関係は近いのに、IPC から見た用途が重ならない特許の組です。別の用途への転用の手がかりになります。</p>
      <button class="btn small" id="goBridges" type="button">一覧を開く（${S.map.bridges.length} 組）</button></div>`;
    $("#goBridges").onclick = () => { S.rmode = "bridges"; renderSide(); };
    return;
  }
  const d = S.map.docs[S.sel];
  const tabs = {overview: "概要", near: "近い特許", why: "類似の根拠"};
  box.innerHTML = `<div class="side-title">${esc(d.title)}</div>
    <div class="rtabs" role="tablist">${Object.entries(tabs).map(([k, v]) => `<button type="button" role="tab" data-r="${k}" aria-selected="${S.rtab === k}">${v}</button>`).join("")}</div><div id="rBody"><p class="hint">読み込んでいます…</p></div>`;
  box.querySelectorAll(".rtabs button").forEach(b => b.onclick = () => { S.rtab = b.dataset.r; renderSide(); });
  if (S.rtab === "near") return renderNear(d);
  if (S.rtab === "why") return renderWhy(d);
  return renderOverview(d);
}
async function renderOverview(d) {
  let det;
  try { det = await api(`/api/projects/${S.pid}/docs/${d.id}`); } catch (e) { $("#rBody").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (S.map.docs[S.sel] !== d || S.rtab !== "overview") return;
  const f = det.fields;
  const sec = (k, lab, open) => f[k] ? `<details class="sec" ${open ? "open" : ""}><summary>${lab}</summary><p>${esc(f[k])}</p></details>` : "";
  $("#rBody").innerHTML = `<dl class="kv"><dt>公報番号</dt><dd>${esc(d.pub || "—")}</dd><dt>出願人</dt><dd>${esc(d.applicants.join("・") || "—")}</dd>
    <dt>出願年</dt><dd>${esc(d.year || "—")}</dd><dt>IPC・FI</dt><dd>${esc(d.ipc || "—")}</dd>${d.modules && d.modules.length ? `<dt>分野別辞書</dt><dd>${esc(d.modules.join("・"))}</dd>` : ""}<dt>用途（${esc(S.map.usage_source.replace(/（.*/, ""))}）</dt><dd>${esc(d[S.map.usage_key].join("・") || "—")}</dd></dl>
    ${sec("problem", "課題", true)}${sec("solution", "解決手段", false)}
    ${sec("use_sentences", `用途 <span class="hint">（用途を述べた文${f.use_sentences_source ? "・" + esc(f.use_sentences_source) : ""}）</span>`, false)}
    ${sec("use_text", `用途の抽出に使った段落 <span class="hint">（${f.use_source ? esc(f.use_source) : "用途の列"}）</span>`, false)}
    ${sec("abstract", "要約", false)}
    <details class="sec"><summary>抽出した技術関係（${det.triples.length} 件）</summary>${det.triples.map(t => `<div class="rel ${t.generic ? "generic" : ""} ${t.use === "weak" ? "weakuse" : ""}">${esc(t.s)} —${esc(t.p)}→ ${esc(t.o)}${useTag(t.use)}</div>`).join("") || `<p>技術関係を抽出できませんでした。技術関係の類似性ではこの特許を比べられません。</p>`}</details>`;
}
function renderNear(d) {
  const by = S.tab || S.method;
  const nb = S.map.methods[by].neighbors[S.sel] || [];
  const myU = new Set(d[S.map.usage_key]);
  $("#rBody").innerHTML = `<div class="form-row"><span class="hint">並び替え</span><div class="seg" style="width:auto;grid-template-columns:repeat(3,auto)">${Object.keys(S.map.methods).map(k => `<button type="button" data-t="${k}" aria-checked="${k === by}">${METHOD_SHORT[k]}</button>`).join("")}</div></div>
    <ol class="nb" id="nbList">${nb.map(([j, sc], r) => { const e = S.map.docs[j], diff = myU.size && e[S.map.usage_key].length && !e[S.map.usage_key].some(u => myU.has(u));
      return `<li data-j="${j}" class="${j === S.nbSel ? "on" : ""}" tabindex="0"><span>${r + 1}. ${esc(e.title)}<br><span class="hint">${esc(e.applicants.slice(0, 2).join("・"))} ${e.year || ""}</span></span><span class="s">${sc.toFixed(2)}</span>
        <span class="u">${diff ? `<span class="tag warn">用途が違う</span>` : myU.size ? `<span class="tag">用途が重なる</span>` : ""}${e[S.map.usage_key].slice(0, 3).map(u => `<span class="tag">${esc(u)}</span>`).join("")}</span></li>`; }).join("") || `<li>近い特許がありません。</li>`}</ol>
    <p class="hint">${esc(METHOD_LONG[by])}で近い順の上位 ${nb.length} 件です。押すと「類似の根拠」を表示します。</p>`;
  $("#rBody").querySelectorAll(".seg button").forEach(b => b.onclick = () => { S.tab = b.dataset.t; renderNear(d); });
  $("#nbList").querySelectorAll("li[data-j]").forEach(li => {
    const go = () => { S.nbSel = +li.dataset.j; S.rtab = "why"; renderSel(); renderSide(); };
    li.onclick = go; li.onkeydown = e => { if (e.key === "Enter") go(); };
  });
}
function evHtml(ev) {
  if (!ev || !ev.text) return "";
  let t = esc(ev.text);
  for (const w of [ev.s, ev.o].filter(Boolean).sort((a, b) => b.length - a.length)) t = t.split(esc(w)).join(`<mark>${esc(w)}</mark>`);
  return `<div class="ev">${t}</div>`;
}
async function renderWhy(d) {
  if (S.nbSel == null) { $("#rBody").innerHTML = `<p class="hint">「近い特許」で相手を選ぶと、2 件が近い理由を表示します。</p>`; return; }
  const b = S.map.docs[S.nbSel];
  let ex;
  try { ex = await api(`/api/projects/${S.pid}/explain?a=${d.id}&b=${b.id}`); } catch (e) { $("#rBody").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (S.map.docs[S.sel] !== d || S.rtab !== "why") return;
  $("#rBody").innerHTML = whyHtml(ex, d, b, true);
  bindWhy(() => renderWhy(d));
}
function whyHtml(ex, a, b, withHead) {
  const sc = k => `<div>${METHOD_SHORT[k]}<b>${ex.scores[k] == null ? "—" : ex.scores[k].toFixed(2)}</b>${ex.ranks[k] + 1} 位</div>`;
  const weak = m => m.a_use === "weak" || m.b_use === "weak";
  const rows = ex.matches.filter(m => !(S.hideGeneric && m.generic) && !(S.hideWeakUse && weak(m)));
  const hidden = ex.matches.length - rows.length;
  const diffs = Object.keys(ROLE_LAB).map(k => (ex.only_a[k].length || ex.only_b[k].length) ? `<tr><th>${ROLE_LAB[k]}</th><td>${ex.only_a[k].map(x => `<span class="tag">${esc(x)}</span>`).join(" ") || "—"}</td><td>${ex.only_b[k].map(x => `<span class="tag">${esc(x)}</span>`).join(" ") || "—"}</td></tr>` : "").join("");
  return `${withHead ? `<div class="hint">相手: <b>${esc(b.title)}</b></div>` : ""}
    <h3 class="h3">A. 類似性の要約</h3><div class="summary">${esc(ex.summary)}</div>
    <h3 class="h3">B. 類似度の内訳</h3><div class="scores">${sc("emb")}${sc("rel")}${sc("hyb")}</div>
    <p class="hint">順位は、この特許から見た相手の近さの順番です（1 位が最も近い）。併用は、2 つの近さを順位にそろえてから半分ずつ合わせた値です。異なる測り方の値は同じ尺度ではありません。</p>
    <h3 class="h3">C. 一致した技術関係</h3>
    <label class="chk"><input type="checkbox" id="hideGen" ${S.hideGeneric ? "checked" : ""}> どの文書にも出る一般的な概念を含む関係を隠す</label>
    <label class="chk"><input type="checkbox" id="hideWeak" ${S.hideWeakUse ? "checked" : ""}> 用途が用途を述べた文で確かめられない関係を隠す</label>
    ${rows.map(m => `<div class="rel ${m.generic ? "generic" : ""} ${weak(m) ? "weakuse" : ""}"><div class="pair">
      <span>この特許：${esc(m.a.s)} —${esc(m.a.p)}→ ${esc(m.a.o)}${useTag(m.a_use)}</span>${evHtml(m.a_ev)}
      <span>相手：${esc(m.b.s)} —${esc(m.b.p)}→ ${esc(m.b.o)}${useTag(m.b_use)}</span>${evHtml(m.b_ev)}</div>
      <span class="sc">一致度 ${m.score.toFixed(2)}</span></div>`).join("") || `<p class="hint">表示する一致した関係はありません${hidden ? `（条件に合わない ${hidden} 件を隠しています）` : ""}。</p>`}
    ${rows.length && hidden ? `<p class="hint">条件に合わない ${hidden} 件を隠しています。</p>` : ""}
    <p class="hint">D. 根拠の原文: 各関係の下の灰色の欄が、関係を抽出した箇所です（黄色が辞書で拾った語）。</p>
    <h3 class="h3">E. 相違点（片方の特許にだけ抽出された概念）</h3>
    ${diffs ? `<table class="cmp diff"><tr><th></th><th>この特許だけ</th><th>相手だけ</th></tr>${diffs}</table>` : `<p class="hint">確認できる違いはありません。</p>`}
    ${S.map.use_check && S.map.use_check.rate != null ? `<p class="hint">このマップでは、技術関係に出る用途の概念のうち ${Math.round(S.map.use_check.rate * 100)}% が、用途を述べた文で確かめられます。</p>` : ""}`;
}
function bindWhy(redo) {
  $("#hideGen").onchange = e => { S.hideGeneric = e.target.checked; redo(); };
  $("#hideWeak").onchange = e => { S.hideWeakUse = e.target.checked; redo(); };
}
// 関係の端の用途の概念が、その文書の用途の文で確かめられるか
function useTag(u) {
  if (u === "ok") return ` <span class="utag ok" title="この用途は、明細書の用途を述べた文にも出ています">用途文に記載あり</span>`;
  if (u === "weak") return ` <span class="utag weak" title="この用途は用途の段落の周辺の語から抽出したもので、用途を述べた文には出ていません">周辺段落から抽出・要確認</span>`;
  return "";
}
async function renderBridges2(box) {
  box.innerHTML = `<div class="side-title">異用途の類似技術</div><p class="hint">技術関係の類似性が高いのに、IPC から見た用途が重ならない組です。用途展開の仮説を考える手がかりで、類似性だけで転用できると判断するものではありません。</p><div id="brList"><p class="hint">読み込んでいます…</p></div>`;
  if (!S.bridges) { try { S.bridges = await api(`/api/projects/${S.pid}/bridges?limit=80`); } catch (e) { $("#brList").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; } }
  if (S.rmode !== "bridges") return;
  const M = S.match, list = S.bridges.filter(b => !M || (M.has(b.a) && M.has(b.b)));
  const st = u => u === "ok" ? `<span class="utag ok">用途文に記載あり</span>` : u === "weak" ? `<span class="utag weak">周辺段落から抽出・要確認</span>` : "";
  $("#brList").innerHTML = list.map((b, k) => { const A = S.map.docs[b.a], B = S.map.docs[b.b];
    return `<div class="bridge-row" data-k="${k}" tabindex="0"><div class="ttl">${esc(A.title)}<br>× ${esc(B.title)}</div>
      <dl class="kv2"><dt>共通する課題</dt><dd>${esc(b.shared.problem.join("・") || "—")}</dd><dt>共通する解決手段</dt><dd>${esc(b.shared.solution.join("・") || "—")}</dd>
      <dt>IPC の用途</dt><dd>${esc(b.ua.join("・"))} ／ ${esc(b.ub.join("・"))}</dd><dt>技術関係の類似度</dt><dd>${b.rel.toFixed(2)}（意味では ${b.emb_rank + 1} 位） ${st(b.use_status)}</dd></dl></div>`; }).join("") || `<p class="hint">該当する組がありません${M ? "（絞り込みを外すと出るかもしれません）" : ""}。</p>`;
  $("#brList").querySelectorAll(".bridge-row").forEach(r => {
    const b = list[+r.dataset.k];
    const go = () => { S.sel = b.a; S.nbSel = b.b; S.rmode = "compare"; S.cmp = b; renderNeighborsLines(); renderSel(); renderSide(); };
    r.onclick = go; r.onkeydown = e => { if (e.key === "Enter") go(); };
  });
}
async function renderCompare(box) {
  const a = S.map.docs[S.sel], b = S.map.docs[S.nbSel];
  box.innerHTML = `<button class="btn ghost small" id="cmpBack" type="button">← 異用途の類似技術の一覧</button><div class="side-title" style="margin-top:6px">2 件の比較</div><div id="cmpBody"><p class="hint">読み込んでいます…</p></div>`;
  $("#cmpBack").onclick = () => { S.rmode = "bridges"; S.sel = null; S.nbSel = null; renderNeighborsLines(); renderSel(); renderSide(); };
  let ex;
  try { ex = await api(`/api/projects/${S.pid}/explain?a=${a.id}&b=${b.id}`); } catch (e) { $("#cmpBody").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (S.rmode !== "compare") return;
  const cell = (k, side) => [...ex.shared[k].map(x => `<span class="tag" style="border-color:var(--brand2)">${esc(x)}</span>`), ...(side === "a" ? ex.only_a[k] : ex.only_b[k]).map(x => `<span class="tag">${esc(x)}</span>`)].join(" ") || "—";
  $("#cmpBody").innerHTML = `<table class="cmp"><tr><th></th><th>A</th><th>B</th></tr>
    <tr><th>特許</th><td>${esc(a.title)}</td><td>${esc(b.title)}</td></tr>
    <tr><th>課題</th><td>${cell("problem", "a")}</td><td>${cell("problem", "b")}</td></tr>
    <tr><th>解決手段</th><td>${cell("solution", "a")}</td><td>${cell("solution", "b")}</td></tr>
    <tr><th>用途（抽出）</th><td>${cell("use", "a")}</td><td>${cell("use", "b")}</td></tr>
    <tr><th>用途（IPC）</th><td>${esc(a[S.map.usage_key].join("・") || "—")}</td><td>${esc(b[S.map.usage_key].join("・") || "—")}</td></tr>
    <tr><th>IPC・FI</th><td>${esc(a.ipc || "—")}</td><td>${esc(b.ipc || "—")}</td></tr>
    <tr><th>出願人</th><td>${esc(a.applicants.join("・") || "—")}</td><td>${esc(b.applicants.join("・") || "—")}</td></tr></table>
    <p class="hint">青枠は両方に共通する概念です。</p>
    ${whyHtml(ex, a, b, false).split("この特許：").join("A：").split("相手：").join("B：").split("この特許だけ").join("A だけ").split("相手だけ").join("B だけ")}`;
  bindWhy(() => renderCompare(box));
}



// ---------- 辞書を改善（共通辞書＋このマップの追加語） ----------
const SEC_LABEL = {problem: "課題", solution: "解決手段", use_text: "用途"};
const TOP_NAME = {P: "課題", M: "解決手段", U: "用途"};
function ontoLabel(L) {
  if (!L) return "（版の記録なし）";
  const base = L.base_label.includes(L.base_version) ? L.base_label : `${L.base_label} ${L.base_version}`;
  return base + (L.use_extension ? ` ＋ このマップの追加語 v${L.extension_version}` : "（共通辞書だけ）");
}
const O = {pid: null, st: null, sec: "solution", poll: null};

async function openOnto(pid) {
  show("viewOnto");
  O.pid = pid;
  $("#topActions").innerHTML = `<button class="btn small" id="backMap" type="button">地図に戻る</button><button class="btn small" id="toList2" type="button">マップ一覧</button>`;
  $("#backMap").onclick = () => { location.hash = "#/map/" + pid; };
  $("#toList2").onclick = () => { location.hash = "#/"; };
  await loadOnto();
}
async function loadOnto() {
  clearTimeout(O.poll);
  const st = await api(`/api/projects/${O.pid}/ontology`);
  O.st = st;
  $("#crumb").textContent = `${st.name} · 辞書を改善`;
  renderOntoLayers(); renderCoverage(); renderRebuild(); renderCandTabs(); renderCands(); renderAdopted();
  const busy = st.status && !["done", "error"].includes(st.status.stage);
  if (busy || st.candidates.state === "computing") O.poll = setTimeout(() => { if (!$("#viewOnto").hidden) loadOnto(); }, 3000);
}
function renderOntoLayers() {
  const st = O.st, e = st.extension, L = st.built.layers;
  $("#ontoLayers").innerHTML = `<h3>2 つの層</h3>
    <div>共通辞書: <b>${esc(st.base.label)}</b>${st.base.label.includes(st.base.version) ? "" : " " + esc(st.base.version)}（概念 ${st.base.n_concepts}）</div>
    ${st.base.modules ? `<table class="cov" style="margin:6px 0"><tr><th>分野別辞書</th><th class="num">概念</th><th class="num">使った文書</th></tr>${st.base.modules.map(m => `<tr><td title="${esc(m.routes.join(", "))}">${esc(m.label)}</td><td class="num">${m.concepts}</td><td class="num">${m.id === "upper" ? "すべて" : m.docs}</td></tr>`).join("")}</table><p class="note">文書の IPC・FI を見て、使う分野別辞書を切り替えます（どれにも当たらない文書はすべての分野別辞書）。</p>` : ""}
    <div>このマップの追加語（下書き）: 新しい概念 <b>${e.concepts.length}</b> · 別名 <b>${e.aliases.length}</b> · 無視 ${e.ignored.length}</div>
    <p class="note">いまの地図: ${esc(ontoLabel(L))}</p>
    ${st.dirty && st.built.use_extension ? `<p class="note warn">下書きの変更は、まだ地図に反映されていません。</p>` : ""}
    ${st.common_upgrade && st.common_upgrade.latest > st.common_upgrade.current ? `<p class="note warn">共通辞書「${esc(st.common_upgrade.name)}」の新しい版 v${st.common_upgrade.latest} が出ています（いまは v${st.common_upgrade.current}）。${st.common_upgrade.notes.map(n => esc(n.note)).filter(Boolean).join(" / ")}</p><button class="btn small" id="doUpgrade" type="button">新しい版に切り替えて再計算</button><p class="note">切り替えると、追加語のうち共通辞書に入ったものはこのマップの追加語から外します。</p>` : ""}`;
  const ub = $("#doUpgrade");
  if (ub) ub.onclick = async () => {
    await api(`/api/projects/${O.pid}/rebuild`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({use_extension: true, upgrade_common: true})});
    S.map = null; loadOnto();
  };
}
function renderCoverage() {
  const c = O.st.coverage, pct = v => v == null ? "—" : (v * 100).toFixed(1) + "%";
  const rows = Object.keys(c.base).map(k => {
    const b = c.base[k].rate, d = c.draft[k].rate;
    return `<tr><td>${esc(c.base[k].label)}</td><td class="num">${c.base[k].n}</td><td class="num">${pct(b)}</td><td class="num ${d != null && b != null && d < b ? "better" : ""}">${pct(d)}</td></tr>`;
  }).join("");
  $("#ontoCoverage").innerHTML = `<h3>概念を抽出できなかった割合</h3>
    <table class="cov"><tr><th>段落</th><th class="num">文書</th><th class="num">共通辞書だけ</th><th class="num">＋このマップの追加語</th></tr>${rows}</table>
    <p class="note">段落が空の文書は数えません。低いほど、その段落の中身を関係として拾えています。</p>
    ${shadowNote()}`;
}
// 追加語が長い一致で共通辞書の語を覆った箇所（例: 「ガス吸着」が課題の段落の「ガス吸着量」を覆い「吸着性能」が消える）
function shadowNote() {
  const sh = O.st.shadowed;
  if (!sh || !sh.terms.length) return "";
  const rows = sh.terms.slice(0, 6).map(t => `<li><b>${esc(t.term)}</b>（${esc(TOP_NAME[t.as_top] || "")}「${esc(t.as)}」）が ${t.n_docs} 件で
    ${t.hidden.map(h => `「${esc(h.label)}」`).join("")} を隠しています · ${Object.entries(t.sections).map(([k, v]) => `${esc(k)} ${v}`).join("、")}</li>`).join("");
  return `<div class="shadow"><p class="note warn">追加語が、共通辞書の語を ${sh.n_docs} 件の文書で隠しています。長い語が優先して拾われるためです。意図と違えば取り消してください。</p><ul>${rows}</ul></div>`;
}
function renderRebuild() {
  const st = O.st, busy = st.status && !["done", "error"].includes(st.status.stage);
  const useExt = st.built.use_extension;
  $("#ontoRebuild").innerHTML = `<h3>変更を反映して再計算</h3>
    <label><input type="radio" name="useExt" value="1" ${useExt ? "checked" : ""}> 共通辞書＋このマップの追加語</label>
    <label><input type="radio" name="useExt" value="0" ${useExt ? "" : "checked"}> 共通辞書だけ（追加語を使わない）</label>
    ${busy ? `<div class="bar" style="margin-top:8px"><span style="width:${Math.round((st.status.progress || 0) * 100)}%"></span></div><p class="note">${esc(st.status.message || "")}</p>`
      : `<button class="btn primary small" id="doRebuild" type="button" style="margin-top:8px">再計算する</button><p class="note">このマップの追加語が前の版から変わっていれば、版を 1 つ上げて控えを残します。</p>`}`;
  const b = $("#doRebuild");
  if (b) b.onclick = async () => {
    const use = document.querySelector("input[name=useExt]:checked").value === "1";
    await api(`/api/projects/${O.pid}/rebuild`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({use_extension: use})});
    S.map = null;
    loadOnto();
  };
}
function renderCandTabs() {
  const cs = O.st.candidates.candidates || [];
  $("#candTabs").innerHTML = Object.entries(SEC_LABEL).map(([k, v]) =>
    `<button type="button" role="tab" data-sec="${k}" aria-selected="${O.sec === k}">${v}（${cs.filter(c => c.section === k).length}）</button>`).join("");
  $("#candTabs").querySelectorAll("button").forEach(b => b.onclick = () => { O.sec = b.dataset.sec; renderCandTabs(); renderCands(); });
  const cd = O.st.candidates;
  $("#candInfo").innerHTML = cd.state === "computing" ? "探しています…（1 分ほど）"
    : cd.state === "error" ? `探せませんでした <button class="btn ghost small" id="recand" type="button">もう一度探す</button>`
    : `${esc(cd.computed || "")} にこのマップの追加語 v${cd.extension_version ?? 0} の下書きで探した結果 · 出た文書数の多い順 <button class="btn ghost small" id="recand" type="button">今の下書きで探し直す</button>`;
  const r = $("#recand");
  if (r) r.onclick = async () => { $("#candInfo").textContent = "探しています…（1 分ほど）"; await api(`/api/projects/${O.pid}/ontology/candidates`, {method: "POST"}); loadOnto(); };
}
function highlight(text, term) {
  const i = text.indexOf(term);
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + "<mark>" + esc(term) + "</mark>" + esc(text.slice(i + term.length));
}
function renderCands() {
  const st = O.st, e = st.extension;
  const taken = new Set([...e.concepts.map(c => c.label), ...e.aliases.map(a => a.alias), ...e.ignored]);
  const rows = (st.candidates.candidates || []).filter(c => c.section === O.sec);
  if (!rows.length) { $("#candTable").innerHTML = `<tr><td class="hint">${st.candidates.state === "computing" ? "探しています。" : "この段落の候補はありません。"}</td></tr>`; return; }
  const ch = st.choices;
  $("#candTable").innerHTML = `<tr><th>語</th><th class="num">文書</th><th>使われ方（例）</th><th>どう入れるか</th></tr>` + rows.map((c, i) => {
    const sugP = (c.parent_suggestions || []).map(p => `<option value="c:${esc(p.id)}">${esc(p.label)}${p.sim != null ? `（近さ ${p.sim.toFixed(2)}）` : ""}</option>`).join("");
    const sugA = (c.alias_suggestions || []).map(p => `<option value="a:${esc(p.id)}">${esc(p.label)}（近さ ${p.sim.toFixed(2)}）</option>`).join("");
    const allP = (ch[c.top] || []).filter(x => x.mid).map(x => `<option value="c:${esc(x.id)}">${esc(x.label)}</option>`).join("");
    const allA = (ch[c.top] || []).map(x => `<option value="a:${esc(x.id)}">${esc(x.label)}</option>`).join("");
    const ex = (c.examples || []).slice(0, 2).map(x => `<div>${esc(x.doc_id)}: …${highlight(x.text, c.term)}…</div>`).join("");
    return `<tr data-i="${i}" class="${taken.has(c.term) ? "done" : ""}"><td class="term">${esc(c.term)}</td><td class="num">${c.df}</td><td class="ex">${ex}</td>
      <td><div class="ops"><select aria-label="${esc(c.term)} の入れ方">
        <optgroup label="新しい概念として、この下に置く（提案）">${sugP}</optgroup>
        <optgroup label="既存の概念の別名にする（提案）">${sugA}</optgroup>
        <optgroup label="新しい概念として、この下に置く（すべて）">${allP}</optgroup>
        <optgroup label="既存の概念の別名にする（すべて）">${allA}</optgroup>
      </select><button class="btn small" data-act="add" type="button">入れる</button><button class="btn ghost small" data-act="ignore" type="button">無視</button></div></td></tr>`;
  }).join("");
  $("#candTable").querySelectorAll("tr[data-i]").forEach(tr => {
    const c = rows[+tr.dataset.i];
    tr.querySelector("[data-act=add]").onclick = async () => {
      const v = tr.querySelector("select").value, [kind, id] = [v.slice(0, 1), v.slice(2)];
      const body = kind === "c" ? {term: c.term, mode: "concept", top: c.top, parent: id} : {term: c.term, mode: "alias", concept_id: id};
      await adoptReq(body);
    };
    tr.querySelector("[data-act=ignore]").onclick = () => adoptReq({term: c.term, mode: "ignore"});
  });
}
async function adoptReq(body) {
  try {
    await api(`/api/projects/${O.pid}/ontology/adopt`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  } catch (e) { alert(e.message); }
  loadOnto();
}
function renderAdopted() {
  const e = O.st.extension, lab = {};
  for (const t of Object.values(O.st.choices)) for (const x of t) lab[x.id] = x.label;
  for (const [k, v] of Object.entries(TOP_NAME)) lab[k] = v + "（上位型の直下）";
  const li = (main, sub, kind, key) => `<li><span>${esc(main)}<br><span class="sub">${esc(sub)}</span></span><button class="btn ghost small" data-kind="${kind}" data-key="${esc(key)}" type="button">取り消す</button></li>`;
  const empty = `<div class="empty">まだありません</div>`;
  $("#adopted").innerHTML = `<div class="adopted">
    <div class="box"><h3>新しい概念</h3><ul>${e.concepts.map(c => li(c.label, `${TOP_NAME[c.top]} · ${lab[c.parent] || c.parent} の下`, "concept", c.id)).join("") || empty}</ul></div>
    <div class="box"><h3>別名</h3><ul>${e.aliases.map(a => li(a.alias, `→ ${a.label}`, "alias", a.concept_id + "\t" + a.alias)).join("") || empty}</ul></div>
    <div class="box"><h3>無視した語</h3><ul>${e.ignored.map(t => li(t, "候補に出さない", "ignore", t)).join("") || empty}</ul></div></div>`;
  $("#adopted").querySelectorAll("button[data-kind]").forEach(b => b.onclick = async () => {
    await api(`/api/projects/${O.pid}/ontology/unadopt?kind=${encodeURIComponent(b.dataset.kind)}&key=${encodeURIComponent(b.dataset.key)}`, {method: "POST"});
    loadOnto();
  });
}

// ---------- 共通辞書を改善 ----------
const C = {cid: null, st: null, tab: "summary", module: "", q: "", parents: null, corpus: null};
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});

async function openCommonList() {
  show("viewCommon");
  $("#crumb").textContent = "共通辞書";
  $("#topActions").innerHTML = `<button class="btn primary small" id="toWiz" type="button">新しい共通辞書</button>`;
  $("#toWiz").onclick = () => { location.hash = "#/common-new"; };
  const [onts, mine] = await Promise.all([api("/api/ontologies"), api("/api/common")]);
  const bundled = onts.filter(o => o.bundled);
  $("#commonBox").innerHTML = `<h1>共通辞書を改善</h1>
    <p class="lead">共通辞書は、関係の近さを測るときに使う辞書です。課題・解決手段・用途の概念と別名を、分野別辞書に分けて持ちます。同梱のものは直せないので、複製してから育ててください。直した下書きは、検査を通して「版」として保存すると、マップで選べるようになります。マップは作ったときの版に固定され、新しい版には、マップの「辞書を改善」から切り替えます。</p>
    <h2>自分の共通辞書</h2>
    ${mine.length ? `<div class="project-list">${mine.map(m => `<div class="card"><h3>${esc(m.name)}</h3>
      <div class="meta">元: ${esc(m.based_on_label)} · 作成 ${esc(m.created)}</div>
      <div>${m.versions.length ? `<span class="tag">保存 v${m.versions[m.versions.length - 1].version}</span>` : `<span class="tag warn">未保存</span>`}</div>
      <div class="row"><button class="btn primary small" data-open="${esc(m.id)}">育てる</button><button class="btn ghost small danger" data-del="${esc(m.id)}">削除</button></div></div>`).join("")}</div>`
      : `<p class="hint">まだありません。下の同梱の共通辞書を複製して始めます。</p>`}
    <h2>同梱の共通辞書から始める</h2>
    <div class="form-row"><select id="cSrc">${[...bundled, ...onts.filter(o => o.common)].map(o => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join("")}</select>
      <input type="text" id="cName" class="wide" placeholder="新しい共通辞書の名前（例: 電池材料・社内版）"><button class="btn primary small" id="cCreate" type="button">複製して作る</button></div>
    <p class="error" id="cErr"></p>`;
  $("#commonBox").querySelectorAll("[data-open]").forEach(b => b.onclick = () => { location.hash = "#/common/" + b.dataset.open; });
  $("#commonBox").querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("この共通辞書を削除します。保存した版を使っているマップは再計算できなくなります。よろしいですか？")) return;
    await api(`/api/common/${b.dataset.del}`, {method: "DELETE"}); openCommonList();
  });
  $("#cCreate").onclick = async () => {
    try { const m = await post("/api/common", {source: $("#cSrc").value, name: $("#cName").value}); location.hash = "#/common/" + m.id; }
    catch (e) { $("#cErr").textContent = e.message; }
  };
}

async function openCommon(cid, params) {
  show("viewCommon");
  C.cid = cid;
  if (params && params.get("tab")) C.tab = params.get("tab");
  $("#topActions").innerHTML = `<button class="btn small" id="toCList" type="button">共通辞書一覧</button><button class="btn small" id="backHome2" type="button">マップ一覧</button>`;
  $("#toCList").onclick = () => { location.hash = "#/common"; };
  $("#backHome2").onclick = () => { location.hash = "#/"; };
  await loadCommon();
}
async function loadCommon() {
  C.st = await api(`/api/common/${C.cid}`);
  C.parents = null;
  const st = C.st, m = st.meta, last = m.versions.length ? m.versions[m.versions.length - 1].version : 0;
  $("#crumb").textContent = `共通辞書 · ${m.name}`;
  const tabs = {summary: "概要と保存", concepts: "概念", promote: "登録候補", corpus: "文書から足す", scope: "別名の分野制限"};
  $("#commonBox").innerHTML = `<h1>${esc(m.name)}</h1>
    <p class="hint">元: ${esc(m.based_on_label)} · 概念 ${st.n_concepts} · ${last ? `保存済み v${last}` : "未保存"}${st.dirty ? ` · <b>下書きに未保存の変更あり</b>` : ""}
      · 検査 ${st.check.errors.length ? `<span class="warn">直すところ ${st.check.errors.length}</span>` : `<span class="ok">問題なし</span>`}</p>
    <div class="ctabs" role="tablist">${Object.entries(tabs).map(([k, v]) => `<button type="button" role="tab" data-t="${k}" aria-selected="${C.tab === k}">${v}</button>`).join("")}</div>
    <div id="cBody"></div>`;
  $("#commonBox").querySelectorAll(".ctabs button").forEach(b => b.onclick = () => { C.tab = b.dataset.t; loadCommon(); });
  ({summary: renderCSummary, concepts: renderCConcepts, promote: renderCPromote, corpus: renderCCorpus, scope: renderCScope})[C.tab]();
}
function checkBox(ch) {
  return (ch.errors.length ? `<ul class="errs">${ch.errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul>` : `<p class="ok">検査: 問題ありません。</p>`) +
    (ch.warnings.length ? `<ul class="warns">${ch.warnings.map(e => `<li>${esc(e)}</li>`).join("")}</ul>` : "");
}
async function cAction(url, body) {
  try { const ch = await post(url, body); await loadCommon(); if (ch && ch.errors && ch.errors.length) alert("検査で直すところがあります:\n" + ch.errors.slice(0, 5).join("\n")); }
  catch (e) { alert(e.message); }
}
function renderCSummary() {
  const st = C.st, m = st.meta;
  $("#cBody").innerHTML = `<div class="onto-grid">
    <div class="box" style="grid-column:span 2"><h3>分野別辞書と振り分け規則</h3>
      <table class="cov"><tr><th>分野別辞書</th><th>ID</th><th>振り分け（IPC・FI の前方一致、カンマ区切り）</th><th class="num">概念</th><th></th></tr>
      ${st.modules.map(x => `<tr><td>${esc(x.label)}</td><td>${esc(x.id)}</td><td>${x.id === "upper" ? "すべての文書" : `<input type="text" data-route="${esc(x.id)}" value="${esc(x.routes.join(", "))}" style="width:100%">`}</td><td class="num">${x.concepts}</td>
        <td>${x.id === "upper" ? "" : `<button class="btn ghost small" data-saveroute="${esc(x.id)}">保存</button>${x.concepts ? "" : `<button class="btn ghost small danger" data-delmod="${esc(x.id)}">削除</button>`}`}</td></tr>`).join("")}</table>
      <p class="note">どれにも当たらない文書は、すべての分野別辞書を使います。全分野共通はいつも使います。</p>
      <div class="form-row"><input type="text" id="mId" placeholder="分野別辞書の ID（英小文字 例: semicon）"><input type="text" id="mLabel" placeholder="表示名（例: 半導体）"><input type="text" id="mRoutes" class="wide" placeholder="振り分け（例: H01L, H10B）"><button class="btn small" id="mAdd">分野別辞書を足す</button></div>
    </div>
    <div class="box"><h3>検査と保存</h3>${checkBox(st.check)}
      <div class="form-row"><input type="text" id="pNote" class="wide" placeholder="この版の変更点（例: 電池の語を 12 個登録）"></div>
      <button class="btn primary small" id="pub" ${st.check.errors.length || !st.dirty ? "disabled" : ""}>v${(m.versions.length ? m.versions[m.versions.length - 1].version : 0) + 1} として保存</button>
      ${!st.dirty ? `<p class="note">下書きは最新の版と同じです。</p>` : ""}
      <h3 style="margin-top:12px">保存した版</h3>${m.versions.length ? `<ul class="log">${m.versions.slice().reverse().map(v => `<li>v${v.version} · ${esc(v.published)} · 概念 ${v.concepts}${v.note ? " · " + esc(v.note) : ""}</li>`).join("")}</ul>` : `<p class="hint">まだありません。</p>`}
    </div></div>
    <h2>変更の記録（新しい順）</h2><ul class="log">${st.log.map(l => `<li>${esc(l.at)} · ${esc(logText(l))}</li>`).join("") || "<li>まだありません</li>"}</ul>`;
  $("#cBody").querySelectorAll("[data-saveroute]").forEach(b => b.onclick = () => cAction(`/api/common/${C.cid}/module`, {op: "edit", id: b.dataset.saveroute, routes: $(`[data-route="${b.dataset.saveroute}"]`).value}));
  $("#cBody").querySelectorAll("[data-delmod]").forEach(b => b.onclick = () => cAction(`/api/common/${C.cid}/module`, {op: "delete", id: b.dataset.delmod}));
  $("#mAdd").onclick = () => cAction(`/api/common/${C.cid}/module`, {op: "add", id: $("#mId").value, label: $("#mLabel").value, routes: $("#mRoutes").value});
  $("#pub").onclick = async () => { try { await post(`/api/common/${C.cid}/publish`, {note: $("#pNote").value}); loadCommon(); } catch (e) { alert(e.message); } };
}
function logText(l) {
  const op = {create: "作成", add: "概念を追加", edit: "概念を変更", delete: "概念を削除", publish: "保存", promote: "共通辞書に登録", promote_ignore: "登録候補を無視",
    module_add: "分野別辞書を追加", module_edit: "振り分けを変更", module_delete: "分野別辞書を削除", scope_add: "別名の分野を限定", scope_remove: "別名の分野制限を解除"}[l.op] || l.op;
  const what = l.label || l.term || (l.after && l.after.label) || l.alias || l.module || (l.version ? "v" + l.version : "") || "";
  return `${op}${what ? "「" + what + "」" : ""}${l.note ? " " + l.note : ""}`;
}
async function parentOptions() {
  if (!C.parents) C.parents = await api(`/api/common/${C.cid}/concepts`);
  const tops = {P: "課題（上位型）", M: "解決手段（上位型）", U: "用途（上位型）"};
  return Object.entries(tops).map(([k, v]) => `<option value="${k}">${v}</option>`).join("") +
    C.parents.map(c => `<option value="${esc(c.id)}">${esc(c.label)}（${esc({P: "課題", M: "手段", U: "用途"}[c.top] || "")}・${esc(c.module)}）</option>`).join("");
}
function moduleOptions(sel) {
  return C.st.modules.map(x => `<option value="${esc(x.id)}" ${x.id === sel ? "selected" : ""}>${esc(x.label)}</option>`).join("");
}
async function renderCConcepts() {
  const list = await api(`/api/common/${C.cid}/concepts?module=${encodeURIComponent(C.module)}&q=${encodeURIComponent(C.q)}`);
  const popts = await parentOptions();
  $("#cBody").innerHTML = `<div class="form-row"><select id="fMod"><option value="">すべての分野別辞書</option>${moduleOptions(C.module)}</select>
      <input type="text" id="fQ" class="wide" placeholder="表示名・別名で探す" value="${esc(C.q)}"><button class="btn small" id="fGo">探す</button><span class="hint">${list.length} 件${list.length >= 600 ? "（先頭 600 件）" : ""}</span></div>
    <div class="box" style="margin:8px 0"><h3>概念を足す</h3><div class="form-row"><select id="nMod">${moduleOptions("upper")}</select><select id="nPar" style="max-width:320px">${popts}</select>
      <input type="text" id="nLab" placeholder="表示名"><input type="text" id="nAl" class="wide" placeholder="別名（カンマ区切り）"><button class="btn primary small" id="nAdd">足す</button></div></div>
    <div class="table-wrap cand-wrap"><table class="ctable"><tr><th>概念</th><th>別名</th><th>親</th><th>分野別辞書</th><th></th></tr>
    ${list.map((c, i) => `<tr data-i="${i}"><td><b>${esc(c.label)}</b><div class="al">${esc(c.id)}</div></td><td class="al">${esc(c.aliases.join("、"))}${c.scoped.length ? `<div>${c.scoped.map(r => `<span class="mtag" title="${esc(r.reason || "")}">「${esc(r.alias)}」は ${esc(r.modules.join("・"))} だけ</span>`).join(" ")}</div>` : ""}</td>
      <td class="al">${esc(c.parent_label)}</td><td><span class="mtag">${esc(c.module)}</span></td>
      <td><button class="btn ghost small" data-act="edit">直す</button>${c.children ? "" : `<button class="btn ghost small danger" data-act="del">削除</button>`}</td></tr>`).join("")}</table></div>`;
  $("#fMod").onchange = e => { C.module = e.target.value; renderCConcepts(); };
  $("#fGo").onclick = () => { C.q = $("#fQ").value; renderCConcepts(); };
  $("#fQ").onkeydown = e => { if (e.key === "Enter") { C.q = $("#fQ").value; renderCConcepts(); } };
  $("#nAdd").onclick = () => cAction(`/api/common/${C.cid}/concept`, {op: "add", module: $("#nMod").value, parent: $("#nPar").value, label: $("#nLab").value, aliases: $("#nAl").value.split(/[,、]/)});
  $("#cBody").querySelectorAll("tr[data-i]").forEach(tr => {
    const c = list[+tr.dataset.i];
    tr.querySelector("[data-act=edit]").onclick = () => {
      const label = prompt("表示名", c.label); if (label === null) return;
      const al = prompt("別名（カンマ区切り）", c.aliases.join(", ")); if (al === null) return;
      cAction(`/api/common/${C.cid}/concept`, {op: "edit", id: c.id, label, aliases: al.split(/[,、]/)});
    };
    const d = tr.querySelector("[data-act=del]");
    if (d) d.onclick = () => { if (confirm(`「${c.label}」を消します。`)) cAction(`/api/common/${C.cid}/concept`, {op: "delete", id: c.id}); };
  });
}
async function renderCPromote() {
  $("#cBody").innerHTML = `<p class="hint">この共通辞書を使うマップの追加語で採用された語のうち、まだ共通辞書に無いものです。多くのマップで採用された語ほど上に出ます。</p><div id="prBox" class="hint">集めています…</div>`;
  const rows = await api(`/api/common/${C.cid}/promotions`);
  const popts = await parentOptions();
  if (!rows.length) { $("#prBox").innerHTML = `<p class="hint">候補はありません。この共通辞書の版を使うマップで、このマップの追加語に語を足すと出てきます。</p>`; return; }
  $("#prBox").outerHTML = `<div class="table-wrap cand-wrap"><table class="ctable"><tr><th>語</th><th class="num">マップ</th><th>採用したマップ</th><th>どう入れるか</th></tr>
    ${rows.map((r, i) => `<tr data-i="${i}"><td><b>${esc(r.term)}</b></td><td class="num">${r.n_maps}</td><td class="al">${esc(r.maps.join("、"))}</td>
      <td><div class="ops"><select data-k="mode"><option value="concept">新しい概念として</option><option value="alias" ${r.kind === "alias" ? "selected" : ""}>既存の概念の別名として</option></select>
      <select data-k="mod">${moduleOptions(r.target_module)}</select>
      <select data-k="target" style="max-width:260px">${popts.replace(`value="${r.target}"`, `value="${r.target}" selected`)}</select>
      <button class="btn small" data-act="in">入れる</button><button class="btn ghost small" data-act="ign">無視</button></div></td></tr>`).join("")}</table></div>
    <p class="note">「新しい概念として」は、選んだ概念の下に置きます（入る分野別辞書は置き場所と同じです）。「別名として」は、選んだ概念の別名にします。</p>`;
  $("#cBody").querySelectorAll("tr[data-i]").forEach(tr => {
    const r = rows[+tr.dataset.i], g = k => tr.querySelector(`[data-k=${k}]`).value;
    tr.querySelector("[data-act=in]").onclick = () => cAction(`/api/common/${C.cid}/promote`, g("mode") === "concept"
      ? {term: r.term, mode: "concept", module: g("mod"), parent: g("target")} : {term: r.term, mode: "alias", concept_id: g("target")});
    tr.querySelector("[data-act=ign]").onclick = () => cAction(`/api/common/${C.cid}/promote`, {term: r.term, mode: "ignore"});
  });
}
async function renderCCorpus() {
  const projects = await api("/api/projects");
  $("#cBody").innerHTML = `<p class="hint">新しい分野別辞書を育てます。マップの文書のうち、その分野別辞書に振り分けられる文書から、いまの下書きで拾えないよく出る語を探します。目安は対象分野の特許 300〜400 件です（それ以上はほとんど増えません）。</p>
    <div class="form-row"><select id="cpP">${projects.filter(p => (p.status || {}).stage === "done").map(p => `<option value="${esc(p.id)}">${esc(p.name)}（${p.n_docs} 件）</option>`).join("")}</select>
      <select id="cpM">${moduleOptions(C.st.modules.length > 1 ? C.st.modules[1].id : "upper")}</select><button class="btn primary small" id="cpGo">探す</button></div><div id="cpRes"></div>`;
  $("#cpGo").onclick = async () => {
    $("#cpRes").innerHTML = `<p class="hint">探しています…（数十秒）</p>`;
    const mod = $("#cpM").value;
    let res; try { res = await api(`/api/common/${C.cid}/corpus?project=${$("#cpP").value}&module=${encodeURIComponent(mod)}`); } catch (e) { $("#cpRes").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    const popts = await parentOptions();
    const secLab = {problem: "課題", solution: "解決手段", use_text: "用途"};
    $("#cpRes").innerHTML = `<p class="hint">対象の文書 ${res.n_docs} 件 · 候補 ${res.candidates.length} 語（出た文書数の多い順）</p>
      <div class="table-wrap cand-wrap"><table class="cand"><tr><th>語</th><th>段落</th><th class="num">文書</th><th>使われ方（例）</th><th>どう入れるか</th></tr>
      ${res.candidates.map((c, i) => `<tr data-i="${i}"><td class="term">${esc(c.term)}</td><td>${esc(secLab[c.section] || c.section)}</td><td class="num">${c.df}</td>
        <td class="ex">${(c.examples || []).slice(0, 1).map(x => `…${highlight(x.text, c.term)}…`).join("")}</td>
        <td><div class="ops"><select data-k="mode"><option value="concept">新しい概念</option><option value="alias">別名</option></select>
        <select data-k="target" style="max-width:240px">${popts.replace(`value="${c.top}"`, `value="${c.top}" selected`)}</select>
        <button class="btn small" data-act="in">入れる</button></div></td></tr>`).join("")}</table></div>
      <p class="note">新しい概念は分野別辞書「${esc(mod)}」に入ります。全分野共通の語と同じ表記にするときは、全分野共通の概念の下に置いてください（細分化）。</p>`;
    $("#cpRes").querySelectorAll("tr[data-i]").forEach(tr => {
      const c = res.candidates[+tr.dataset.i], g = k => tr.querySelector(`[data-k=${k}]`).value;
      tr.querySelector("[data-act=in]").onclick = async () => {
        const body = g("mode") === "concept" ? {op: "add", module: mod, parent: g("target"), label: c.term, aliases: []} : null;
        try {
          const ch = body ? await post(`/api/common/${C.cid}/concept`, body) : await post(`/api/common/${C.cid}/promote`, {term: c.term, mode: "alias", concept_id: g("target")});
          tr.classList.add("done"); C.parents = null;
          if (ch.errors && ch.errors.length) alert("検査で直すところがあります:\n" + ch.errors.slice(0, 5).join("\n"));
        } catch (e) { alert(e.message); }
      };
    });
  };
}
async function renderCScope() {
  const st = C.st;
  const mods = st.modules.filter(m => m.id !== "upper");
  $("#cBody").innerHTML = `<p class="hint">分野で意味が変わる別名は、使う分野別辞書を限ります（例: 「容量」を放電容量の別名として使うのは電池・無機材料の文書だけにし、半導体では使わない）。表示名は限れません。</p>
    <div class="box" style="margin:8px 0"><h3>分野を限る</h3><div class="form-row"><input type="text" id="sQ" placeholder="別名（例: 容量）"><button class="btn small" id="sFind">その別名を持つ概念を探す</button></div><div id="sHit"></div></div>
    <div class="table-wrap cand-wrap"><table class="ctable"><tr><th>別名</th><th>概念</th><th>使ってよい分野別辞書</th><th>理由</th><th></th></tr>
    ${st.alias_scopes.map((r, i) => `<tr data-i="${i}"><td><b>${esc(r.alias)}</b></td><td>${esc(r.label || r.concept)}</td><td>${esc(r.modules.join("・") || "（なし）")}</td><td class="al">${esc(r.reason || "")}</td><td><button class="btn ghost small" data-act="rm">解除</button></td></tr>`).join("") || `<tr><td colspan="5" class="hint">まだありません</td></tr>`}</table></div>`;
  $("#cBody").querySelectorAll("tr[data-i]").forEach(tr => {
    const r = st.alias_scopes[+tr.dataset.i];
    tr.querySelector("[data-act=rm]").onclick = () => cAction(`/api/common/${C.cid}/scope`, {op: "remove", concept: r.concept, alias: r.alias});
  });
  $("#sFind").onclick = async () => {
    const a = $("#sQ").value.trim(); if (!a) return;
    const hits = (await api(`/api/common/${C.cid}/concepts?q=${encodeURIComponent(a)}`)).filter(c => c.aliases.some(x => x.normalize("NFKC").toLowerCase() === a.normalize("NFKC").toLowerCase()));
    $("#sHit").innerHTML = hits.length ? hits.map((c, i) => `<div class="form-row" data-i="${i}"><b>${esc(c.label)}</b><span class="mtag">${esc(c.module)}</span>
      ${mods.map(m => `<label class="chk"><input type="checkbox" value="${esc(m.id)}"> ${esc(m.label)}</label>`).join("")}<input type="text" class="wide" placeholder="理由（例: 半導体では静電容量の意味）">
      <button class="btn small">この分野だけで使う</button></div>`).join("") : `<p class="hint">「${esc(a)}」を別名に持つ概念はありません（表示名は限れません）。</p>`;
    $("#sHit").querySelectorAll("[data-i]").forEach(row => {
      const c = hits[+row.dataset.i];
      row.querySelector("button").onclick = () => cAction(`/api/common/${C.cid}/scope`, {op: "add", concept: c.id, alias: a,
        modules: [...row.querySelectorAll("input[type=checkbox]:checked")].map(x => x.value), reason: row.querySelector("input[type=text]").value});
    });
  };
}

// ---------- 新しい共通辞書（ステップ） ----------
const W = {step: 1, mode: "", prompt: "", imp: null, trial: null, field: "", mods: [{label: "", hint: ""}], n: 200, sample: ""};
const WSTEPS = ["始め方", "下書きを頼む", "CSV を取り込む", "試し当て", "保存"];
function openWizard() {
  show("viewCommonNew");
  $("#crumb").textContent = "新しい共通辞書";
  $("#topActions").innerHTML = "";
  renderWizard();
}
function wizHead() {
  return `<h1>新しい共通辞書</h1>
    <ol class="steps">${WSTEPS.map((t, i) => `<li class="${i + 1 === W.step ? "on" : i + 1 < W.step ? "done" : ""}">${t}</li>`).join("")}</ol>`;
}
function wizNav(back, nextLabel, nextId = "wNext", disabled = false) {
  return `<div class="actions">${back ? `<button class="btn" id="wBack" type="button">戻る</button>` : ""}${nextLabel ? `<button class="btn primary" id="${nextId}" type="button" ${disabled ? "disabled" : ""}>${nextLabel}</button>` : ""}</div>`;
}
function renderWizard() {
  const box = $("#wizBox");
  box.className = "import wiz";
  if (W.step === 1) {
    box.innerHTML = wizHead() + `<p class="lead">共通辞書は、課題・解決手段・用途の語の辞書です。始め方を選んでください。</p>
      <div class="choice">
        <button type="button" data-m="llm" aria-pressed="${W.mode === "llm"}"><b>チャットの AI に下書きを頼む（おすすめ）</b>分野を書くとプロンプトを作ります。Claude などのチャットに貼り、返ってきた CSV を取り込みます。</button>
        <button type="button" data-m="csv" aria-pressed="${W.mode === "csv"}"><b>CSV を自分で書く</b>雛形の CSV（型・分野・中間概念・概念・別名）を Excel などで埋めて取り込みます。</button>
        <button type="button" data-m="copy" aria-pressed="${W.mode === "copy"}"><b>既存の共通辞書を複製する</b>同梱の辞書（無機材料・技術（試作）など）や、自分で作った版から始めます。</button>
      </div>` + wizNav(false, "次へ", "wNext", !W.mode);
    box.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { W.mode = b.dataset.m; renderWizard(); });
    $("#wNext").onclick = () => { if (W.mode === "copy") { location.hash = "#/common"; return; } W.step = W.mode === "llm" ? 2 : 3; renderWizard(); };
    return;
  }
  if (W.step === 2) {
    box.innerHTML = wizHead() + `<p class="lead">対象の分野を書いてください。分野をいくつかに分けると、分野で意味が変わる語（例: 「容量」）を分けて持てます。</p>
      <label class="lb" for="wField">対象の分野</label><textarea id="wField" rows="3" placeholder="例: リチウムイオン二次電池と全固体電池（電極・電解質・セパレータ・外装・充放電の管理）">${esc(W.field)}</textarea>
      <label class="lb">分野に分ける（任意）</label>
      <div id="wMods">${W.mods.map((m, i) => `<div class="form-row" data-i="${i}"><input type="text" data-k="label" placeholder="分野名（例: 電池）" value="${esc(m.label)}"><input type="text" data-k="hint" class="wide" placeholder="範囲（例: 電極・電解液・セパレータ・電池パック）" value="${esc(m.hint)}"></div>`).join("")}</div>
      <button class="btn ghost small" id="wAddMod" type="button">分野を足す</button>
      <div class="form-row" style="margin-top:10px"><label class="lb" style="margin:0">概念の数</label><select id="wN">${[100, 200, 300].map(n => `<option ${n === W.n ? "selected" : ""}>${n}</option>`).join("")}</select>
        <span class="hint">目安: 1 分野 70〜150。多すぎると AI の出力が途中で切れます。</span></div>
      <label class="lb" for="wSample">見本の特許文（任意。要約や課題の段落を数件貼ると、本文に出る書き方に寄ります）</label><textarea id="wSample" rows="4">${esc(W.sample)}</textarea>
      <div class="actions" style="justify-content:flex-start"><button class="btn" id="wMake" type="button">プロンプトを作る</button></div>
      ${W.prompt ? `<label class="lb">チャットに貼るプロンプト</label><textarea id="wPrompt" class="mono" rows="14" readonly>${esc(W.prompt)}</textarea>
        <div class="form-row"><button class="btn small" id="wCopy" type="button">コピー</button><span class="hint" id="wCopied"></span></div>
        <p class="hint">Claude などのチャットに貼って送ると、CSV が返ってきます。そのまま次のステップの欄に貼るか、.csv で保存して取り込みます。出力が途中で切れたら「続けて」と送ってください。</p>` : ""}` +
      wizNav(true, "CSV を取り込む", "wNext", !W.prompt);
    const save = () => { W.field = $("#wField").value; W.n = +$("#wN").value; W.sample = $("#wSample").value;
      W.mods = [...document.querySelectorAll("#wMods [data-i]")].map(r => ({label: r.querySelector("[data-k=label]").value, hint: r.querySelector("[data-k=hint]").value})); };
    $("#wAddMod").onclick = () => { save(); W.mods.push({label: "", hint: ""}); renderWizard(); };
    $("#wMake").onclick = async () => { save(); const r = await post("/api/common_prompt", {field: W.field, modules: W.mods, n_concepts: W.n, sample: W.sample}); W.prompt = r.prompt; renderWizard(); };
    const cp = $("#wCopy");
    if (cp) cp.onclick = async () => { try { await navigator.clipboard.writeText(W.prompt); $("#wCopied").textContent = "コピーしました"; } catch (_) { $("#wPrompt").select(); document.execCommand("copy"); $("#wCopied").textContent = "コピーしました"; } };
    $("#wBack").onclick = () => { save(); W.step = 1; renderWizard(); };
    $("#wNext").onclick = () => { save(); W.step = 3; renderWizard(); };
    return;
  }
  if (W.step === 3) {
    const r = W.imp;
    box.innerHTML = wizHead() + `<p class="lead">AI が返した CSV（または自分で書いた CSV）を取り込みます。決まりに合わない行や別名は外して、何を外したかを示します。</p>
      ${W.mode === "csv" ? `<p><a class="btn small" href="/api/common_template.csv">雛形の CSV をダウンロード</a></p>` : ""}
      <div class="form-row"><input type="file" id="wFile" accept=".csv,.txt"><span class="hint">または下の欄に貼り付け</span></div>
      <textarea id="wPaste" class="mono" rows="8" placeholder="型,分野,中間概念,概念,別名&#10;課題,電池,安全性の課題,内部短絡,短絡|ショート"></textarea>
      <div class="form-row"><input type="text" id="wName" class="wide" placeholder="共通辞書の名前（例: 電池・半導体 社内版）" value="${esc(r ? "" : W.field.slice(0, 30))}"><button class="btn" id="wImp" type="button">${r ? "取り込み直す" : "取り込む"}</button></div>
      <p class="error" id="wErr"></p>
      ${r ? `<div class="onto-grid" style="grid-template-columns:1fr 1fr">
        <div class="box"><h3>取り込んだ結果</h3><p>${r.rows} 行 → 概念 ${r.concepts}・中間概念 ${r.mids}</p>
          <table class="cov"><tr><th>層</th><th>型</th><th class="num">概念</th></tr>${r.counts.map(c => `<tr><td>${esc(c.module === "upper" ? "全分野共通" : c.module)}</td><td>${esc(c.type)}</td><td class="num">${c.n}</td></tr>`).join("")}</table>
          ${r.modules.length ? `<h3 style="margin-top:10px">分野の振り分け（IPC・FI の前方一致）</h3>${r.modules.map(m => `<div class="form-row"><span style="min-width:70px">${esc(m.label)}</span><input type="text" class="wide" data-route="${esc(m.id)}" value="${esc(m.routes.join(", "))}" placeholder="例: H01M, H02J7"></div>`).join("")}<p class="note">どれにも当たらない文書は、すべての分野を使います。</p>` : ""}
        </div>
        <div class="box"><h3>検査</h3>${checkBox(r.check)}<h3 style="margin-top:10px">外したもの・まとめたもの（${r.notes.length}）</h3>
          <ul class="warns" style="max-height:240px;overflow:auto">${r.notes.map(n => `<li>${n.row ? n.row + " 行目: " : ""}${esc(n.msg)}</li>`).join("") || "<li>ありません</li>"}</ul></div></div>` : ""}` +
      wizNav(true, "試し当てへ", "wNext", !r || r.check.errors.length > 0);
    $("#wImp").onclick = async () => {
      const f = $("#wFile").files[0], txt = $("#wPaste").value.trim();
      if (!f && !txt) { $("#wErr").textContent = "ファイルを選ぶか、CSV を貼ってください"; return; }
      const fd = new FormData();
      fd.append("file", f || new Blob([txt], {type: "text/csv"}), f ? f.name : "貼り付け.csv");
      try {
        W.imp = await api(`/api/common_import?name=${encodeURIComponent($("#wName").value)}${W.imp ? "&cid=" + W.imp.id : ""}`, {method: "POST", body: fd});
        C.cid = W.imp.id; renderWizard();
      } catch (e) { $("#wErr").textContent = e.message; }
    };
    $("#wBack").onclick = () => { W.step = W.mode === "llm" ? 2 : 1; renderWizard(); };
    $("#wNext").onclick = async () => {
      for (const inp of document.querySelectorAll("[data-route]")) await post(`/api/common/${W.imp.id}/module`, {op: "edit", id: inp.dataset.route, routes: inp.value});
      W.step = 4; renderWizard();
    };
    return;
  }
  if (W.step === 4) {
    box.innerHTML = wizHead() + `<p class="lead">作ったマップの文書に当てて、概念を抽出できなかった段落の割合を、そのマップが今使っている共通辞書と比べます（任意）。</p>
      <div class="form-row"><select id="wProj"><option value="">マップを選ぶ…</option></select><button class="btn" id="wTry" type="button">試す</button></div><div id="wTrial"></div>` +
      wizNav(true, "保存へ");
    api("/api/projects").then(ps => { $("#wProj").innerHTML += ps.filter(p => (p.status || {}).stage === "done").map(p => `<option value="${esc(p.id)}">${esc(p.name)}（${p.n_docs} 件）</option>`).join(""); });
    $("#wTry").onclick = async () => {
      const pid = $("#wProj").value; if (!pid) return;
      $("#wTrial").innerHTML = `<p class="hint">当てています…</p>`;
      const t = await api(`/api/common/${W.imp.id}/trial?project=${pid}`);
      const pct = v => v == null ? "—" : (v * 100).toFixed(1) + "%";
      $("#wTrial").innerHTML = `<table class="cov" style="max-width:640px"><tr><th>段落</th><th class="num">文書</th><th class="num">今の共通辞書</th><th class="num">この下書き</th></tr>
        ${Object.keys(t.draft).map(k => `<tr><td>${esc(t.draft[k].label)}</td><td class="num">${t.draft[k].n}</td><td class="num">${pct(t.current[k].rate)}</td><td class="num ${t.draft[k].rate != null && t.current[k].rate != null && t.draft[k].rate < t.current[k].rate ? "better" : ""}">${pct(t.draft[k].rate)}</td></tr>`).join("")}</table>
        <p class="note">文書の振り分け: ${Object.entries(t.routes).map(([k, v]) => `${esc(k)} ${v} 件`).join("、")}。低いほど、その段落の中身を拾えています。</p>
        <div class="box" style="margin-top:8px"><h3>抽出できない語を足す（おすすめ）</h3>
          <p class="note">AI の下書きは、本文によく出る書き方（「電池」「二次電池」「基板」など）が抜けやすく、そのままだと概念を抽出できない段落が多くなります（電池・半導体の試しで、用途の段落の 6〜7 割で概念を抽出できませんでした）。保存の前に、このマップの文書から拾えない語を見て足してください。</p>
          <a class="btn small" href="#/common/${esc(W.imp.id)}?tab=corpus">「文書から足す」を開く（下書きのまま。保存は「概要と保存」から）</a></div>`;
    };
    $("#wBack").onclick = () => { W.step = 3; renderWizard(); };
    $("#wNext").onclick = () => { W.step = 5; renderWizard(); };
    return;
  }
  if (W.step === 5) {
    box.innerHTML = wizHead() + `<p class="lead">名前を付けて v1 として保存すると、新しいマップときに選べるようになります。保存したあとも「共通辞書一覧」から育てられます。</p>
      <label class="lb" for="wPubName">名前</label><input type="text" id="wPubName" class="wide" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px">
      <label class="lb" for="wNote">メモ（任意）</label><input type="text" id="wNote" style="width:100%;border:1px solid var(--line);border-radius:6px;padding:6px 9px" placeholder="例: Claude に下書きさせ、外したもの 12 件">
      <p class="error" id="wErr2"></p>` + wizNav(true, "v1 として保存");
    api(`/api/common/${W.imp.id}`).then(st => { $("#wPubName").value = st.meta.name; });
    $("#wBack").onclick = () => { W.step = 4; renderWizard(); };
    $("#wNext").onclick = async () => {
      try {
        await post(`/api/common/${W.imp.id}/rename`, {name: $("#wPubName").value});
        await post(`/api/common/${W.imp.id}/publish`, {note: $("#wNote").value});
        const id = W.imp.id;
        Object.assign(W, {step: 1, mode: "", prompt: "", imp: null, trial: null});
        $("#wizBox").innerHTML = wizHead() + `<p class="lead">保存しました。</p><div class="actions" style="justify-content:flex-start"><a class="btn primary" href="#/new">この共通辞書で新しいマップ</a><a class="btn" href="#/common/${esc(id)}">共通辞書を改善</a></div>`;
      } catch (e) { $("#wErr2").textContent = e.message; }
    };
  }
}

// ---------- 統計（取り込んだ特許の集計と、共通辞書の概念の集計） ----------
// 集計は画面側で行う（絞り込みにすぐ追従させるため）。サーバーは文書ごとの属性と概念だけを返す（/api/projects/<id>/stats）。
const ST = {pid: null, data: null, tab: "all", f: {usage: "", y0: "", y1: "", app: ""}, gen: false, growRole: "solution", sets: {},
  cmp: {apps: []}, gap: {pair: "pm"}, out: {min: 5}};
const ST_ROLE = [["problem", "課題"], ["solution", "解決手段"], ["use", "用途"]];
const ST_SEQ = ["#eef4fc", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281"];  // 一色の濃淡（件数の多さ）
const ST_SERIES = ["var(--c1)", "var(--c2)", "var(--c3)"];  // 出願人比較の系列色（最大 3 者）
const ST_TABS = [["all", "全体"], ["compare", "出願人比較"], ["gaps", "空白探索"], ["outliers", "外れ値の特許"]];
const ST_LEAD = {
  all: ont => `出願年・出願人・用途・分類と、共通辞書（${ont}）で拾った課題・解決手段・用途の概念を数えます。棒やマスを押すと、該当する特許を右に出します。`,
  compare: () => "選んだ 2〜3 者の特許を、課題・解決手段・用途の概念の割合で並べます。重なる領域と、その出願人だけの領域が分かります。",
  gaps: () => "課題と解決手段（または解決手段と用途）の組み合わせで、出願が無い組・薄い組を探します。周りが混んでいるのに空いている組ほど上に出ます。",
  outliers: () => "ふだん一緒に出ない概念どうしが結び付いた特許を、珍しさの順に並べます。新しい組み合わせの手がかりになる一方、辞書の拾い間違いも混ざります。",
};

async function openStats(pid, params = new URLSearchParams()) {
  show("viewStats");
  $("#stDrawer").hidden = true;
  if (ST.pid !== pid || !ST.data) {
    $("#stGrid").innerHTML = `<p class="hint">集計しています…</p>`;
    $("#stKpis").innerHTML = ""; $("#stFilter").innerHTML = ""; $("#stTabs").innerHTML = "";
    try { ST.data = await api(`/api/projects/${pid}/stats`); } catch (e) { $("#stGrid").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    ST.pid = pid; ST.f = {usage: "", y0: "", y1: "", app: ""}; ST.cmp = {apps: []};
  }
  const t = params.get("tab");
  ST.tab = ST_TABS.some(([k]) => k === t) ? t : "all";
  const D = ST.data;
  $("#crumb").textContent = D.name + " · 統計";
  $("#topActions").innerHTML = "";
  $("#stLead").textContent = `${D.name}。${ST_LEAD[ST.tab](D.ontology || "—")}`;
  $("#stToMap").onclick = () => { location.hash = "#/map/" + pid; };
  $("#stTabs").innerHTML = ST_TABS.map(([k, lab]) => `<button type="button" role="tab" data-tab="${k}" aria-selected="${ST.tab === k}">${lab}</button>`).join("");
  $("#stTabs").querySelectorAll("button").forEach(b => b.onclick = () => { location.hash = `#/stats/${pid}?tab=${b.dataset.tab}`; });
  renderStFilter();
  renderStats();
}

// 画面の幅が変わったら描き直す（年の棒グラフは画面の幅で描くため）
window.addEventListener("resize", () => { if (!$("#viewStats").hidden && ST.data) { clearTimeout(ST.rt); ST.rt = setTimeout(renderStats, 200); } });
function stDocs() {
  const f = ST.f, y0 = +f.y0 || -Infinity, y1 = +f.y1 || Infinity, app = norm(f.app);
  return ST.data.docs.filter(d => (!f.usage || d.usage.includes(f.usage)) && (d.year == null ? !(f.y0 || f.y1) : d.year >= y0 && d.year <= y1)
    && (!app || d.applicants.some(a => norm(a).includes(app))));
}
const stLabel = c => (ST.data.concepts[c] || {}).label || c;
const stGeneric = c => !!(ST.data.concepts[c] || {}).generic;
function stCount(docs, key) {  // 1 件に複数付く属性（出願人・用途・IPC・概念）を、文書の数で数える
  const m = new Map();
  for (const d of docs) for (const v of new Set(d[key] || [])) { if (!m.has(v)) m.set(v, []); m.get(v).push(d.id); }
  return [...m.entries()].map(([k, ids]) => ({k, ids, n: ids.length})).sort((a, b) => b.n - a.n || String(a.k).localeCompare(String(b.k), "ja"));
}

function renderStFilter() {
  const D = ST.data, f = ST.f;
  const usages = stCount(D.docs, "usage");
  const years = D.docs.map(d => d.year).filter(y => y != null);
  $("#stFilter").innerHTML = `
    <label>用途<select id="stUsage"><option value="">すべて</option>${usages.map(u => `<option value="${esc(u.k)}" ${u.k === f.usage ? "selected" : ""}>${esc(u.k)}（${u.n}）</option>`).join("")}</select></label>
    <label>出願年<input id="stY0" type="number" placeholder="${years.length ? Math.min(...years) : ""}" value="${esc(f.y0)}"> 〜 <input id="stY1" type="number" placeholder="${years.length ? Math.max(...years) : ""}" value="${esc(f.y1)}"></label>
    <label>出願人<input id="stApp" type="text" list="stAppList" placeholder="名前の一部" value="${esc(f.app)}"></label>
    <datalist id="stAppList">${stCount(D.docs, "applicants").slice(0, 200).map(a => `<option value="${esc(a.k)}">`).join("")}</datalist>
    <label class="chk"><input type="checkbox" id="stGen" ${ST.gen ? "checked" : ""}> 一般的な概念も数える</label>
    <button class="btn ghost small" id="stClear" type="button">条件を消す</button>`;
  const upd = () => { ST.f = {usage: $("#stUsage").value, y0: $("#stY0").value, y1: $("#stY1").value, app: $("#stApp").value}; ST.gen = $("#stGen").checked; renderStats(); };
  for (const id of ["#stUsage", "#stY0", "#stY1", "#stApp", "#stGen"]) $(id).onchange = upd;
  $("#stApp").oninput = () => { clearTimeout(ST.t); ST.t = setTimeout(upd, 250); };
  $("#stClear").onclick = () => { ST.f = {usage: "", y0: "", y1: "", app: ""}; ST.gen = false; renderStFilter(); renderStats(); };
}

// 部品: 横棒のランキング（左が基線。値と割合を棒の右に）
function stBars(rows, total, opt = {}) {
  if (!rows.length) return `<p class="hint">該当なし</p>`;
  const max = Math.max(...rows.map(r => r.n));
  return `<ol class="st-bars">${rows.map(r => {
    const key = stSet(r.ids, opt.title ? opt.title(r) : r.label);
    const pct = total ? Math.round(r.n / total * 1000) / 10 : 0;
    return `<li data-set="${key}" data-tip="${esc(`${r.label}：${r.n} 件（${pct}%）`)}" class="${r.muted ? "muted" : ""}">
      <span class="lb" title="${esc(r.label)}">${esc(r.label)}</span>
      <span class="bw"><span class="b" style="width:${(r.n / max * 100).toFixed(1)}%"></span></span>
      <span class="v">${r.n}<small> ${pct}%</small></span></li>`;
  }).join("")}</ol>`;
}
function stSet(ids, title) { const k = "s" + Object.keys(ST.sets).length; ST.sets[k] = {ids, title}; return k; }

// 部品: 年ごとの縦棒（上端だけ角を丸め、基線は四角のまま）
function stYears(docs) {
  const ys = docs.map(d => d.year).filter(y => y != null);
  if (!ys.length) return `<p class="hint">出願年の列がありません</p>`;
  const y0 = Math.min(...ys), y1 = Math.max(...ys), by = new Map();
  for (const d of docs) if (d.year != null) { if (!by.has(d.year)) by.set(d.year, []); by.get(d.year).push(d.id); }
  const n = y1 - y0 + 1, W = Math.max(560, ($("#stGrid").clientWidth || 920) - 40), H = 230, L = 36, B = 26, T = 14, bw = Math.min(46, (W - L) / n * 0.68);
  const max = Math.max(...[...by.values()].map(v => v.length)), step = niceStep(max);
  const top = Math.ceil(max / step) * step, sx = i => L + (i + 0.5) * (W - L) / n, sy = v => H - B - (H - B - T) * v / top;
  let g = "";
  for (let v = 0; v <= top; v += step) g += `<line x1="${L}" x2="${W}" y1="${sy(v)}" y2="${sy(v)}" class="grid"/><text x="${L - 6}" y="${sy(v) + 4}" class="ax" text-anchor="end">${v}</text>`;
  for (let i = 0; i < n; i++) {
    const y = y0 + i, ids = by.get(y) || [], x = sx(i) - bw / 2, h = sy(0) - sy(ids.length), r = Math.min(4, h / 2, bw / 2);
    if (ids.length) {
      const key = stSet(ids, `${y} 年に出願された特許`);
      g += `<path data-set="${key}" data-tip="${y} 年：${ids.length} 件" class="bar" d="M${x},${sy(0)} V${sy(ids.length) + r} Q${x},${sy(ids.length)} ${x + r},${sy(ids.length)} H${x + bw - r} Q${x + bw},${sy(ids.length)} ${x + bw},${sy(ids.length) + r} V${sy(0)} Z"/>`;
    }
    if (n <= 16 || i % Math.ceil(n / 16) === 0 || i === n - 1) g += `<text x="${sx(i)}" y="${H - 8}" class="ax" text-anchor="middle">${y}</text>`;
  }
  return `<svg class="st-years" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="出願年ごとの件数">${g}<line x1="${L}" x2="${W}" y1="${sy(0)}" y2="${sy(0)}" class="base"/></svg>`;
}
function niceStep(max) { const raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(Math.max(1, raw)))); return [1, 2, 5, 10].map(m => m * p).find(s => s >= raw) || p * 10; }

// 部品: 行×列の濃淡表（2 つの属性を両方持つ文書の数）
function stHeat(docs, rowKey, colKey, rows, cols, rowLab, colLab, titleOf) {
  if (!rows.length || !cols.length) return `<p class="hint">該当なし</p>`;
  const cell = new Map();
  for (const d of docs) for (const r of new Set(d[rowKey])) if (rows.includes(r)) for (const c of new Set(d[colKey])) if (cols.includes(c)) { const k = r + "\u0001" + c; if (!cell.has(k)) cell.set(k, []); cell.get(k).push(d.id); }
  const max = Math.max(1, ...[...cell.values()].map(v => v.length));
  const head = `<tr><th></th>${cols.map(c => `<th class="ch" title="${esc(colLab(c))}"><span>${esc(colLab(c))}</span></th>`).join("")}</tr>`;
  const body = rows.map(r => `<tr><th class="rh" title="${esc(rowLab(r))}">${esc(rowLab(r))}</th>${cols.map(c => {
    const ids = cell.get(r + "\u0001" + c) || [];
    if (!ids.length) return `<td class="z"></td>`;
    const lv = Math.min(ST_SEQ.length - 1, Math.ceil(ids.length / max * (ST_SEQ.length - 1)));
    const key = stSet(ids, titleOf(r, c));
    return `<td data-set="${key}" data-tip="${esc(`${rowLab(r)} × ${colLab(c)}：${ids.length} 件`)}" style="background:${ST_SEQ[lv]};color:${lv >= 4 ? "#fff" : "var(--ink)"}">${ids.length}</td>`;
  }).join("")}</tr>`).join("");
  const lg = `<div class="st-scale"><span>少</span>${ST_SEQ.slice(1).map(c => `<i style="background:${c}"></i>`).join("")}<span>多（最大 ${max} 件）</span></div>`;
  return `<div class="st-heat-wrap"><table class="st-heat">${head}${body}</table></div>${lg}`;
}

// 部品: 伸びている・減っている概念（直近 5 年の割合 − それより前の割合。ポイント差）
function stGrowth(docs, role) {
  const ys = docs.map(d => d.year).filter(y => y != null);
  if (ys.length < 20) return `<p class="hint">出願年のある文書が少ないため、比べられません（20 件以上で表示）。</p>`;
  const y1 = Math.max(...ys), cut = y1 - 4;
  const rec = docs.filter(d => d.year != null && d.year >= cut), old = docs.filter(d => d.year != null && d.year < cut);
  if (rec.length < 10 || old.length < 10) return `<p class="hint">直近 5 年（${cut}〜${y1}）とそれより前の、どちらかの件数が 10 件未満のため比べられません。</p>`;
  const rows = stCount(docs.filter(d => d.year != null), role).filter(r => r.n >= 5 && (ST.gen || !stGeneric(r.k))).map(r => {
    const s = new Set(r.ids), a = rec.filter(d => s.has(d.id)).length / rec.length, b = old.filter(d => s.has(d.id)).length / old.length;
    return {k: r.k, ids: r.ids, d: (a - b) * 100, a: a * 100, b: b * 100};
  });
  const up = rows.filter(r => r.d > 0).sort((p, q) => q.d - p.d).slice(0, 8), dn = rows.filter(r => r.d < 0).sort((p, q) => p.d - q.d).slice(0, 5);
  const list = [...up, ...dn.reverse()];
  if (!list.length) return `<p class="hint">5 件以上の概念がありません。</p>`;
  const m = Math.max(...list.map(r => Math.abs(r.d)));
  return `<p class="st-sub">直近 5 年（${cut}〜${y1}、${rec.length} 件）とそれより前（${old.length} 件）で、その概念を含む文書の割合を比べた差です。</p>
    <ol class="st-div">${list.map(r => {
      const key = stSet(r.ids, `「${stLabel(r.k)}」を含む特許`);
      const w = (Math.abs(r.d) / m * 50).toFixed(1);
      return `<li data-set="${key}" data-tip="${esc(`${stLabel(r.k)}：直近 ${r.a.toFixed(1)}% ／ それより前 ${r.b.toFixed(1)}%`)}">
        <span class="lb" title="${esc(stLabel(r.k))}">${esc(stLabel(r.k))}</span>
        <span class="dw"><span class="mid"></span><span class="d ${r.d > 0 ? "up" : "dn"}" style="${r.d > 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`}"></span></span>
        <span class="v ${r.d > 0 ? "up" : "dn"}">${r.d > 0 ? "+" : "−"}${Math.abs(r.d).toFixed(1)} pt</span></li>`;
    }).join("")}</ol>
    <div class="st-legend"><span><i class="up"></i>直近で増えた</span><span><i class="dn"></i>直近で減った</span></div>`;
}

function renderStats() {
  ST.sets = {};
  const docs = stDocs(), n = docs.length, D = ST.data;
  const okC = c => ST.gen || !stGeneric(c);
  const apps = stCount(docs, "applicants");
  const ys = docs.map(d => d.year).filter(y => y != null);
  const withC = docs.filter(d => ST_ROLE.some(([k]) => d[k].length)).length;
  $("#stKpis").innerHTML = [
    ["特許", `${n}`, `件${n !== D.n ? `（全 ${D.n} 件中）` : ""}`],
    ["出願人", `${apps.length}`, "者"],
    ["出願年", ys.length ? `${Math.min(...ys)}〜${Math.max(...ys)}` : "—", ""],
    ["辞書で概念を拾えた特許", n ? `${Math.round(withC / n * 100)}` : "—", `%（${withC} 件）`],
  ].map(([k, v, u]) => `<div class="st-kpi"><div class="k">${k}</div><div class="v">${v}<small>${u}</small></div></div>`).join("");
  if (!n) { $("#stGrid").innerHTML = `<p class="hint">条件に合う特許がありません。</p>`; return; }
  const grid = $("#stGrid");
  grid.innerHTML = ST.tab === "compare" ? renderStCompare(docs) : ST.tab === "gaps" ? renderStGaps(docs) : ST.tab === "outliers" ? renderStOutliers(docs) : renderStAll(docs);
  // 各タブの中の切り替え（伸びている概念の役割・比べる出願人・空白の組み合わせ・外れ値の下限）
  grid.querySelectorAll(".st-tabs button[data-role]").forEach(b => b.onclick = () => { ST.growRole = b.dataset.role; renderStats(); });
  grid.querySelectorAll(".st-tabs button[data-gap]").forEach(b => b.onclick = () => { ST.gap.pair = b.dataset.gap; renderStats(); });
  grid.querySelectorAll("select[data-cmp]").forEach(s => s.onchange = () => { const a = [...ST.cmp.apps]; a[+s.dataset.cmp] = s.value; ST.cmp.apps = a; renderStats(); });
  const om = grid.querySelector("#stOutMin");
  if (om) om.onchange = () => { ST.out.min = +om.value; renderStats(); };
  bindStInteract();
}
const stCard = (title, sub, body, cls = "") => `<section class="st-card ${cls}"><h2>${title}</h2>${sub ? `<p class="st-sub">${sub}</p>` : ""}${body}</section>`;
const stOk = c => ST.gen || !stGeneric(c);
const stDocLine = d => `${esc(d.applicants.slice(0, 2).join("、"))}${d.applicants.length > 2 ? " ほか" : ""} · ${d.year ?? "—"} · ${esc(d.pub)}`;

// タブ「全体」: ランキングと推移
function renderStAll(docs) {
  const n = docs.length, apps = stCount(docs, "applicants");
  const concept = role => stCount(docs, role).filter(r => stOk(r.k)).slice(0, 12).map(r => ({...r, label: stLabel(r.k), muted: stGeneric(r.k)}));
  const topApps = apps.slice(0, 10).map(r => r.k);
  const topM = stCount(docs, "solution").filter(r => stOk(r.k)).slice(0, 10).map(r => r.k);
  const topP = stCount(docs, "problem").filter(r => stOk(r.k)).slice(0, 10).map(r => r.k);
  return [
    stCard("出願件数の推移", "出願年ごとの件数です。直近の 1〜2 年は、まだ公開されていない出願があるため少なく見えます。", stYears(docs), "wide"),
    stCard("出願人ランキング", `上位 15 者（全 ${apps.length} 者）。共同出願は、それぞれの出願人に 1 件ずつ数えます。`, stBars(apps.slice(0, 15).map(r => ({...r, label: r.k})), n, {title: r => `${r.k} の特許`})),
    stCard("用途の内訳", "IPC から判定した用途です。1 件に複数の用途が付くため、合計は件数を超えます。", stBars(stCount(docs, "usage").map(r => ({...r, label: r.k})), n, {title: r => `用途「${r.k}」の特許`})),
    stCard("課題・解決手段・用途のランキング（共通辞書）", `技術関係に出た概念を、それを含む特許の数で並べました（各 12 位まで）。${ST.gen ? "灰色はどの特許にも出る一般的な概念（全体の 20% 超）です。" : "どの特許にも出る一般的な概念（全体の 20% 超）は除いています。"}`,
      `<div class="st-three">${ST_ROLE.map(([k, lab]) => `<div><h3>${lab}</h3>${stBars(concept(k), n, {title: r => `${lab}「${r.label}」を含む特許`})}</div>`).join("")}</div>`, "wide"),
    stCard("伸びている概念", "", `<div class="tabs st-tabs" role="tablist">${ST_ROLE.map(([k, lab]) => `<button type="button" role="tab" data-role="${k}" aria-selected="${ST.growRole === k}">${lab}</button>`).join("")}</div>${stGrowth(docs, ST.growRole)}`),
    stCard("IPC（メイングループ）ランキング", "上位 15。1 件に複数の分類が付くため、合計は件数を超えます。", stBars(stCount(docs, "ipc").slice(0, 15).map(r => ({...r, label: r.k})), n, {title: r => `IPC ${r.k} の特許`})),
    stCard("出願人 × 解決手段", "上位 10 者と、上位 10 の解決手段。マスの数字は、その出願人の特許のうち、その手段を含むものの件数です。", stHeat(docs, "applicants", "solution", topApps, topM, a => a, stLabel, (a, c) => `${a} ×「${stLabel(c)}」`), "wide"),
    stCard("課題 × 解決手段", "上位 10 の課題と上位 10 の解決手段。マスの数字は、両方を含む特許の件数です。", stHeat(docs, "problem", "solution", topP, topM, stLabel, stLabel, (p, c) => `「${stLabel(p)}」×「${stLabel(c)}」`), "wide"),
  ].join("");
}

// ---------- タブ「出願人比較」: 2〜3 者の概念の割合を並べる ----------
function renderStCompare(docs) {
  const apps = stCount(docs, "applicants");
  if (apps.length < 2) return stCard("出願人比較", "", `<p class="hint">条件に合う特許の出願人が 2 者未満のため、比べられません。</p>`, "wide");
  const names = new Set(apps.map(a => a.k));
  let sel = [...new Set(ST.cmp.apps.filter(a => a && names.has(a)))];
  for (const a of apps) { if (sel.length >= 2) break; if (!sel.includes(a.k)) sel.push(a.k); }  // 選ばれていなければ上位 2 者
  ST.cmp.apps = sel;
  const sets = sel.map(a => docs.filter(d => d.applicants.includes(a)));
  const pick = `<div class="st-cmp-sel">${[0, 1, 2].map(i => `<label><i style="background:${ST_SERIES[i]}"></i><select data-cmp="${i}">${i === 2 ? `<option value="" ${sel[2] ? "" : "selected"}>（3 者目なし）</option>` : ""}${apps.map(a => `<option value="${esc(a.k)}" ${sel[i] === a.k ? "selected" : ""}>${esc(a.k)}（${a.n}）</option>`).join("")}</select></label>`).join("")}</div>`;
  // 概要
  const ov = `<div class="st-cmp-ov" style="grid-template-columns:repeat(${sel.length},minmax(0,1fr))">${sel.map((a, i) => {
    const S = sets[i], ys = S.map(d => d.year).filter(y => y != null), us = stCount(S, "usage").slice(0, 2), ms = stCount(S, "solution").filter(r => stOk(r.k)).slice(0, 3);
    const key = stSet(S.map(d => d.id), `${a} の特許`);
    return `<div class="st-cmp-card" style="border-top-color:${ST_SERIES[i]}"><b>${esc(a)}</b><div class="v" data-set="${key}" data-tip="${esc(`${a}：${S.length} 件`)}">${S.length}<small> 件</small></div>
      <div class="hint">出願年 ${ys.length ? `${Math.min(...ys)}〜${Math.max(...ys)}` : "—"}</div>
      <div class="hint">用途: ${us.map(u => esc(u.k)).join("、") || "—"}</div>
      <div class="hint">主な手段: ${ms.map(m => esc(stLabel(m.k))).join("、") || "—"}</div></div>`;
  }).join("")}</div>`;
  // 概念の重なり（各役割の概念の割合を並べたベクトルのコサイン類似度）
  const vec = S => { const m = new Map(); for (const d of S) for (const [k] of ST_ROLE) for (const c of new Set(d[k])) if (stOk(c)) m.set(k + c, (m.get(k + c) || 0) + 1 / S.length); return m; };
  const cos = (a, b) => { let dot = 0, na = 0, nb = 0; for (const [k, v] of a) { na += v * v; if (b.has(k)) dot += v * b.get(k); } for (const v of b.values()) nb += v * v; return na && nb ? dot / Math.sqrt(na * nb) : 0; };
  const vs = sets.map(vec), pairs = [];
  for (let i = 0; i < sel.length; i++) for (let j = i + 1; j < sel.length; j++) pairs.push({a: sel[i], b: sel[j], v: cos(vs[i], vs[j])});
  const overlap = `<ol class="st-bars st-ov">${pairs.map(p => `<li><span class="lb">${esc(p.a)} と ${esc(p.b)}</span><span class="bw"><span class="b" style="width:${(p.v * 100).toFixed(1)}%"></span></span><span class="v">${(p.v * 100).toFixed(0)}<small>%</small></span></li>`).join("")}</ol>`;
  // 役割ごとの割合比較（選んだ出願人のどこかで割合が高い概念の上位 10）
  const roleCmp = ST_ROLE.map(([k, lab]) => {
    const share = sets.map(S => new Map(stCount(S, k).filter(r => stOk(r.k)).map(r => [r.k, r])));
    const all = new Set(); share.forEach(m => m.forEach((_, c) => all.add(c)));
    const rows = [...all].map(c => ({c, s: share.map((m, i) => (m.get(c)?.n || 0) / sets[i].length), ids: share.map(m => m.get(c)?.ids || [])}));
    rows.forEach(r => { r.max = Math.max(...r.s); });
    const top = rows.sort((a, b) => b.max - a.max).slice(0, 10), M = Math.max(0.01, ...top.map(r => r.max));
    if (!top.length) return `<div><h3>${lab}</h3><p class="hint">該当なし</p></div>`;
    return `<div><h3>${lab}</h3><ol class="st-grp">${top.map(r => `<li><span class="lb" title="${esc(stLabel(r.c))}">${esc(stLabel(r.c))}</span><span class="gw">${r.s.map((v, i) => {
      const key = stSet(r.ids[i], `${sel[i]} の${lab}「${stLabel(r.c)}」を含む特許`);
      return `<span class="g" data-set="${key}" data-tip="${esc(`${sel[i]}：「${stLabel(r.c)}」${(v * 100).toFixed(0)}%（${r.ids[i].length} 件）`)}"><span class="b" style="width:${(v / M * 100).toFixed(1)}%;background:${ST_SERIES[i]}"></span><span class="pv">${v ? (v * 100).toFixed(0) + "%" : ""}</span></span>`;
    }).join("")}</span></li>`).join("")}</ol></div>`;
  }).join("");
  // 重なりと独自領域（2 件以上ある概念を「持つ」とみなす）
  const su = ST_ROLE.map(([k, lab]) => {
    const cnt = sets.map(S => new Map(stCount(S, k).filter(r => stOk(r.k)).map(r => [r.k, r])));
    const has = (m, c) => (m.get(c)?.n || 0) >= 2;
    const all = new Set(); cnt.forEach(m => m.forEach((_, c) => all.add(c)));
    const pill = (c, ids, title, style) => `<span class="st-pill" data-set="${stSet(ids, title)}" data-tip="${esc(`${stLabel(c)}：${ids.length} 件`)}" style="${style}">${esc(stLabel(c))}<small>${ids.length}</small></span>`;
    const lim = (xs, f) => xs.slice(0, 8).map(f).join("") + (xs.length > 8 ? `<span class="hint">ほか ${xs.length - 8}</span>` : "");
    const shared = [...all].filter(c => cnt.every(m => has(m, c))).sort((a, b) => cnt.reduce((s, m) => s + m.get(b).n, 0) - cnt.reduce((s, m) => s + m.get(a).n, 0));
    const rowsHtml = [`<div class="st-su-row"><span class="k">共通</span><span>${shared.length ? lim(shared, c => pill(c, cnt.flatMap(m => m.get(c).ids), `${lab}「${stLabel(c)}」を含む特許（${sel.join("・")}）`, "")) : `<span class="hint">なし</span>`}</span></div>`];
    sel.forEach((a, i) => {
      const only = [...all].filter(c => has(cnt[i], c) && cnt.every((m, j) => j === i || !m.get(c))).sort((x, y) => cnt[i].get(y).n - cnt[i].get(x).n);
      rowsHtml.push(`<div class="st-su-row"><span class="k" style="color:${ST_SERIES[i]}">${esc(a)} だけ</span><span>${only.length ? lim(only, c => pill(c, cnt[i].get(c).ids, `${a} の${lab}「${stLabel(c)}」を含む特許`, `border-color:${ST_SERIES[i]}`)) : `<span class="hint">なし</span>`}</span></div>`);
    });
    return `<div class="st-su"><h3>${lab}</h3>${rowsHtml.join("")}</div>`;
  }).join("");
  return [
    stCard("比べる出願人", "2 者または 3 者を選びます。候補は、今の条件に合う特許の出願人です。", pick, "wide"),
    stCard("概要", "", ov, "wide"),
    stCard("概念の重なり", "課題・解決手段・用途の概念の割合を並べ、向きの近さ（コサイン類似度）で測りました。100% なら同じ構成、0% なら共通の概念がありません。", overlap),
    stCard("出願件数の推移", "出願年ごとの件数です。直近の 1〜2 年は、未公開の出願があるため少なく見えます。", stLines(sets, sel)),
    stCard("課題・解決手段・用途の割合", "それぞれの出願人の特許のうち、その概念を含むものの割合です。選んだ出願人のどこかで割合が高い概念の上位 10 を並べました。", `<div class="st-three">${roleCmp}</div>`, "wide"),
    stCard("重なりと独自領域", "2 件以上ある概念を「持つ」とみなします。「共通」は選んだ全員が持つ概念、「〜だけ」はその出願人だけが持ち、ほかの出願人には 1 件も無い概念です。", su, "wide"),
  ].join("");
}

// 部品: 出願人ごとの年推移（折れ線。点を押すとその年のその出願人の特許）
function stLines(sets, names) {
  const all = sets.flat(), ys = all.map(d => d.year).filter(y => y != null);
  if (!ys.length) return `<p class="hint">出願年の列がありません</p>`;
  const y0 = Math.min(...ys), y1 = Math.max(...ys), n = y1 - y0 + 1;
  const W = Math.max(400, Math.floor((($("#stGrid").clientWidth || 920) - 14) / 2) - 40), H = 210, L = 36, R = 18, B = 26, T = 14;
  const series = sets.map(S => { const by = new Map(); for (const d of S) if (d.year != null) { if (!by.has(d.year)) by.set(d.year, []); by.get(d.year).push(d.id); } return by; });
  const max = Math.max(1, ...series.flatMap(by => [...by.values()].map(v => v.length))), step = niceStep(max), top = Math.ceil(max / step) * step;
  const sx = i => L + (n === 1 ? (W - L - R) / 2 : 8 + i * (W - L - R - 16) / (n - 1)), sy = v => H - B - (H - B - T) * v / top;
  let g = "";
  for (let v = 0; v <= top; v += step) g += `<line x1="${L}" x2="${W}" y1="${sy(v)}" y2="${sy(v)}" class="grid"/><text x="${L - 6}" y="${sy(v) + 4}" class="ax" text-anchor="end">${v}</text>`;
  for (let i = 0; i < n; i++) if (n <= 12 || i % Math.ceil(n / 12) === 0 || i === n - 1) g += `<text x="${sx(i)}" y="${H - 8}" class="ax" text-anchor="middle">${y0 + i}</text>`;
  series.forEach((by, s) => {
    const pts = []; for (let i = 0; i < n; i++) pts.push([sx(i), sy((by.get(y0 + i) || []).length)]);
    g += `<path class="ln" style="stroke:${ST_SERIES[s]}" d="M${pts.map(p => p.join(",")).join(" L")}"/>`;
    for (let i = 0; i < n; i++) { const ids = by.get(y0 + i) || []; if (!ids.length) continue;
      g += `<circle class="pt" style="fill:${ST_SERIES[s]}" cx="${pts[i][0]}" cy="${pts[i][1]}" r="4" data-set="${stSet(ids, `${names[s]} が ${y0 + i} 年に出願した特許`)}" data-tip="${esc(`${names[s]} ${y0 + i} 年：${ids.length} 件`)}"/>`; }
  });
  const lg = `<div class="st-legend">${names.map((a, i) => `<span><i style="background:${ST_SERIES[i]}"></i>${esc(a)}</span>`).join("")}</div>`;
  return `<svg class="st-years st-lines" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="出願人ごとの出願年の件数">${g}<line x1="${L}" x2="${W}" y1="${sy(0)}" y2="${sy(0)}" class="base"/></svg>${lg}`;
}

// ---------- タブ「空白探索」: 期待より出願の無い・薄い組 ----------
// 期待件数 = 行の件数 × 列の件数 ÷ 全件数（2 つの概念が独立に現れるなら出るはずの件数）
const ST_GAP = {pm: ["problem", "solution", "課題", "解決手段"], mu: ["solution", "use", "解決手段", "用途（辞書）"], mi: ["solution", "usage", "解決手段", "用途（IPC）"]};
function renderStGaps(docs) {
  const N = docs.length, [rk, ck, rl, cl] = ST_GAP[ST.gap.pair];
  const labOf = key => key === "usage" ? (x => x) : stLabel, labR = labOf(rk), labC = labOf(ck);
  const side = key => stCount(docs, key).filter(r => (key === "usage" || stOk(r.k)) && r.n >= 5).slice(0, 15);
  const rows = side(rk), cols = side(ck);
  const tabs = `<div class="tabs st-tabs" role="tablist">${Object.entries(ST_GAP).map(([k, v]) => `<button type="button" role="tab" data-gap="${k}" aria-selected="${ST.gap.pair === k}">${v[2]} × ${v[3]}</button>`).join("")}</div>`;
  if (rows.length < 2 || cols.length < 2) return stCard("空白探索", "", `${tabs}<p class="hint">5 件以上ある${rl}または${cl}が 2 つ未満のため、組み合わせを作れません。</p>`, "wide");
  const cell = new Map();
  for (const d of docs) for (const r of new Set(d[rk])) for (const c of new Set(d[ck])) { const k = r + "\u0001" + c; if (!cell.has(k)) cell.set(k, []); cell.get(k).push(d.id); }
  const cells = [];
  for (const r of rows) for (const c of cols) { const ids = cell.get(r.k + "\u0001" + c.k) || [], exp = r.n * c.n / N; cells.push({r, c, ids, exp}); }
  const gaps = cells.filter(x => !x.ids.length && x.exp >= 1).sort((a, b) => b.exp - a.exp);
  const thin = cells.filter(x => x.ids.length && x.ids.length <= 2 && x.ids.length < x.exp / 4).sort((a, b) => b.exp - a.exp);
  const gapKey = new Set(gaps.map(x => x.r.k + "\u0001" + x.c.k)), thinKey = new Set(thin.map(x => x.r.k + "\u0001" + x.c.k));
  const max = Math.max(1, ...cells.map(x => x.ids.length));
  // 濃淡表（空白と薄い組に印）
  const head = `<tr><th></th>${cols.map(c => `<th class="ch" title="${esc(labC(c.k))}"><span>${esc(labC(c.k))}</span></th>`).join("")}</tr>`;
  const body = rows.map(r => `<tr><th class="rh" title="${esc(labR(r.k))}">${esc(labR(r.k))}<small> ${r.n}</small></th>${cols.map(c => {
    const x = cells.find(y => y.r === r && y.c === c), k = r.k + "\u0001" + c.k, tip = `${labR(r.k)} × ${labC(c.k)}：実績 ${x.ids.length} 件／期待 ${x.exp.toFixed(1)} 件`;
    if (!x.ids.length) return gapKey.has(k) ? `<td class="gap" data-set="${stSet(stUnion(docs, rk, r.k, ck, c.k), `「${labR(r.k)}」または「${labC(c.k)}」を含む特許（空白の周り）`)}" data-tip="${esc(tip)}">0</td>` : `<td class="z" data-tip="${esc(tip)}"></td>`;
    const lv = Math.min(ST_SEQ.length - 1, Math.ceil(x.ids.length / max * (ST_SEQ.length - 1)));
    return `<td class="${thinKey.has(k) ? "thin" : ""}" data-set="${stSet(x.ids, `「${labR(r.k)}」×「${labC(c.k)}」の特許`)}" data-tip="${esc(tip)}" style="background:${ST_SEQ[lv]};color:${lv >= 4 ? "#fff" : "var(--ink)"}">${x.ids.length}</td>`;
  }).join("")}</tr>`).join("");
  const heat = `<div class="st-heat-wrap"><table class="st-heat">${head}${body}</table></div>
    <div class="st-scale"><span>少</span>${ST_SEQ.slice(1).map(c => `<i style="background:${c}"></i>`).join("")}<span>多（最大 ${max} 件）</span><i class="gapk"></i><span>空白（期待 1 件以上で実績 0）</span><i class="think"></i><span>薄い（1〜2 件で期待の 1/4 未満）</span></div>`;
  const who = ids => { const m = stCount(ids.map(i => ST.data.docs.find(d => d.id === i)).filter(Boolean), "applicants").slice(0, 2); return m.map(a => `${a.k}（${a.n}）`).join("、") || "—"; };
  const item = (x, union) => { const ids = union ? stUnion(docs, rk, x.r.k, ck, x.c.k) : x.ids;
    return `<li data-set="${stSet(ids, union ? `「${labR(x.r.k)}」または「${labC(x.c.k)}」を含む特許（空白の周り）` : `「${labR(x.r.k)}」×「${labC(x.c.k)}」の特許`)}" data-tip="${esc(`期待 ${x.exp.toFixed(1)} 件／実績 ${x.ids.length} 件`)}">
      <div class="t"><span><b>${esc(labR(x.r.k))}</b> × <b>${esc(labC(x.c.k))}</b></span><span class="v">期待 ${x.exp.toFixed(1)} 件 → 実績 ${x.ids.length}</span></div>
      <div class="hint">「${esc(labR(x.r.k))}」は ${x.r.n} 件、「${esc(labC(x.c.k))}」は ${x.c.n} 件。周りの主な出願人: ${esc(who(ids))}</div></li>`; };
  return [
    stCard("組み合わせ", "行と列に取る役割を選びます。行・列ともに 5 件以上ある概念の上位 15 です。", tabs, "wide"),
    stCard(`${rl} × ${cl}`, "マスの数字は両方を含む特許の件数。橙の枠が空白、点線の枠が薄い組です。期待件数は、2 つの概念が独立に現れるなら出るはずの件数（行の件数 × 列の件数 ÷ 全件数）で、マスに重ねると見えます。", heat, "wide"),
    stCard("空白の候補", `期待件数が大きいのに実績 0 の組を、期待の大きい順に並べました（${gaps.length} 組）。押すと、周りの特許（行か列のどちらかを含む特許）を右に出します。`, gaps.length ? `<ol class="st-gaps">${gaps.slice(0, 12).map(x => item(x, true)).join("")}</ol>` : `<p class="hint">期待 1 件以上で実績 0 の組はありません。</p>`),
    stCard("薄い組", `実績が 1〜2 件で、期待の 1/4 に届かない組です（${thin.length} 組）。押すと、その少数の特許を右に出します。`, thin.length ? `<ol class="st-gaps">${thin.slice(0, 8).map(x => item(x, false)).join("")}</ol>` : `<p class="hint">該当なし</p>`),
    stCard("読み方の注意", "", `<ul class="st-notes"><li>空白は「書かれていない」であって「できない」ではありません。技術的に成り立たない組、法規制で選べない組、言い換えが辞書に無いだけの組が混ざります。</li><li>母集団の集め方（検索式・用途の層ごとの件数）が、そのまま期待件数に影響します。層の件数を変えると空白も変わります。</li><li>空白の候補は、周りの特許を読んで「なぜ無いか」を確かめてから使ってください。</li></ul>`, "wide"),
  ].join("");
}
function stUnion(docs, rk, r, ck, c) { return docs.filter(d => d[rk].includes(r) || d[ck].includes(c)).map(d => d.id); }

// ---------- タブ「外れ値の特許」: ふだん一緒に出ない概念が結び付いた特許 ----------
// 珍しさ = log(期待件数 ÷ 実際の件数)。期待件数は空白探索と同じ（2 つの概念が独立なら出るはずの件数）。
function renderStOutliers(docs) {
  const N = docs.length, min = ST.out.min;
  const cnt = Object.fromEntries(ST_ROLE.map(([k]) => [k, new Map(stCount(docs, k).map(r => [r.k, r]))]));
  const kinds = [["problem", "solution", "課題", "解決手段"], ["solution", "use", "解決手段", "用途"]];
  const okc = (k, c) => stOk(c) && cnt[k].get(c).n >= min;
  const support = new Map();
  for (const d of docs) for (const [ak, bk] of kinds) for (const a of new Set(d[ak])) if (okc(ak, a)) for (const b of new Set(d[bk])) if (okc(bk, b)) { const key = ak + "\u0001" + a + "\u0001" + b; if (!support.has(key)) support.set(key, []); support.get(key).push(d.id); }
  const LN2 = Math.log(2);
  const scored = docs.map(d => {
    const ps = [];
    for (const [ak, bk, al, bl] of kinds) for (const a of new Set(d[ak])) if (okc(ak, a)) for (const b of new Set(d[bk])) if (okc(bk, b)) {
      const ids = support.get(ak + "\u0001" + a + "\u0001" + b), exp = cnt[ak].get(a).n * cnt[bk].get(b).n / N, s = Math.log(exp / ids.length);
      if (s >= LN2) ps.push({a, b, al, bl, ids, exp, s});
    }
    ps.sort((p, q) => q.s - p.s);
    return {d, ps, score: ps.length ? ps[0].s : 0};
  }).filter(x => x.ps.length).sort((a, b) => b.score - a.score || b.ps.length - a.ps.length);
  const top = scored.slice(0, 30), M = top.length ? top[0].score : 1;
  const ctl = `<label class="st-inline">概念の下限 <select id="stOutMin">${[3, 5, 10].map(v => `<option value="${v}" ${v === min ? "selected" : ""}>${v} 件以上</option>`).join("")}</select></label>`;
  const list = top.length ? `<ol class="st-out">${top.map(x => `<li>
      <div class="t"><span class="sb" data-tip="${esc(`珍しさ ${x.score.toFixed(2)}（期待は実際の ${Math.exp(x.score).toFixed(1)} 倍）`)}"><span class="b" style="width:${(x.score / M * 100).toFixed(1)}%"></span></span>
        <a href="#/map/${esc(ST.pid)}?sel=${esc(x.d.id)}">${esc(x.d.title)}</a></div>
      <div class="hint">${stDocLine(x.d)}</div>
      <ul class="st-pairs">${x.ps.slice(0, 2).map(p => `<li data-set="${stSet(p.ids, `${p.al}「${stLabel(p.a)}」× ${p.bl}「${stLabel(p.b)}」の特許`)}" data-tip="${esc(`期待 ${p.exp.toFixed(1)} 件／実際 ${p.ids.length} 件`)}">${p.al}「${esc(stLabel(p.a))}」× ${p.bl}「${esc(stLabel(p.b))}」<span class="v">ほかに ${p.ids.length - 1} 件（期待 ${p.exp.toFixed(1)} 件）</span></li>`).join("")}${x.ps.length > 2 ? `<li class="hint">ほか ${x.ps.length - 2} 組</li>` : ""}</ul></li>`).join("")}</ol>`
    : `<p class="hint">期待が実際の 2 倍以上になる組を持つ特許はありません。概念の下限を下げると増えます。</p>`;
  // この母集団で 1 件だけの概念
  const singles = [];
  for (const [k, lab] of ST_ROLE) for (const r of cnt[k].values()) if (r.n === 1 && stOk(r.k)) singles.push({k, lab, c: r.k, d: docs.find(d => d.id === r.ids[0])});
  singles.sort((a, b) => (b.d.year || 0) - (a.d.year || 0));
  const sg = singles.length ? `<ol class="st-out st-single">${singles.slice(0, 20).map(x => `<li><div class="t"><span class="st-pill" style="cursor:default">${x.lab}<small>${esc(stLabel(x.c))}</small></span><a href="#/map/${esc(ST.pid)}?sel=${esc(x.d.id)}">${esc(x.d.title)}</a></div><div class="hint">${stDocLine(x.d)}</div></li>`).join("")}</ol>${singles.length > 20 ? `<p class="hint">ほか ${singles.length - 20} 件</p>` : ""}` : `<p class="hint">1 件だけの概念はありません。</p>`;
  return [
    stCard("珍しい組み合わせを持つ特許", `課題×解決手段、解決手段×用途の組ごとに、期待件数（2 つが独立なら出るはずの件数）と実際の件数を比べ、期待が実際の 2 倍以上の組を持つ特許を珍しさの順に並べました（${scored.length} 件中、上位 30）。棒が長いほど、ふだん結び付かない概念が結び付いています。組を押すと、同じ組を持つ特許を右に出します。 ${ctl}`, list, "wide"),
    stCard("この母集団で 1 件だけの概念", `ほかのどの特許にも出ない概念と、それを持つ特許です（${singles.length} 件、出願年の新しい順）。新しい語の手がかりになります。`, sg),
    stCard("読み方の注意", "", `<ul class="st-notes"><li>外れ値には、辞書の拾い間違い（別名が広すぎる、配合例の語を拾った）が多く混ざります。上位は原文で確かめ、間違いなら辞書を直してください。辞書の点検にも使えます。</li><li>期待件数は 2 つの概念が独立に現れると仮定した値です。片方がもう片方の前提になる組（例: 課題と、それ専用の手段）は、実際の件数が多くなり外れ値に出にくくなります。</li><li>件数の少ない概念は期待件数も小さく、外れ値に出にくくなります。下限を下げると増えますが、偶然の組も増えます。</li></ul>`),
  ].join("");
}

function bindStInteract() {
  const tip = $("#stTip");
  $("#viewStats").querySelectorAll("[data-tip]").forEach(el => {
    el.onmousemove = e => { tip.textContent = el.dataset.tip; tip.hidden = false; tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 340) + "px"; tip.style.top = (e.clientY + 14) + "px"; };
    el.onmouseleave = () => { tip.hidden = true; };
  });
  $("#viewStats").querySelectorAll("[data-set]").forEach(el => {
    el.tabIndex = 0;
    const open = () => stDrawer(ST.sets[el.dataset.set]);
    el.onclick = open;
    el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
}

function stDrawer(set) {
  if (!set) return;
  const byId = new Map(ST.data.docs.map(d => [d.id, d]));
  const docs = set.ids.map(i => byId.get(i)).filter(Boolean).sort((a, b) => (b.year || 0) - (a.year || 0));
  const dr = $("#stDrawer");
  dr.hidden = false;
  dr.innerHTML = `<div class="st-dh"><b>${esc(set.title)}</b><button class="tbtn" id="stClose" type="button" aria-label="閉じる">×</button></div>
    <p class="hint">${docs.length} 件（出願年の新しい順）。名称を押すと、地図でその特許を選びます。</p>
    <ol class="st-docs">${docs.map(d => `<li><a href="#/map/${esc(ST.pid)}?sel=${esc(d.id)}">${esc(d.title)}</a>
      <div class="hint">${esc(d.applicants.slice(0, 2).join("、"))}${d.applicants.length > 2 ? " ほか" : ""} · ${d.year ?? "—"} · ${esc(d.pub)}</div></li>`).join("")}</ol>`;
  $("#stClose").onclick = () => { dr.hidden = true; };
}

// すべての定義のあとで、今の URL の画面を開く
route();
