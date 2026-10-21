"""マップ比較ビューア（単一 HTML）。2 手法の配置を重ね、点の移動をアニメーションで見る。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .similarity.graph_sim import RelationConfig, TripleIndex

ROLE_ORDER = ["B1", "B2", "B3", "P1", "P2", "P3", "P3tuned", "A1", "A2"]
# data/sections.csv の列 → 表示見出し（段落付き文書のとき）
SECTION_COLUMNS = [("pub_number", "公報番号"), ("title", "名称"), ("applicants", "出願人"), ("ipc_first", "筆頭 IPC"), ("problem", "課題"), ("solution", "解決手段"), ("use", "用途")]


def _method_key(m: str) -> tuple:
    base, _, layer = m.partition("@")
    return (ROLE_ORDER.index(base) if base in ROLE_ORDER else 99, layer)


def _nan(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def build_viewer(run) -> Path:
    """maps/viewer.html を書く。"""
    from . import pipeline as pl

    pl.ensure_docs(run)
    if not run.sims:
        pl.load_similarities(run)
    coords = pd.read_csv(run.root / "maps" / "map_coords.csv")
    methods = sorted(coords.method.unique(), key=_method_key)
    n = len(run.docs)
    pos = {m: {} for m in methods}
    for (m, red), g in coords.groupby(["method", "reducer"]):
        g = g.set_index("doc_id").loc[run.doc_ids]
        xy = g[["x", "y"]].to_numpy()  # pipeline で中心からの RMS 距離 = 1 の単位にそろえ済み
        pos[m][red] = [[round(float(x), 4), round(float(y), 4)] for x, y in xy]

    def topk(S: np.ndarray, i: int, k: int = 10) -> list[list]:
        row = S[i].astype(float).copy()
        row[i] = -np.inf
        row[np.isnan(row)] = -np.inf
        order = np.argsort(-row, kind="stable")[:k]
        return [[int(j), round(float(row[j]), 3)] for j in order if np.isfinite(row[j])]

    neighbors = {m: [topk(run.sims[m], i) for i in range(n)] for m in methods if m in run.sims}

    # 関係の対応づけ（類似の根拠）
    rc = RelationConfig.from_dict(run.config["relation_similarity"])
    layers = sorted({m.split("@")[1] for m in methods if "@" in m})
    triples, expl = {}, {}
    for L in layers:
        if L not in run.layers:
            from .schema import GraphLayer

            run.layers[L] = GraphLayer.load(run.root / "graphs", L, run.doc_ids)
        ti = run.triple_index.get(L) or TripleIndex(run.layers[L], run.doc_ids, run.onto, rc)
        lab = run.onto.label
        pred = {p: v.label for p, v in run.onto.predicates.items()}
        triples[L] = [
            [[lab(t.subject_id), pred.get(t.predicate, t.predicate), lab(t.object_id), t.role, int(t.negated), int(t.speculative), t.evidence_text] for t in ti.triples[a:b]]
            for a, b in ti.offsets
        ]
        expl[L] = []
        for i in range(n):
            cand = set()
            for m in methods:
                if m in neighbors:
                    cand |= {j for j, _ in neighbors[m][i]}
            d = {}
            a0 = ti.offsets[i][0]
            for j in sorted(cand):
                sc, matches = ti.doc_pair(i, j)
                if math.isnan(sc):
                    continue
                b0 = ti.offsets[j][0]
                d[str(j)] = [round(sc, 3), [[x - a0, y - b0, round(s, 3)] for x, y, s in matches if s > 0]]
            expl[L].append(d)

    ev_dir = run.root / "eval"
    # 評価ファイルは無くてもよい（正解ラベルの無い特許の run など）。無ければ空の表として扱う。
    q = pd.read_csv(ev_dir / "similarity_quality.csv") if (ev_dir / "similarity_quality.csv").exists() else pd.DataFrame(columns=["subset", "method"])
    q = q[q.subset == "all"].set_index("method")
    f = pd.read_csv(ev_dir / "map_fidelity.csv").set_index(["method", "reducer"]) if (ev_dir / "map_fidelity.csv").exists() else pd.DataFrame(columns=["method", "reducer"]).set_index(["method", "reducer"])
    lp = pd.read_csv(ev_dir / "map_label_preservation.csv").set_index(["method", "reducer"]) if (ev_dir / "map_label_preservation.csv").exists() else pd.DataFrame(columns=["method", "reducer"]).set_index(["method", "reducer"])
    metrics = {}
    for m in methods:
        metrics[m] = {
            "spearman": _nan(float(q.loc[m, "spearman"])) if m in q.index else None,
            "ndcg": _nan(float(q.loc[m, "ndcg@10"])) if m in q.index else None,
            "para_beats_inv": _nan(float(q.loc[m, "para_beats_inv"])) if m in q.index and "para_beats_inv" in q else None,
            "label_source": str(q.loc[m, "label_source"]) if m in q.index else "",
        }
        for red in ("mds", "umap"):
            if (m, red) in f.index:
                metrics[m][red] = {
                    "knn10": round(float(f.loc[(m, red), "knn@10"]), 3),
                    "stress": round(float(f.loc[(m, red), "stress"]), 3),
                    "rel_in_knn10": round(float(lp.loc[(m, red), "relevant_in_2d_knn10"]), 3) if (m, red) in lp.index else None,
                }
    docs = [{"id": d.doc_id, "type": d.source_type, "domain": d.tech_domain, "text": d.text, "orig": d.original_id} for d in run.docs]
    # 段落付きの文書（特許）: data/sections.csv があれば名称・課題・解決手段・用途を添える
    sec_path = run.root / "data" / "sections.csv"
    if sec_path.exists():
        sec = pd.read_csv(sec_path, dtype=str, keep_default_na=False).set_index("doc_id")
        cols = [(c, h) for c, h in SECTION_COLUMNS if c in sec.columns]
        for d in docs:
            if d["id"] in sec.index:
                r = sec.loc[d["id"]]
                d["sec"] = [[h, r[c]] for c, h in cols if r[c]]
    vcfg = run.config.get("viewer") or {}
    manifest = json.loads((run.root / "similarity" / "manifest.json").read_text(encoding="utf-8"))
    data = {
        "run": run.root.name,
        "docs": docs,
        "methods": methods,
        "methodInfo": {m: manifest.get(m, {}).get("kind", "") for m in methods},
        "pos": pos,
        "neighbors": neighbors,
        "triples": triples,
        "expl": expl,
        "metrics": metrics,
        "synthetic": (run.root / "data" / "_truth.jsonl").exists(),
        "defaultA": vcfg.get("default_a"),
        "defaultB": vcfg.get("default_b"),
    }
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = run.root / "maps" / "viewer.html"
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>手法間マップ比較</title>
<style>
:root{
  color-scheme: light;
  --surface-0:#f4f4f2; --surface-1:#fcfcfb; --surface-2:#ffffff; --border:#e2e1dc; --grid:#ecebe7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#7a7974;
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  --trail:#9b9a94; --link-para:#2a78d6; --link-inv:#eb6834; --focus:#0b0b0b; --accent-bg:#e9f1fb;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --surface-0:#121211; --surface-1:#1a1a19; --surface-2:#222220; --border:#34342f; --grid:#2a2a27;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8f8e86;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
    --trail:#6f6e68; --link-para:#3987e5; --link-inv:#d95926; --focus:#ffffff; --accent-bg:#1d2a3a;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface-0:#121211; --surface-1:#1a1a19; --surface-2:#222220; --border:#34342f; --grid:#2a2a27;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8f8e86;
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  --trail:#6f6e68; --link-para:#3987e5; --link-inv:#d95926; --focus:#ffffff; --accent-bg:#1d2a3a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-0);color:var(--text-primary);font:14px/1.6 "Hiragino Sans","Noto Sans JP",system-ui,sans-serif}
header{padding:16px 20px 8px}
h1{font-size:18px;margin:0 0 2px;font-weight:600}
.sub{color:var(--text-secondary);font-size:12.5px;margin:0}
.warn{margin:8px 20px 0;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-2);color:var(--text-secondary);font-size:12.5px}
.controls{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--border)}
.controls label{font-size:12px;color:var(--text-secondary);display:flex;gap:6px;align-items:center}
select,button,input[type=search]{font:inherit;font-size:13px;color:var(--text-primary);background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:4px 8px}
button{cursor:pointer}
button.primary{background:var(--text-primary);color:var(--surface-1);border-color:var(--text-primary);min-width:84px}
input[type=range]{width:170px}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px 20px}
@media (max-width:980px){.layout{grid-template-columns:1fr}}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.chart-head h2{font-size:15px;margin:0;font-weight:600}
.tlabel{font-size:12px;color:var(--text-secondary)}
svg{display:block;width:100%;height:auto;touch-action:manipulation}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--text-secondary);margin-top:8px}
.legend span{display:inline-flex;align-items:center;gap:5px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.stat{border:1px solid var(--border);border-radius:8px;padding:8px 10px;background:var(--surface-2)}
.stat .k{font-size:11.5px;color:var(--text-secondary)}
.stat .v{font-size:13px;font-variant-numeric:tabular-nums}
.side h3{font-size:13.5px;margin:12px 0 6px;font-weight:600}
.side h3:first-child{margin-top:0}
.doctext{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:13px}
.meta{font-size:12px;color:var(--text-secondary)}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:500}
td.num,th.num{text-align:right}
tr.clickable{cursor:pointer}
tr.clickable:hover{background:var(--accent-bg)}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:999px;border:1px solid var(--border);color:var(--text-secondary)}
.match{font-size:12px;border-left:3px solid var(--border);padding:2px 8px;margin:4px 0}
.match .s{color:var(--text-secondary)}
#tip{position:fixed;pointer-events:none;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:12px;max-width:300px;box-shadow:0 2px 10px rgba(0,0,0,.12);display:none;z-index:9}
.hint{color:var(--text-muted);font-size:12px}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13px}
.tablewrap{max-height:340px;overflow:auto}
</style>
</head>
<body>
<header>
  <h1>手法間マップ比較</h1>
  <p class="sub">手法 A の配置から手法 B の配置へ、各文書がどう動くかを見る。2 つの配置は回転・反転・拡大をそろえてから重ねている（Procrustes 整列）。</p>
</header>
<div class="warn" id="synthWarn" hidden>見本コーパス（合成文書）と模擬ラベルでの結果です。実文書での性能の根拠にはなりません。近くに置かれることは、侵害・新規性・同一技術の証拠になりません。</div>
<div class="controls" role="group" aria-label="表示の設定">
  <label>配置 <select id="reducer"><option value="mds">Metric MDS</option><option value="umap">UMAP</option></select></label>
  <label>手法 A <select id="mA"></select></label>
  <label>手法 B <select id="mB"></select></label>
  <button class="primary" id="play" type="button">▶ 再生</button>
  <label>A <input type="range" id="t" min="0" max="1000" value="0" aria-label="A から B への補間"> B</label>
  <label><input type="checkbox" id="trail" checked> 移動の線</label>
  <label><input type="checkbox" id="links"> 元文書と制御文書を結ぶ</label>
  <label>領域 <select id="domain"><option value="">すべて</option></select></label>
  <label>種別 <select id="stype"><option value="">すべて</option><option value="real">実文書</option><option value="paraphrase">言い換え</option><option value="inverted">関係反転</option></select></label>
  <label><input type="search" id="q" placeholder="文書 ID で探す" size="12"></label>
</div>
<div class="layout">
  <div>
    <div class="card">
      <div class="chart-head"><h2 id="chartTitle"></h2><span class="tlabel" id="tLabel"></span></div>
      <svg id="map" viewBox="0 0 720 720" role="img" aria-label="文書の二次元配置"></svg>
      <div class="legend" id="legend"></div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="chart-head"><h2>大きく動いた文書</h2><span class="hint">行を押すと選択</span></div>
      <p class="hint" style="margin:4px 0 6px">移動距離は、配置の中心からの平均距離を 1 とした単位。近傍の重なりは、2D の近い 10 文書が A と B でどれだけ同じか（1 = 同じ、0 = すべて入れ替わり）。</p>
      <div class="tablewrap"><table id="movers"></table></div>
    </div>
  </div>
  <aside class="card side" id="side" aria-live="polite"></aside>
</div>
<div id="tip" role="tooltip"></div>
<script>
const DATA = /*__DATA__*/null;
const TYPE_LABEL = {real:"実文書", paraphrase:"言い換え", inverted:"関係反転"};
const TYPE_VAR = {real:"--series-1", paraphrase:"--series-3", inverted:"--series-2"};
const DOMAINS = [...new Set(DATA.docs.map(d=>d.domain))];
const SHAPES = ["circle","square","triangle","diamond"];
const $ = s => document.querySelector(s);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const svg = $("#map"), NS = "http://www.w3.org/2000/svg";
const W = 720, PAD = 28;
const state = {sel:null, t:0, playing:false, dir:1};
if (DATA.synthetic) $("#synthWarn").hidden = false;
const esc = s => String(s??"").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const docBody = d => d.sec && d.sec.length ? d.sec.map(([h,t])=>`<div><b>${esc(h)}</b>　${esc(t)}</div>`).join("") : esc(d.text);
const fmt = (v,d=3) => v==null||Number.isNaN(v) ? "—" : Number(v).toFixed(d);
const mLabel = m => {
  const [b,l] = m.split("@");
  const names = {B1:"文字 n-gram TF-IDF",B2:"文埋め込み",B3:"Doc2Vec（参考）",P1:"概念集合",P2:"関係グラフ",P3:"ハイブリッド",P3tuned:"ハイブリッド（重み調整）",A1:"エッジ除去",A2:"始点終点の入替"};
  const layer = l ? ({gold:"正解グラフ",dict:"辞書抽出",llm:"LLM 抽出"}[l]||l) : "";
  return `${b} ${names[b]||""}${layer?"・"+layer:""}`;
};
for (const id of ["#mA","#mB"]) for (const m of DATA.methods) $(id).append(new Option(mLabel(m), m));
$("#mA").value = DATA.methods.includes(DATA.defaultA) ? DATA.defaultA : DATA.methods.includes("B2") ? "B2" : DATA.methods[0];
$("#mB").value = DATA.methods.includes(DATA.defaultB) ? DATA.defaultB : DATA.methods.includes("P2@dict") ? "P2@dict" : DATA.methods[DATA.methods.length-1];
for (const d of DOMAINS) $("#domain").append(new Option(d, d));

function shapePath(shape, x, y, r){
  if (shape==="circle") return `M${x-r},${y}a${r},${r} 0 1,0 ${2*r},0a${r},${r} 0 1,0 ${-2*r},0`;
  if (shape==="square") { const s=r*0.9; return `M${x-s},${y-s}h${2*s}v${2*s}h${-2*s}z`; }
  if (shape==="triangle") { const s=r*1.2; return `M${x},${y-s}L${x+s*0.95},${y+s*0.7}L${x-s*0.95},${y+s*0.7}z`; }
  const s=r*1.25; return `M${x},${y-s}L${x+s},${y}L${x},${y+s}L${x-s},${y}z`;
}
let scale = null;
function computeScale(){
  const red = $("#reducer").value, A = DATA.pos[$("#mA").value][red], B = DATA.pos[$("#mB").value][red];
  let lo=Infinity, hi=-Infinity;
  for (const P of [A,B]) for (const [x,y] of P){ lo=Math.min(lo,x,y); hi=Math.max(hi,x,y); }
  const m = Math.max(Math.abs(lo), Math.abs(hi)) * 1.04;
  scale = v => PAD + (v + m) / (2*m) * (W - 2*PAD);
}
const ease = t => t<.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;
function positions(){
  const red=$("#reducer").value, A=DATA.pos[$("#mA").value][red], B=DATA.pos[$("#mB").value][red], e=ease(state.t);
  return A.map((a,i)=>[scale(a[0]*(1-e)+B[i][0]*e), scale(-(a[1]*(1-e)+B[i][1]*e))]);
}
function visible(i){
  const d=DATA.docs[i], dom=$("#domain").value, st=$("#stype").value;
  return (!dom||d.domain===dom) && (!st||d.type===st);
}
const idIndex = Object.fromEntries(DATA.docs.map((d,i)=>[d.id,i]));
let layerTrail, layerLinks, layerPts, layerSel;
function buildSvg(){
  svg.innerHTML="";
  const frame=document.createElementNS(NS,"rect");
  Object.entries({x:PAD/2,y:PAD/2,width:W-PAD,height:W-PAD,rx:8,fill:"var(--surface-2)",stroke:"var(--grid)"}).forEach(([k,v])=>frame.setAttribute(k,v));
  svg.append(frame);
  layerTrail=document.createElementNS(NS,"g"); layerLinks=document.createElementNS(NS,"g");
  layerPts=document.createElementNS(NS,"g"); layerSel=document.createElementNS(NS,"g");
  svg.append(layerTrail, layerLinks, layerPts, layerSel);
  DATA.docs.forEach((d,i)=>{
    const p=document.createElementNS(NS,"path");
    p.setAttribute("fill",`var(${TYPE_VAR[d.type]||"--series-1"})`);
    p.setAttribute("stroke","var(--surface-2)"); p.setAttribute("stroke-width","2");
    p.dataset.i=i; p.style.cursor="pointer";
    p.addEventListener("mouseenter",ev=>showTip(ev,i)); p.addEventListener("mousemove",ev=>moveTip(ev));
    p.addEventListener("mouseleave",hideTip); p.addEventListener("click",()=>select(i));
    // 当たり判定を大きくするための透明な円
    const hit=document.createElementNS(NS,"circle"); hit.setAttribute("r","9"); hit.setAttribute("fill","transparent"); hit.dataset.i=i; hit.style.cursor="pointer";
    hit.addEventListener("mouseenter",ev=>showTip(ev,i)); hit.addEventListener("mousemove",ev=>moveTip(ev));
    hit.addEventListener("mouseleave",hideTip); hit.addEventListener("click",()=>select(i));
    layerPts.append(p, hit);
  });
}
function draw(){
  const P=positions(), red=$("#reducer").value;
  const A=DATA.pos[$("#mA").value][red].map(([x,y])=>[scale(x),scale(-y)]);
  const B=DATA.pos[$("#mB").value][red].map(([x,y])=>[scale(x),scale(-y)]);
  const nodes=layerPts.children;
  for (let k=0;k<nodes.length;k+=2){
    const i=+nodes[k].dataset.i, d=DATA.docs[i], vis=visible(i);
    const shape=SHAPES[DOMAINS.indexOf(d.domain)%4];
    nodes[k].setAttribute("d",shapePath(shape,P[i][0],P[i][1],5.2));
    nodes[k].style.opacity = vis ? (state.sel==null||state.sel===i||nbSet.has(i) ? 1 : .28) : .06;
    nodes[k+1].setAttribute("cx",P[i][0]); nodes[k+1].setAttribute("cy",P[i][1]);
    nodes[k+1].style.display = vis ? "" : "none";
  }
  // 移動の線（A の位置 → B の位置）
  let tr="";
  if ($("#trail").checked) DATA.docs.forEach((d,i)=>{ if(visible(i)) tr+=`M${A[i][0]},${A[i][1]}L${B[i][0]},${B[i][1]}`; });
  layerTrail.innerHTML = tr ? `<path d="${tr}" stroke="var(--trail)" stroke-width="1" fill="none" opacity=".45"/>` : "";
  // 制御文書と元文書を結ぶ線
  let lp="", li="";
  if ($("#links").checked) DATA.docs.forEach((d,i)=>{
    if(!d.orig||!(d.orig in idIndex)||!visible(i)) return;
    const j=idIndex[d.orig], s=`M${P[i][0]},${P[i][1]}L${P[j][0]},${P[j][1]}`;
    if(d.type==="paraphrase") lp+=s; else li+=s;
  });
  layerLinks.innerHTML = (lp?`<path d="${lp}" stroke="var(--link-para)" stroke-width="1.5" fill="none" opacity=".8"/>`:"")
                       + (li?`<path d="${li}" stroke="var(--link-inv)" stroke-width="1.5" stroke-dasharray="4 3" fill="none" opacity=".8"/>`:"");
  // 選択中の文書と近傍
  layerSel.innerHTML="";
  if (state.sel!=null){
    const [x,y]=P[state.sel];
    layerSel.innerHTML=`<circle cx="${x}" cy="${y}" r="10" fill="none" stroke="var(--focus)" stroke-width="2"/>`;
  }
  $("#tLabel").textContent = state.t===0 ? "表示中: A の配置" : state.t===1 ? "表示中: B の配置" : `A → B ${Math.round(state.t*100)}%`;
}
let nbSet=new Set();
function legend(){
  const L=$("#legend");
  L.innerHTML = Object.entries(TYPE_LABEL).map(([k,v])=>`<span><svg width="14" height="14" viewBox="0 0 14 14"><path d="${shapePath("circle",7,7,5)}" fill="var(${TYPE_VAR[k]})"/></svg>${v}</span>`).join("")
    + DOMAINS.map((d,i)=>`<span><svg width="14" height="14" viewBox="0 0 14 14"><path d="${shapePath(SHAPES[i%4],7,7,4.6)}" fill="var(--text-secondary)"/></svg>${d}</span>`).join("")
    + `<span><svg width="22" height="8"><line x1="1" y1="4" x2="21" y2="4" stroke="var(--link-para)" stroke-width="1.5"/></svg>言い換え ↔ 元文書</span>`
    + `<span><svg width="22" height="8"><line x1="1" y1="4" x2="21" y2="4" stroke="var(--link-inv)" stroke-width="1.5" stroke-dasharray="4 3"/></svg>関係反転 ↔ 元文書</span>`;
}
function stats(){
  const red=$("#reducer").value, a=$("#mA").value, b=$("#mB").value, MA=DATA.metrics[a], MB=DATA.metrics[b];
  const row=(k,fa,fb)=>`<div class="stat"><div class="k">${k}</div><div class="v">A ${fa} ／ B ${fb}</div></div>`;
  $("#stats").innerHTML =
    row("類似度とラベルの Spearman", fmt(MA.spearman), fmt(MB.spearman)) +
    row("nDCG@10", fmt(MA.ndcg), fmt(MB.ndcg)) +
    row("2D の 10 近傍保持率", fmt(MA[red]?.knn10), fmt(MB[red]?.knn10)) +
    row("スコア 3 のペアが 2D 近傍に残る率", fmt(MA[red]?.rel_in_knn10), fmt(MB[red]?.rel_in_knn10)) +
    row("言い換えが関係反転より近い率", fmt(MA.para_beats_inv), fmt(MB.para_beats_inv)) +
    row("正規化ストレス（低いほど良い）", fmt(MA[red]?.stress), fmt(MB[red]?.stress));
  $("#chartTitle").textContent = `${mLabel(a)}  →  ${mLabel(b)}`;
}
let moves=[];
function movement(){
  const red=$("#reducer").value, A=DATA.pos[$("#mA").value][red], B=DATA.pos[$("#mB").value][red], n=A.length;
  const knn=P=>P.map((p,i)=>P.map((q,j)=>[j,(p[0]-q[0])**2+(p[1]-q[1])**2]).filter(([j])=>j!==i).sort((x,y)=>x[1]-y[1]).slice(0,10).map(([j])=>j));
  const ka=knn(A), kb=knn(B);
  moves = A.map((a,i)=>{
    const sa=new Set(ka[i]), sb=new Set(kb[i]), inter=[...sa].filter(x=>sb.has(x)).length;
    return {i, shift:Math.hypot(a[0]-B[i][0],a[1]-B[i][1]), jac: inter/(20-inter)};
  });
  const rows=[...moves].filter(m=>visible(m.i)).sort((x,y)=>x.jac-y.jac||y.shift-x.shift).slice(0,25);
  $("#movers").innerHTML = `<thead><tr><th>文書</th><th>種別</th><th>領域</th><th class="num">移動距離</th><th class="num">近傍の重なり</th></tr></thead><tbody>`+
    rows.map(m=>{const d=DATA.docs[m.i]; return `<tr class="clickable" data-i="${m.i}"><td>${d.id}</td><td>${TYPE_LABEL[d.type]||d.type}</td><td>${d.domain}</td><td class="num">${fmt(m.shift,2)}</td><td class="num">${fmt(m.jac,2)}</td></tr>`}).join("")+`</tbody>`;
  $("#movers").querySelectorAll("tr.clickable").forEach(tr=>tr.addEventListener("click",()=>select(+tr.dataset.i)));
}
function layerOf(m){ return m.includes("@") ? m.split("@")[1] : null; }
function explainPair(i,j){
  const layers=[layerOf($("#mB").value), layerOf($("#mA").value), "dict", "gold"].filter((l,k,a)=>l&&a.indexOf(l)===k&&DATA.expl[l]);
  for (const L of layers){
    const e=DATA.expl[L][i]?.[String(j)];
    const T=DATA.triples[L];
    if (!T) continue;
    const head=`<div class="meta">関係グラフ（${({gold:"正解グラフ",dict:"辞書抽出",llm:"LLM 抽出"}[L]||L)}）での関係類似度 ${e?fmt(e[0]):"—"}</div>`;
    if (!e || !e[1].some(x=>x[2]>=0.1)) return head + `<div class="hint">対応する関係はありません。</div>`;
    const tri=t=>`${esc(t[0])} —${esc(t[1])}${t[4]?"（否定）":""}${t[5]?"（推量）":""}→ ${esc(t[2])} <span class="s">[${esc(t[3])}]</span>`;
    const ms=[...e[1]].filter(x=>x[2]>=0.1).sort((x,y)=>y[2]-x[2]).slice(0,3).map(([a,b,s])=>`<div class="match"><div>${tri(T[i][a])}</div><div>${tri(T[j][b])}</div><div class="s">一致度 ${fmt(s,2)}</div></div>`).join("");
    const used=new Set(e[1].filter(x=>x[2]>=0.1).map(x=>x[0])), miss=T[i].filter((_,k)=>!used.has(k));
    return head + ms + (miss.length?`<div class="hint">対応相手のない関係: ${miss.map(t=>`${esc(t[0])}→${esc(t[2])}`).join("、")}</div>`:"");
  }
  return `<div class="hint">関係グラフの根拠はありません。</div>`;
}
function select(i){
  state.sel = (i==null || state.sel===i) ? null : i;
  renderSide(); draw();
}
function renderSide(){
  const S=$("#side");
  if (state.sel==null){
    nbSet=new Set();
    S.innerHTML=`<h3>文書を選ぶ</h3><p class="hint">マップ上の点か、左の表の行を押すと、本文と A・B それぞれの近い文書、近いと判定した根拠（一致した関係）を表示します。</p>
    <h3>見方</h3><ul class="hint" style="padding-left:18px;margin:0">
    <li>再生で A の配置から B の配置へ点が動きます。灰色の線は各点の移動の向きです。</li>
    <li>「元文書と制御文書を結ぶ」を入れると、言い換え（実線）と関係反転（破線）が元文書とどれだけ離れているかが見えます。関係を見る手法では実線が短く、破線が長くなるのが期待される動きです。</li>
    <li>表の「近傍の重なり」が小さい文書ほど、A と B で周りの文書が入れ替わっています。</li></ul>`;
    return;
  }
  const i=state.sel, d=DATA.docs[i], a=$("#mA").value, b=$("#mB").value;
  const NA=DATA.neighbors[a][i].slice(0,8), NB=DATA.neighbors[b][i].slice(0,8);
  const sa=new Set(NA.map(x=>x[0])), sb=new Set(NB.map(x=>x[0]));
  nbSet=new Set([...sa,...sb]);
  const mv=moves[i];
  const nbTable=(L,other,label)=>`<table><thead><tr><th>${label}</th><th>種別</th><th class="num">類似度</th><th></th></tr></thead><tbody>`+
    L.map(([j,s])=>`<tr class="clickable" data-j="${j}"><td>${DATA.docs[j].id}</td><td>${TYPE_LABEL[DATA.docs[j].type]}</td><td class="num">${fmt(s)}</td><td>${other.has(j)?'<span class="tag">共通</span>':''}</td></tr>`).join("")+`</tbody></table>`;
  const src = d.orig && d.orig in idIndex ? `<div class="meta">元文書: <a href="#" data-goto="${idIndex[d.orig]}">${d.orig}</a></div>` : "";
  S.innerHTML=`<h3>${d.id}　<span class="tag">${TYPE_LABEL[d.type]}</span> <span class="tag">${d.domain}</span></h3>
    <div class="doctext">${docBody(d)}</div>${src}
    <div class="meta" style="margin-top:6px">A→B の移動距離 ${fmt(mv?.shift,2)}　近傍の重なり ${fmt(mv?.jac,2)}</div>
    <h3>A で類似度が高い文書</h3>${nbTable(NA,sb,"A: "+a)}
    <h3>B で類似度が高い文書</h3>${nbTable(NB,sa,"B: "+b)}
    <h3>近い理由</h3><div id="why" class="hint">上の表の行を押すと、一致した関係を表示します。</div>`;
  S.querySelectorAll("tr.clickable").forEach(tr=>tr.addEventListener("click",()=>{
    const j=+tr.dataset.j;
    $("#why").innerHTML=`<div class="doctext" style="margin-bottom:6px"><b>${esc(DATA.docs[j].id)}</b> ${docBody(DATA.docs[j])}</div>`+explainPair(i,j);
  }));
  S.querySelectorAll("[data-goto]").forEach(el=>el.addEventListener("click",ev=>{ev.preventDefault(); state.sel=null; select(+el.dataset.goto);}));
}
const tip=$("#tip");
function showTip(ev,i){ const d=DATA.docs[i]; const title=(d.sec||[]).find(x=>x[0]==="名称"); tip.innerHTML=`<b>${esc(d.id)}</b>　${esc(TYPE_LABEL[d.type]||d.type)}・${esc(d.domain)}<br>${esc(title?title[1]:d.text.slice(0,60)+"…")}`; tip.style.display="block"; moveTip(ev); }
function moveTip(ev){ tip.style.left=Math.min(ev.clientX+14, innerWidth-310)+"px"; tip.style.top=(ev.clientY+14)+"px"; }
function hideTip(){ tip.style.display="none"; }
let last=null;
function frame(ts){
  if(!state.playing){ last=null; return; }
  if(last!=null){ state.t=Math.min(1,Math.max(0,state.t+state.dir*(ts-last)/1800)); }
  last=ts; $("#t").value=Math.round(state.t*1000); draw();
  if(state.t>=1||state.t<=0){ state.playing=false; $("#play").textContent= state.t>=1 ? "◀ 戻す" : "▶ 再生"; return; }
  requestAnimationFrame(frame);
}
$("#play").addEventListener("click",()=>{
  if(state.playing){ state.playing=false; $("#play").textContent="▶ 再生"; return; }
  state.dir = state.t>=1 ? -1 : 1; state.playing=true; $("#play").textContent="❚❚ 停止"; requestAnimationFrame(frame);
});
$("#t").addEventListener("input",e=>{ state.t=e.target.value/1000; state.playing=false; $("#play").textContent= state.t>=1?"◀ 戻す":"▶ 再生"; draw(); });
function refresh(){ computeScale(); stats(); movement(); renderSide(); draw(); }
["#reducer","#mA","#mB"].forEach(s=>$(s).addEventListener("change",refresh));
["#trail","#links"].forEach(s=>$(s).addEventListener("change",draw));
["#domain","#stype"].forEach(s=>$(s).addEventListener("change",()=>{movement(); draw();}));
$("#q").addEventListener("change",e=>{ const v=e.target.value.trim().toUpperCase(); if(v in idIndex){ state.sel=null; select(idIndex[v]); } });
buildSvg(); legend(); refresh();
</script>
</body>
</html>
"""
