"""用途のまとまりを、オントロジーと無関係な物差しで比べる。

物差し:
- ipc: 審査官が付けた IPC から用途を割り当てる（多ラベル）。どのオントロジーにも依存しない。
- ontology: 指定したオントロジー（評価に使う方式とは別のもの）の用途概念から割り当てる。

比べる方式:
- V-concat（文埋め込み）
- P2（関係方式、全部）
- P2 用途抜き（用途概念と、それを端点に持つ三つ組を除く）
- P2 用途だけ（用途概念を端点に持つ三つ組だけ）

python -m ontomap.patents.usage_eval --config config/experiment_mof648.yaml --labels ipc
"""
from __future__ import annotations

import argparse
import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.spatial.distance import pdist, squareform

from ..ontology import Ontology
from ..reduce import run_umap, to_distance
from ..schema import GraphLayer
from ..similarity.graph_sim import RelationConfig, relation_similarity

# IPC（サブクラスまたはメイングループの前方一致）→ 用途。MOF/無機材料の特許で用途を表す分類だけを選ぶ。
IPC_USAGE: list[tuple[str, str]] = [
    (r"^B01D53", "ガス分離・ガス精製"),
    (r"^B01D(69|71)", "分離膜"),
    (r"^F17C", "ガス貯蔵"),
    (r"^C01B3/", "水素の製造・貯蔵"),
    (r"^B01J(2[1-9]|3[0-8])", "触媒"),
    (r"^H01M", "電池・燃料電池"),
    (r"^H01G", "キャパシタ"),
    (r"^G01N", "測定・センサ"),
    (r"^A61[KP]", "医薬"),
    (r"^A61L", "消臭・殺菌"),
    (r"^C02F", "水処理"),
    (r"^H01L|^H10", "電子デバイス"),
]


def ipc_usage_labels(sections_csv: Path, doc_ids: list[str]) -> dict[str, set[str]]:
    """全 IPC から用途ラベルを付ける（筆頭に限らない）。"""
    df = pd.read_csv(sections_csv, dtype=str, keep_default_na=False).set_index("doc_id")
    out = {}
    for d in doc_ids:
        codes = [c.strip() for c in re.split(r";", df.loc[d, "ipc"]) if c.strip()]
        out[d] = {lab for pat, lab in IPC_USAGE for c in codes if re.search(pat, c)}
    return out


def ontology_usage_labels(sections_csv: Path, onto_path: Path, doc_ids: list[str]) -> dict[str, set[str]]:
    """指定オントロジーの用途概念から付ける（snapshots.usage_labels を使う）。"""
    from ..snapshots import usage_labels

    lab = usage_labels(sections_csv, Ontology.load(onto_path))
    return {d: lab.get(d, set()) for d in doc_ids}


def _per_doc(xy: np.ndarray, member: np.ndarray, k: int = 10) -> np.ndarray:
    D = squareform(pdist(xy))
    np.fill_diagonal(D, np.inf)
    return np.array([member[np.argsort(D[i])[:k]].mean() for i in np.where(member)[0]])


def evaluate(run_dir: Path, onto: Ontology, rel_cfg: dict, labels: dict[str, set[str]], doc_ids: list[str], seeds: int = 5, min_docs: int = 15, n_boot: int = 2000) -> tuple[pd.DataFrame, dict]:
    """方式ごとの近傍一致率（乱数種で平均）と、文埋め込みとの差の 95% 区間。"""
    L = GraphLayer.load(run_dir / "graphs", "dict", doc_ids)

    def is_u(c: str) -> bool:
        return onto.is_known(c) and onto.top_type(c) == "U"

    no_u = GraphLayer("noU", {d: [t for t in ts if not (is_u(t.subject_id) or is_u(t.object_id))] for d, ts in L.triples.items()}, L.concepts)
    only_u = GraphLayer("onlyU", {d: [t for t in ts if is_u(t.subject_id) or is_u(t.object_id)] for d, ts in L.triples.items()}, L.concepts)
    rc = RelationConfig.from_dict(rel_cfg)
    Dz = np.load(run_dir / "maps" / "_distances.npz")
    mats = {
        "文埋め込み": Dz["V-concat"],
        "関係方式（全部）": Dz["P2__dict"],
        "関係方式（用途抜き）": to_distance(relation_similarity(no_u, doc_ids, onto, rc)[0])[0],
        "関係方式（用途だけ）": to_distance(relation_similarity(only_u, doc_ids, onto, rc)[0])[0],
    }
    info = {
        "triples": {"all": sum(map(len, L.triples.values())), "no_usage": sum(map(len, no_u.triples.values())), "usage_only": sum(map(len, only_u.triples.values()))},
        "incomparable_docs": {"all": sum(1 for d in doc_ids if not L.triples[d]), "no_usage": sum(1 for d in doc_ids if not no_u.triples[d]), "usage_only": sum(1 for d in doc_ids if not only_u.triples[d])},
        "labeled_docs": sum(bool(s) for s in labels.values()),
    }
    cats = [c for c in sorted({c for s in labels.values() for c in s}) if sum(c in labels[d] for d in doc_ids) >= min_docs]
    per = {m: {c: [] for c in cats} for m in mats}
    for seed in range(seeds):
        for m, D in mats.items():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                Y = run_umap(D, seed)
            for c in cats:
                per[m][c].append(_per_doc(Y, np.array([c in labels[d] for d in doc_ids])))
    rng = np.random.default_rng(0)
    rows = []
    for c in cats:
        n = int(sum(c in labels[d] for d in doc_ids))
        base = np.mean(per["文埋め込み"][c], axis=0)
        r = {"用途": c, "件数": n, "無作為": round((n - 1) / (len(doc_ids) - 1), 2), "文埋め込み": round(float(base.mean()), 2)}
        for m in list(mats)[1:]:
            v = np.mean(per[m][c], axis=0)
            d = v - base
            bs = [rng.choice(d, len(d)).mean() for _ in range(n_boot)]
            r[m] = round(float(v.mean()), 2)
            r[f"{m} 差"] = round(float(d.mean()), 2)
            r[f"{m} 差の95%区間"] = f"{np.percentile(bs, 2.5):+.2f}〜{np.percentile(bs, 97.5):+.2f}"
        rows.append(r)
    return pd.DataFrame(rows).sort_values("件数", ascending=False), info


def main() -> None:
    """エントリポイント。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--labels", default="ipc", help="ipc か ontology:<YAML のパス>")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / a.config).read_text(encoding="utf-8"))
    run_dir = root / "runs" / cfg["run_name"]
    sections = root / (cfg.get("data") or cfg["corpus"])["documents_csv"]
    onto = Ontology.load(root / cfg["ontology"])
    doc_ids = pd.read_csv(run_dir / "data" / "documents.csv", dtype=str).doc_id.tolist()
    if a.labels == "ipc":
        labels = ipc_usage_labels(sections, doc_ids)
        tag = "ipc"
    else:
        path = root / a.labels.split(":", 1)[1]
        labels = ontology_usage_labels(sections, path, doc_ids)
        tag = "onto_" + path.stem
    table, info = evaluate(run_dir, onto, cfg["relation_similarity"], labels, doc_ids)
    out = Path(a.out) if a.out else run_dir / "report" / f"usage_eval_{tag}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    print(info)
    pd.set_option("display.width", 250)
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
