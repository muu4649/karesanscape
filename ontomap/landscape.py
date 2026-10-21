"""等高線ランドスケープ図: 文書の密度を等高線で描き、まとまりに見出しを置き、
出願人（または用途）の範囲と、出願年に沿った重心の軌跡を重ねる。2 手法を並べて比べる。

見出しは両手法とも同じ手順（まとまりごとに、全体より多く出る概念のオントロジー名）で付けるので、
手法の違いは配置の違いだけになる。
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from scipy.stats import gaussian_kde  # noqa: E402
from sklearn.cluster import KMeans  # noqa: E402

plt.rcParams["font.family"] = ["Hiragino Sans", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
CONTOUR = "#2a78d6"
FILL = LinearSegmentedColormap.from_list("land", ["#fcfcfb", "#cde2fb", "#86b6ef"])
GROUP_COLORS = ["#eb6834", "#1baf7a"]  # 2 群まで（全ペア判別を通る組）
SUFFIX = re.compile(r"(株式会社|国立大学法人|公立大学法人|学校法人|国立研究開発法人|独立行政法人|一般財団法人|公益財団法人|有限会社|合同会社)")


def short_applicant(name: str) -> str:
    """表示用に法人格を外した出願人名。"""
    n = unicodedata.normalize("NFKC", name).strip()
    n = SUFFIX.sub("", n).strip()
    return re.sub(r"\s+", " ", n)


def applicants_of(cell: str) -> list[str]:
    """「;」区切りの出願人欄を、表示用の名前の並びにする。"""
    return [short_applicant(a) for a in re.split(r"[;；]", cell or "") if a.strip()]


def _normalize_xy(xy: np.ndarray) -> np.ndarray:
    c = xy - np.median(xy, axis=0)
    r = np.percentile(np.linalg.norm(c, axis=1), 97)
    return c / (r if r > 0 else 1.0)


def _kde_grid(xy: np.ndarray, lim: float, n: int = 260, bw: float = 0.12):
    gx, gy = np.meshgrid(np.linspace(-lim, lim, n), np.linspace(-lim, lim, n))
    k = gaussian_kde(xy.T, bw_method=bw)
    z = k(np.vstack([gx.ravel(), gy.ravel()])).reshape(gx.shape)
    return gx, gy, z


def _mass_level(z: np.ndarray, mass: float) -> float:
    """密度の高い側から mass の割合を含む等高線の高さ。"""
    v = np.sort(z.ravel())[::-1]
    c = np.cumsum(v)
    return float(v[np.searchsorted(c, mass * c[-1])])


def cluster_labels(xy: np.ndarray, doc_concepts: list[set[str]], onto, k: int, seed: int, types: tuple[str, ...] = ("U", "M")) -> list[tuple[np.ndarray, list[str], int]]:
    """2D 上で k 個のまとまりに分け、型ごとに全体より多く出る概念の名前を見出しにする。"""
    km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(xy)
    total = Counter(c for s in doc_concepts for c in s)
    n = len(doc_concepts)
    out = []
    for c in range(k):
        idx = np.where(km.labels_ == c)[0]
        if len(idx) < 4:
            continue
        cnt = Counter(cc for i in idx for cc in doc_concepts[i])
        lines = []
        for t in types:
            cands = [(cnt[cc] / len(idx)) * np.log((cnt[cc] / len(idx)) / (total[cc] / n)) for cc in cnt if onto.top_type(cc) == t and cnt[cc] >= max(2, 0.2 * len(idx))]
            names = [cc for cc in cnt if onto.top_type(cc) == t and cnt[cc] >= max(2, 0.2 * len(idx))]
            if names:
                best = names[int(np.argmax(cands))]
                lines.append(onto.label(best))
        if lines:
            out.append((xy[idx].mean(axis=0), lines, len(idx)))
    return out


def trajectory(xy: np.ndarray, years: np.ndarray, n_steps: int = 4) -> tuple[np.ndarray, list[str]]:
    """出願日の順に同じ件数ずつ n_steps 組に分け、各組の重心と年の範囲を返す。"""
    order = np.argsort(years, kind="stable")
    groups = [g for g in np.array_split(order, min(n_steps, max(1, len(order) // 2))) if len(g)]
    pts = np.array([np.median(xy[g], axis=0) for g in groups])
    labs = [f"{int(years[g].min())}" if years[g].min() == years[g].max() else f"{int(years[g].min())}–{str(int(years[g].max()))[-2:]}" for g in groups]
    return pts, labs


def _smooth(pts: np.ndarray, rounds: int = 3) -> np.ndarray:
    """Chaikin の角削りで折れ線をなめらかにする。点の間で膨らまない（無い回り込みを描かない）。"""
    p = pts.copy()
    for _ in range(rounds):
        if len(p) < 3:
            return p
        q = [p[0]]
        for a, b in zip(p[:-1], p[1:]):
            q += [0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b]
        q.append(p[-1])
        p = np.array(q)
    return p


def _repel(pos: np.ndarray, sizes: np.ndarray, fixed: np.ndarray | None = None, iters: int = 200) -> np.ndarray:
    """文字の箱（幅, 高さ）が重ならないよう位置を少しずつ押し離す。fixed は動かさない障害物の位置。"""
    pos = pos.copy()
    obstacles = fixed if fixed is not None else np.zeros((0, 2))
    for _ in range(iters):
        moved = False
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                d = pos[j] - pos[i]
                ox = (sizes[i, 0] + sizes[j, 0]) / 2 - abs(d[0])
                oy = (sizes[i, 1] + sizes[j, 1]) / 2 - abs(d[1])
                if ox > 0 and oy > 0:
                    moved = True
                    if oy < ox:
                        step = np.array([0, np.sign(d[1]) or 1.0]) * oy / 2
                    else:
                        step = np.array([np.sign(d[0]) or 1.0, 0]) * ox / 2
                    pos[i] -= step
                    pos[j] += step
            for o in obstacles:
                d = pos[i] - o
                ox, oy = sizes[i, 0] / 2 + 0.13 - abs(d[0]), sizes[i, 1] / 2 + 0.06 - abs(d[1])  # 年の文字（約 0.26×0.12）を箱とみなす
                if ox > 0 and oy > 0:
                    moved = True
                    pos[i] += np.array([0, (np.sign(d[1]) or 1.0) * oy]) if oy < ox else np.array([(np.sign(d[0]) or 1.0) * ox, 0])
        if not moved:
            break
    return pos


def _halo(width: float = 3.0):
    return [pe.withStroke(linewidth=width, foreground=SURFACE)]


def draw_panel(ax, xy: np.ndarray, doc_concepts: list[set[str]], onto, groups: list[tuple[str, np.ndarray]], years: np.ndarray, title: str, k: int, seed: int) -> list[dict]:
    """1 手法ぶんのランドスケープ。groups は (名前, 文書の真偽配列)。"""
    lim = 1.2
    gx, gy, z = _kde_grid(xy, lim)
    ax.set_facecolor(SURFACE)
    levels = np.linspace(z.max() * 0.025, z.max(), 24)
    ax.contourf(gx, gy, z, levels=levels, cmap=FILL, alpha=0.55, zorder=0)
    ax.contour(gx, gy, z, levels=levels, colors=CONTOUR, linewidths=0.45, alpha=0.6, zorder=1)
    for r in (0.5, 1.0):
        ax.add_patch(plt.Circle((0, 0), r, fill=False, color="#c3c2b7", lw=0.6, ls=(0, (2, 3)), zorder=1))
    summary = []
    year_pts = []
    for gi, (name, member) in enumerate(groups):
        col = GROUP_COLORS[gi % 2]
        if member.sum() >= 3:
            _, _, zg = _kde_grid(xy[member], lim, bw=0.35)
            l80, l50 = _mass_level(zg, 0.8), _mass_level(zg, 0.5)
            ax.contourf(gx, gy, zg, levels=[l80, l50, zg.max() * 1.01], colors=[col, col], alpha=0.16, zorder=2)
            ax.contour(gx, gy, zg, levels=[l80], colors=[col], linewidths=1.0, alpha=0.7, zorder=2)
        pts, labs = trajectory(xy[member], years[member])
        line = _smooth(pts)
        ax.plot(line[:, 0], line[:, 1], color=col, lw=2.2, zorder=5, solid_capstyle="round", path_effects=[pe.withStroke(linewidth=4, foreground=SURFACE)])
        if len(line) >= 2:
            ax.annotate("", xy=line[-1], xytext=line[-2], arrowprops=dict(arrowstyle="-|>", color=col, lw=2.2, mutation_scale=16), zorder=6)
        ax.scatter(pts[:, 0], pts[:, 1], s=26, color=col, edgecolors=SURFACE, linewidths=1.5, zorder=6)
        # 始点の年は左下、終点の年は右上に置く（両端が近くても重ならない）
        for p, lab, sgn in ((pts[0], labs[0], -1), (pts[-1], labs[-1], 1)):
            ax.text(p[0] + sgn * 0.03, p[1] + sgn * 0.035, lab, color=col, fontsize=9, fontweight="bold", ha="left" if sgn > 0 else "right", va="bottom" if sgn > 0 else "top", zorder=7, path_effects=_halo())
            year_pts.append(p + np.array([sgn * 0.14, sgn * 0.07]))
        year_pts.extend(line[:: max(1, len(line) // 12)])
        summary.append({"group": name, "n_docs": int(member.sum()), "path_length": float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()), "start": labs[0], "end": labs[-1]})
    labs = cluster_labels(xy, doc_concepts, onto, k, seed)
    if labs:
        pos = np.array([c for c, _, _ in labs])
        sizes = np.array([[max(len(t) for t in ls) * 0.042 + 0.04, 0.075 * len(ls) + 0.03] for _, ls, _ in labs])
        pos = _repel(pos, sizes, np.array(year_pts) if year_pts else None)
        for p, (_, lines, n) in zip(pos, labs):
            ax.text(p[0], p[1], "\n".join(lines), ha="center", va="center", fontsize=9.5, color=INK, linespacing=1.15, zorder=8, path_effects=_halo(3.5))
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color("#e2e1dc")
    ax.set_title(title, fontsize=12.5, loc="left", color=INK, pad=8)
    return summary


def build_landscape(run_dir: Path, sections_csv: Path, onto, methods: list[str], method_names: dict[str, str], reducer: str, out_path: Path,
                    group_by: str = "applicant", groups: list[str] | None = None, k: int = 9, seed: int = 42, usage_labels: dict[str, set[str]] | None = None) -> dict:
    """2 手法を並べたランドスケープ図を書く。group_by は applicant（出願人）か usage（用途）。"""
    coords = pd.read_csv(run_dir / "maps" / "map_coords.csv")
    sec = pd.read_csv(sections_csv, dtype=str, keep_default_na=False).set_index("doc_id")
    doc_ids = pd.read_csv(run_dir / "data" / "documents.csv", dtype=str).doc_id.tolist()
    sec = sec.loc[doc_ids]
    years = np.array([min(int(str(d)[:4]), 2025) if str(d)[:4].isdigit() else 2018 for d in sec.filing_date], dtype=float)
    concepts: dict[str, set[str]] = {d: set() for d in doc_ids}
    for line in (run_dir / "graphs" / "dict" / "concepts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            m = json.loads(line)
            if m["doc_id"] in concepts:
                concepts[m["doc_id"]].add(m["concept_id"])
    doc_concepts = [concepts[d] for d in doc_ids]
    if group_by == "applicant":
        apps = [applicants_of(a) for a in sec.applicants]
        if groups is None:
            groups = [a for a, _ in Counter(x for s in apps for x in set(s)).most_common(2)]
        members = [(g, np.array([g in s for s in apps])) for g in groups]
        legend_title = "出願人"
    else:
        labels = usage_labels or {}
        if groups is None:
            groups = [a for a, _ in Counter(c for d in doc_ids for c in labels.get(d, set())).most_common(2)]
        members = [(g, np.array([g in labels.get(d, set()) for d in doc_ids])) for g in groups]
        legend_title = "用途"
    fig, axes = plt.subplots(1, len(methods), figsize=(7.4 * len(methods), 7.9))
    fig.patch.set_facecolor(SURFACE)
    result = {"reducer": reducer, "group_by": group_by, "panels": {}}
    for ax, m in zip(np.atleast_1d(axes), methods):
        g = coords[(coords.method == m) & (coords.reducer == reducer)].set_index("doc_id").loc[doc_ids]
        xy = _normalize_xy(g[["x", "y"]].to_numpy())
        result["panels"][m] = draw_panel(ax, xy, doc_concepts, onto, members, years, method_names.get(m, m), k, seed)
    handles = [plt.Line2D([], [], color=GROUP_COLORS[i % 2], lw=2.2, marker="o", markersize=5, label=f"{g}（{int(mm.sum())} 件）") for i, (g, mm) in enumerate(members)]
    handles.append(plt.Line2D([], [], color=CONTOUR, lw=0.8, alpha=0.7, label="全文書の密度（等高線）"))
    fig.legend(handles=handles, title=f"{legend_title}の範囲（濃い面 = 文書の半分、線 = 8 割）と出願年に沿った重心の動き", loc="lower center", ncol=len(handles), frameon=False, fontsize=10, title_fontsize=10)
    red_name = "UMAP" if reducer == "umap" else "Metric MDS"
    fig.suptitle(f"MOF/PCP 国内特許 {len(doc_ids)} 件のランドスケープ（{red_name}）　見出し = まとまりに多い用途／解決手段", fontsize=13.5, x=0.02, ha="left", color=INK)
    fig.tight_layout(rect=(0, 0.07, 1, 0.95))
    fig.savefig(out_path, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return result
