"""特許の比較図をまとめて作る: 用途のスナップ図、等高線ランドスケープ図、用途ごとの差のぶれ。

python -m ontomap.patents.figures --config config/experiment_mof648.yaml
"""
from __future__ import annotations

import argparse
import json
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.spatial.distance import pdist, squareform

from ..landscape import build_landscape
from ..ontology import Ontology
from ..reduce import run_umap
from ..snapshots import make_snapshots, usage_labels

# 用途の色分けで並べる組（件数の多い用途は全体を覆って違いが見えないため、比べがいのある組を選ぶ）
USAGE_PAIRS = [("有害物質の除去", "水吸着"), ("電気化学デバイス", "センサ")]

NAMES = {
    "V-concat": "文埋め込み（全段落を連結）",
    "V-section": "文埋め込み（段落ごと）",
    "P2@dict": "関係グラフ（課題・解決手段・用途）",
}


def _per_doc_purity(xy: np.ndarray, member: np.ndarray, k: int = 10) -> np.ndarray:
    D = squareform(pdist(xy))
    np.fill_diagonal(D, np.inf)
    return np.array([member[np.argsort(D[i])[:k]].mean() for i in np.where(member)[0]])


def stability(run_dir: Path, doc_ids: list[str], labels: dict[str, set[str]], cats: list[str], a: str = "V-concat", b: str = "P2@dict", seeds: int = 5, n_boot: int = 2000) -> pd.DataFrame:
    """用途ごとの近傍一致率の差（b − a）を、文書の再抽出と UMAP の乱数種で測る。"""
    D = np.load(run_dir / "maps" / "_distances.npz")
    mats = {a: D[a.replace("@", "__")], b: D[b.replace("@", "__")]}
    rows = []
    for seed in range(seeds):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            Y = {m: run_umap(d, seed) for m, d in mats.items()}
        for c in cats:
            member = np.array([c in labels[d] for d in doc_ids])
            rows.append({"seed": seed, "cat": c, "n": int(member.sum()), "a": _per_doc_purity(Y[a], member), "b": _per_doc_purity(Y[b], member)})
    rng = np.random.default_rng(0)
    out = []
    for c in cats:
        sub = [r for r in rows if r["cat"] == c]
        # 文書ごとの一致率を乱数種で平均し、その差を文書単位で再抽出する（表の列はすべて同じ平均にそろえる）
        pa = np.mean([r["a"] for r in sub], axis=0)
        pb = np.mean([r["b"] for r in sub], axis=0)
        d = pb - pa
        bs = [rng.choice(d, len(d)).mean() for _ in range(n_boot)]
        diffs = [r["b"].mean() - r["a"].mean() for r in sub]
        out.append({"用途": c, "件数": sub[0]["n"], "無作為": round((sub[0]["n"] - 1) / (len(doc_ids) - 1), 2),
                    NAMES.get(a, a): round(float(pa.mean()), 2), NAMES.get(b, b): round(float(pb.mean()), 2),
                    "差": round(float(d.mean()), 2), "差の 95% 区間（文書の再抽出）": f"{np.percentile(bs, 2.5):.2f}〜{np.percentile(bs, 97.5):.2f}",
                    f"差の幅（乱数種 {seeds} 通り）": f"{min(diffs):.2f}〜{max(diffs):.2f}"})
    return pd.DataFrame(out)


def main() -> None:
    """エントリポイント。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--n-categories", type=int, default=8)
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / a.config).read_text(encoding="utf-8")) if not Path(a.config).is_absolute() else yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    run_dir = root / "runs" / cfg["run_name"]
    sections = root / (cfg.get("data") or cfg["corpus"])["documents_csv"]
    onto = Ontology.load(root / cfg["ontology"])
    out = run_dir / "report"
    doc_ids = pd.read_csv(run_dir / "data" / "documents.csv", dtype=str).doc_id.tolist()
    labels_all = usage_labels(sections, onto)
    labels = {d: labels_all.get(d, set()) for d in doc_ids}
    cnt = Counter(c for s in labels.values() for c in s)
    cats = [c for c, n in cnt.most_common(a.n_categories) if n >= 10]
    print("用途の件数:", cnt.most_common(14), "分類が付いた文書", sum(bool(s) for s in labels.values()), "/", len(labels), flush=True)
    methods = ["V-concat", "V-section", "P2@dict"]
    make_snapshots(run_dir, labels, methods, NAMES, cats, out / "snapshots", [("V-concat", "P2@dict", c) for c in cats[:4]], label_name="用途")
    land = out / "landscape"
    land.mkdir(parents=True, exist_ok=True)
    res = {}
    for red in ("umap", "mds"):
        res[f"applicant_{red}"] = build_landscape(run_dir, sections, onto, ["V-concat", "P2@dict"], NAMES, red, land / f"landscape_applicant_{red}.png")
        for k, pair in enumerate(USAGE_PAIRS):
            groups = [g for g in pair if g in cats]
            if len(groups) == 2:
                res[f"usage{k + 1}_{red}"] = build_landscape(run_dir, sections, onto, ["V-concat", "P2@dict"], NAMES, red, land / f"landscape_usage{k + 1}_{red}.png", group_by="usage", groups=groups, usage_labels=labels)
    (land / "landscape_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    st = stability(run_dir, doc_ids, labels, cats)
    st.to_csv(out / "snapshots" / "usage_stability.csv", index=False)
    print(st.to_string(index=False))
    print(json.dumps(res, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
