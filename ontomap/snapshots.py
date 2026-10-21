"""カテゴリ（用途・領域など）ごとの「出具合」を手法間で比べるスナップ図。

- grid: 行 = カテゴリ、列 = 手法。そのカテゴリの文書だけ色を付け、ほかは灰色。
- morph: 1 カテゴリについて、手法 A の配置から B の配置へ動く途中を数コマ。
- purity: 2D の 10 近傍のうち同じカテゴリの文書の割合（近傍一致率）を、出現率（無作為の期待値）と並べる。

カテゴリは 1 文書に複数付いてよい（多ラベル）。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.spatial.distance import pdist, squareform  # noqa: E402

plt.rcParams["font.family"] = ["Hiragino Sans", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

HIGHLIGHT = "#2a78d6"
BACKGROUND = "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # 3 手法まで（散布・点図で全ペア判別を通る順）
INK = "#52514e"


def _xy(coords: pd.DataFrame, method: str, reducer: str, doc_ids: list[str]) -> np.ndarray:
    g = coords[(coords.method == method) & (coords.reducer == reducer)].set_index("doc_id").loc[doc_ids]
    return g[["x", "y"]].to_numpy()


def neighbor_purity(xy: np.ndarray, member: np.ndarray, k: int = 10) -> float:
    """カテゴリの文書について、2D の k 近傍のうち同じカテゴリの割合の平均。"""
    D = squareform(pdist(xy))
    np.fill_diagonal(D, np.inf)
    idx = np.where(member)[0]
    if len(idx) < 2:
        return float("nan")
    return float(np.mean([member[np.argsort(D[i])[:k]].mean() for i in idx]))


def purity_table(coords: pd.DataFrame, labels: dict[str, set[str]], methods: list[str], reducer: str, categories: list[str], k: int = 10) -> pd.DataFrame:
    """カテゴリ × 手法の近傍一致率と、出現率（無作為に並べたときの期待値）。"""
    doc_ids = list(labels)
    rows = []
    for c in categories:
        member = np.array([c in labels[d] for d in doc_ids])
        prev = (member.sum() - 1) / (len(doc_ids) - 1)
        for m in methods:
            rows.append({"category": c, "n_docs": int(member.sum()), "method": m, "reducer": reducer, "purity@k": neighbor_purity(_xy(coords, m, reducer, doc_ids), member, k), "expected_random": prev})
    df = pd.DataFrame(rows)
    df["lift"] = df["purity@k"] / df["expected_random"]
    return df


def robust_lim(*arrays: np.ndarray, q: float = 97.0) -> float:
    """外れ点に引っ張られないよう、座標の絶対値の分位点で表示範囲を決める。"""
    v = np.concatenate([np.abs(a - np.median(a, axis=0)).max(axis=1) for a in arrays])  # 1 点 1 値
    return float(np.percentile(v, q) * 1.25)


def _panel(ax, xy: np.ndarray, member: np.ndarray, lim: float) -> int:
    """散布を描く。表示範囲の外の点は縁に寄せて白抜きで描き、その件数を返す。"""
    xy = xy - np.median(xy, axis=0)
    out = (np.abs(xy) > lim).any(axis=1)
    c = np.clip(xy, -lim * 0.98, lim * 0.98)
    ax.scatter(c[~member & ~out, 0], c[~member & ~out, 1], s=7, c=BACKGROUND, linewidths=0, alpha=0.7)
    ax.scatter(c[member & ~out, 0], c[member & ~out, 1], s=20, c=HIGHLIGHT, edgecolors="white", linewidths=0.6, zorder=3)
    ax.scatter(c[out, 0], c[out, 1], s=16, facecolors="none", edgecolors=np.where(member[out], HIGHLIGHT, BACKGROUND), linewidths=1.0, zorder=2)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_aspect("equal")
    for s in ax.spines.values():
        s.set_color("#e2e1dc")
    return int(out.sum())


def grid_figure(coords: pd.DataFrame, labels: dict[str, set[str]], methods: list[str], method_names: dict[str, str], reducer: str, categories: list[str], path: Path, k: int = 10, title: str = "") -> pd.DataFrame:
    """行 = カテゴリ、列 = 手法のスナップ図。各コマに近傍一致率を添える。"""
    doc_ids = list(labels)
    pt = purity_table(coords, labels, methods, reducer, categories, k)
    xys = {m: _xy(coords, m, reducer, doc_ids) for m in methods}
    lims = {m: robust_lim(v) for m, v in xys.items()}  # 手法ごとに縮尺を合わせる（形の比較が目的）
    fig, axes = plt.subplots(len(categories), len(methods), figsize=(3.0 * len(methods) + 1.6, 3.0 * len(categories)), squeeze=False)
    for r, c in enumerate(categories):
        member = np.array([c in labels[d] for d in doc_ids])
        for col, m in enumerate(methods):
            ax = axes[r, col]
            n_out = _panel(ax, xys[m], member, lims[m])
            row = pt[(pt.category == c) & (pt.method == m)].iloc[0]
            ax.text(0.03, 0.03, f"近傍一致率 {row['purity@k']:.2f}（無作為 {row['expected_random']:.2f}）", transform=ax.transAxes, fontsize=8, color=INK, va="bottom")
            if n_out:
                ax.text(0.97, 0.97, f"枠外 {n_out} 件（白抜き）", transform=ax.transAxes, fontsize=7.5, color=INK, ha="right", va="top")
            if r == 0:
                ax.set_title(method_names.get(m, m), fontsize=10.5)
            if col == 0:
                ax.set_ylabel(f"{c}\n（{int(member.sum())} 件）", fontsize=10, rotation=0, ha="right", va="center", labelpad=10)
    if title:
        fig.suptitle(title, fontsize=12, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97 if title else 1))
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return pt


def morph_figure(coords: pd.DataFrame, labels: dict[str, set[str]], a: str, b: str, method_names: dict[str, str], reducer: str, category: str, path: Path, steps: tuple[float, ...] = (0.0, 0.33, 0.67, 1.0)) -> None:
    """1 カテゴリについて、A の配置から B の配置へ動く途中のコマ。灰色の線は各文書の移動の向き。"""
    doc_ids = list(labels)
    A, B = _xy(coords, a, reducer, doc_ids), _xy(coords, b, reducer, doc_ids)
    member = np.array([category in labels[d] for d in doc_ids])
    A = A - np.median(A, axis=0)
    B = B - np.median(B, axis=0)
    A, B = A / robust_lim(A), B / robust_lim(B)  # 広がりの違いを除き、形の変化だけを見る
    lim = 1.0
    fig, axes = plt.subplots(1, len(steps), figsize=(3.1 * len(steps), 3.5))
    for ax, t in zip(axes, steps):
        P = A * (1 - t) + B * t
        for i in np.where(member)[0]:
            pa, pb = np.clip(A[i], -lim, lim), np.clip(B[i], -lim, lim)
            ax.plot([pa[0], pb[0]], [pa[1], pb[1]], color=BACKGROUND, lw=0.6, zorder=1)
        _panel(ax, P, member, lim)
        lab = method_names.get(a, a) if t == 0 else method_names.get(b, b) if t == 1 else f"途中 {int(t * 100)}%"
        ax.set_title(lab, fontsize=10)
    fig.suptitle(f"「{category}」の文書の動き（{method_names.get(a, a)} → {method_names.get(b, b)}、{'UMAP' if reducer == 'umap' else 'Metric MDS'}）", fontsize=11.5, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def purity_figure(pt: pd.DataFrame, methods: list[str], method_names: dict[str, str], path: Path, title: str = "") -> None:
    """カテゴリごとの近傍一致率を手法別の点で並べる（灰色の縦棒 = 無作為の期待値）。"""
    cats = list(dict.fromkeys(pt.category))
    fig, ax = plt.subplots(figsize=(7.6, 0.6 * len(cats) + 1.7))
    for r, c in enumerate(cats):
        sub = pt[pt.category == c]
        ax.plot([sub.expected_random.iloc[0]] * 2, [r - 0.32, r + 0.32], color=BACKGROUND, lw=2.5, solid_capstyle="round", zorder=1)
        vals = [sub[sub.method == m]["purity@k"].iloc[0] for m in methods]
        ax.plot([min(vals), max(vals)], [r, r], color="#e2e1dc", lw=1.2, zorder=1)
        for k, (m, v) in enumerate(zip(methods, vals)):
            off = (k - (len(methods) - 1) / 2) * 0.16  # 同じ値でも重ならないよう上下にずらす
            ax.scatter(v, r + off, s=60, color=SERIES[k % 3], edgecolors="white", linewidths=1.5, zorder=3, label=method_names.get(m, m) if r == 0 else None)
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels([f"{c}（{int(pt[pt.category == c].n_docs.iloc[0])}）" for c in cats], fontsize=9.5, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.03)
    ax.set_xlabel("2D の 10 近傍のうち同じカテゴリの文書の割合", fontsize=9.5, color=INK)
    ax.grid(axis="x", color="#ecebe7", lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#e2e1dc")
    h, l = ax.get_legend_handles_labels()
    h.append(plt.Line2D([], [], color=BACKGROUND, lw=2.5))
    l.append("無作為に並べたときの期待値")
    ax.legend(h, l, frameon=False, fontsize=8.5, loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2)
    if title:
        fig.suptitle(title, fontsize=11.5, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.94 if title else 1))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def make_snapshots(run_dir: Path, labels: dict[str, set[str]], methods: list[str], method_names: dict[str, str], categories: list[str], out_dir: Path, morph_pairs: list[tuple[str, str, str]], label_name: str = "カテゴリ", morph_reducer: str = "umap") -> dict[str, Path]:
    """3 種類の図と近傍一致率の表をまとめて書く。morph_pairs は (A, B, カテゴリ) の組。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    coords = pd.read_csv(run_dir / "maps" / "map_coords.csv")
    outs: dict[str, Path] = {}
    tables = []
    for red in ("mds", "umap"):
        p = out_dir / f"snap_grid_{red}.png"
        pt = grid_figure(coords, labels, methods, method_names, red, categories, p, title=f"{label_name}ごとの出具合（{'Metric MDS' if red == 'mds' else 'UMAP'}）")
        outs[f"grid_{red}"] = p
        tables.append(pt)
        q = out_dir / f"snap_purity_{red}.png"
        purity_figure(pt, methods, method_names, q, title=f"{label_name}のまとまり具合（{'Metric MDS' if red == 'mds' else 'UMAP'}）")
        outs[f"purity_{red}"] = q
    for k, (a, b, c) in enumerate(morph_pairs):
        p = out_dir / f"snap_morph_{k + 1}.png"
        morph_figure(coords, labels, a, b, method_names, morph_reducer, c, p)
        outs[f"morph_{k + 1}"] = p
    pd.concat(tables).to_csv(out_dir / "category_purity.csv", index=False)
    return outs


def usage_labels(sections_csv: Path, onto, text_columns: tuple[str, ...] = ("use_text",), top_type: str = "U") -> dict[str, set[str]]:
    """用途記載から、文書ごとの用途の分類（上位型 U の直下の中間概念）を付ける。

    注意: 関係方式（P1/P2）も同じオントロジーの用途概念を使うので、この分類での比較は関係方式に有利に働く。
    用途の「出具合」を見る目安であり、品質の根拠にはしない。
    """
    from .extract.dictionary import find_mentions

    df = pd.read_csv(sections_csv, dtype=str, keep_default_na=False)

    def category(cid: str) -> str | None:
        if onto.top_type(cid) != top_type:
            return None
        cur = cid
        while True:
            parents = onto.concepts[cur].parent_ids
            if not parents or top_type in parents:
                return onto.label(cur)
            cur = parents[0]

    out: dict[str, set[str]] = {}
    for r in df.to_dict("records"):
        text = "\n".join(r.get(c, "") for c in text_columns)
        cats = {category(cid) for _, _, _, cid in find_mentions(text, onto)}
        out[r["doc_id"]] = {c for c in cats if c}
    return out
