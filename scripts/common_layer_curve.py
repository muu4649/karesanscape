"""共通層（inorg-1.0）を作るのに何件の特許が要るかを測る（学習曲線の模擬）。

やり方:
- 無機材料 1,200 件の開発用 600 件（doc_id が奇数）から N 件を無作為に選ぶ。
- その N 件の課題・解決手段・用途の段落に出た表記（inorg-1.0 の表示名・別名）だけを残した辞書を作る。
  「人が N 件を読んで、出てきた語はすべて辞書に入れた」場合の上限にあたる。中間概念（木の形）は全部残す。
- その辞書で、確認用 600 件（偶数）と MOF 614 件を測る。
  - 拾える割合: 全部入りの inorg-1.0 で拾える（段落, 概念）のうち、縮めた辞書でも拾える割合
  - 取りこぼし率: 段落の型の概念が 1 つも拾えない段落の割合
- N ごとに 5 回選び直して平均と幅を出す。

使い方: .venv/bin/python scripts/common_layer_curve.py  → runs/common_layer_curve/curve.csv と curve.png（図だけ描き直すときは引数 plot）
"""
from __future__ import annotations

import copy
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ontomap.app.layers import Matcher, _norm  # noqa: E402
from ontomap.ontology import Ontology  # noqa: E402

import yaml  # noqa: E402

BASE = ROOT / "config/ontology_inorganic_v1.yaml"
OUT = ROOT / "runs/common_layer_curve"
SECTIONS = {"problem": "P", "solution": "M", "use_text": "U"}
NS = [10, 25, 50, 100, 200, 300, 400, 600]
REPEATS = 5


def doc_hits(df: pd.DataFrame, onto: Ontology) -> list[dict[str, set]]:
    """文書ごと・段落ごとの (表記, 概念) の集合（段落の型の概念だけ）。"""
    m = Matcher(onto)
    out = []
    for _, r in df.iterrows():
        row = {}
        for col, top in SECTIONS.items():
            t = _norm(r.get(col, ""))
            row[col] = {(s, c) for _, _, s, c in m.find(t) if onto.top_type(c) == top} if t.strip() else None
        out.append(row)
    return out


def pruned(base: dict, keep: set[str]) -> Ontology:
    """見た表記だけを残した辞書。表記が 1 つも残らない末端概念は消す。中間概念は残す。"""
    data = copy.deepcopy(base)
    mids = {p for c in data["concepts"] for p in c.get("parents", [])}
    concepts = []
    for c in data["concepts"]:
        if c["id"] in mids:
            concepts.append(c)
            continue
        forms = [c["label"], *c.get("aliases", [])]
        kept = [f for f in forms if f in keep]
        if not kept:
            continue
        c = dict(c)
        c["label"], c["aliases"] = kept[0], kept[1:]
        concepts.append(c)
    data["concepts"] = concepts
    return Ontology(data)


def measure(df: pd.DataFrame, full_hits: list[dict], onto: Ontology) -> dict:
    hits = doc_hits(df, onto)
    out = {}
    for col in SECTIONS:
        num = den = zero = n = 0
        for fh, h in zip(full_hits, hits):
            if fh[col] is None:
                continue
            n += 1
            full_c = {c for _, c in fh[col]}
            now_c = {c for _, c in h[col]}
            den += len(full_c)
            num += len(full_c & now_c)
            zero += not now_c
        out[f"{col}_recall"] = num / den if den else np.nan
        out[f"{col}_zero"] = zero / n if n else np.nan
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    full = Ontology(base)
    inorg = pd.read_csv(ROOT / "datasets/inorganic1200/documents_sections.csv", dtype=str, keep_default_na=False)
    num = inorg["doc_id"].str.extract(r"(\d+)")[0].astype(int)
    dev, chk = inorg[num % 2 == 1].reset_index(drop=True), inorg[num % 2 == 0].reset_index(drop=True)
    mof = pd.read_csv(ROOT / "datasets/mof648/documents_sections.csv", dtype=str, keep_default_na=False)
    dev_hits = doc_hits(dev, full)
    targets = {"確認用": (chk, doc_hits(chk, full)), "MOF": (mof, doc_hits(mof, full))}
    n_forms = len(full.surface_forms())
    rows = []
    for n in NS:
        for rep in range(REPEATS if n < len(dev) else 1):
            idx = random.Random(1000 * n + rep).sample(range(len(dev)), n)
            keep = {s for i in idx for col in SECTIONS if dev_hits[i][col] for s, _ in dev_hits[i][col]}
            onto = pruned(base, keep)
            leaves = sum(1 for c in onto.concepts.values() if c.concept_id not in onto.top_types and not any(c.concept_id in x.parent_ids for x in onto.concepts.values()))
            for name, (df, fh) in targets.items():
                r = {"N": n, "rep": rep, "対象": name, "表記数": len(keep), "表記の割合": len(keep) / n_forms, "末端概念数": leaves}
                r.update(measure(df, fh, onto))
                rows.append(r)
            print(n, rep, len(keep), flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "curve.csv", index=False)
    # 全部入り（inorg-1.0 そのもの）の取りこぼし率も残す
    ref = [{"対象": name, **measure(df, fh, full)} for name, (df, fh) in targets.items()]
    pd.DataFrame(ref).to_csv(OUT / "full.csv", index=False)
    print(out.groupby(["対象", "N"]).mean(numeric_only=True).round(3).to_string())




def plot() -> None:
    """curve.csv から図（curve.png）を描く。使い方: python scripts/common_layer_curve.py plot"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    for f in ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans CJK JP"]:
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    d = pd.read_csv(OUT / "curve.csv")
    cols = {"problem": ("課題", "#2a78d6"), "solution": ("解決手段", "#eb6834"), "use_text": ("用途", "#1baf7a")}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, tgt in zip(axes, ["確認用", "MOF"]):
        s = d[d["対象"] == tgt].groupby("N")
        for c, (lab, col) in cols.items():
            m, lo, hi = s[f"{c}_recall"].mean(), s[f"{c}_recall"].min(), s[f"{c}_recall"].max()
            ax.plot(m.index, m.values, "-o", color=col, label=lab, ms=4)
            ax.fill_between(m.index, lo, hi, color=col, alpha=.15, lw=0)
        ax.axvline(300, color="#172B3A", alpha=.35, lw=1, ls="--")
        ax.set_title({"確認用": "無機材料 確認用 600 件", "MOF": "MOF 614 件（共通層を作るときに見ていない分野）"}[tgt], fontsize=11)
        ax.set_xlabel("共通層を作るのに読んだ特許の件数（無機材料の開発用から無作為）")
        ax.set_ylim(0, 1.02)
        ax.grid(alpha=.3)
    axes[0].set_ylabel("全部入りの inorg-1.0 で拾える概念のうち\n縮めた辞書でも拾える割合")
    axes[0].legend(loc="lower right")
    fig.suptitle("共通層は 300 件で 85〜96% に届き、400→600 件では 100 件あたり 0.5〜2.2 ポイントしか増えない", fontsize=12)
    fig.text(0.01, 0.005, "線は 5 回の平均、帯は最小〜最大。読んだ文書に出た表記はすべて辞書に入れたと仮定した上限。"
             "出典: 無機材料 1,200 件・MOF 614 件（Summaria-MCP(Model Context Protocol) で収集）", fontsize=8, color="#4A5866")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(OUT / "curve.png", dpi=150)


if __name__ == "__main__":
    if sys.argv[1:] == ["plot"]:
        plot()
    else:
        main()
        plot()
