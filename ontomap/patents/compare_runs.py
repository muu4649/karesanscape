"""オントロジー違いの実行を、オントロジーと無関係な物差し（IPC 由来の用途）で比べる図と表。

python -m ontomap.patents.compare_runs --base config/experiment_mof648.yaml --new config/experiment_mof648_inorg.yaml
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..extract.dictionary import find_mentions
from ..landscape import build_landscape
from ..ontology import Ontology
from ..snapshots import grid_figure, morph_figure, purity_figure
from .usage_eval import evaluate, ipc_usage_labels

ROOT = Path(__file__).resolve().parents[2]


def _load(cfg_path: str) -> tuple[dict, Path, Path, Ontology]:
    cfg = yaml.safe_load((ROOT / cfg_path).read_text(encoding="utf-8"))
    return cfg, ROOT / "runs" / cfg["run_name"], ROOT / (cfg.get("data") or cfg["corpus"])["documents_csv"], Ontology.load(ROOT / cfg["ontology"])


def coverage(sections_csv: Path, onto: Ontology) -> dict[str, float]:
    """段落ごとに、対応する上位型の概念が 1 つも拾えない文書の割合（空の段落は除く）。"""
    import unicodedata

    df = pd.read_csv(sections_csv, dtype=str, keep_default_na=False)
    out = {}
    for col, top in (("problem", "P"), ("solution", "M"), ("use_text", "U")):
        texts = [unicodedata.normalize("NFKC", t) for t in df[col] if t.strip()]
        zero = sum(1 for t in texts if not any(onto.top_type(c) == top for _, _, _, c in find_mentions(t, onto)))
        out[col] = round(zero / len(texts), 3) if texts else float("nan")
    return out


def main() -> None:
    """エントリポイント。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="前回の実行（MOF オントロジー）の設定")
    ap.add_argument("--new", required=True, help="新しい実行（無機材料オントロジー）の設定")
    a = ap.parse_args()
    cfg_b, run_b, sec, onto_b = _load(a.base)
    cfg_n, run_n, _, onto_n = _load(a.new)
    out = run_n / "report" / "compare"
    out.mkdir(parents=True, exist_ok=True)
    doc_ids = pd.read_csv(run_n / "data" / "documents.csv", dtype=str).doc_id.tolist()
    labels = ipc_usage_labels(sec, doc_ids)

    # 1. IPC 由来の用途での近傍一致率（乱数種 5 通りの平均）
    tb, info_b = evaluate(run_b, onto_b, cfg_b["relation_similarity"], labels, doc_ids)
    tn, info_n = evaluate(run_n, onto_n, cfg_n["relation_similarity"], labels, doc_ids)
    tb.to_csv(out / "usage_ipc_mof_ontology.csv", index=False)
    tn.to_csv(out / "usage_ipc_inorganic_ontology.csv", index=False)
    merged = tn[["用途", "件数", "無作為", "文埋め込み"]].merge(
        tb[["用途", "関係方式（全部）", "関係方式（全部） 差", "関係方式（全部） 差の95%区間"]].rename(columns=lambda c: c.replace("関係方式", "MOF オントロジー")), on="用途"
    ).merge(
        tn[["用途", "関係方式（全部）", "関係方式（全部） 差", "関係方式（全部） 差の95%区間", "関係方式（用途抜き）", "関係方式（用途抜き） 差の95%区間"]].rename(columns=lambda c: c.replace("関係方式", "無機オントロジー")), on="用途"
    )
    merged.to_csv(out / "usage_ipc_comparison.csv", index=False)

    names = {"emb": "文埋め込み", "mof": "関係方式（MOF オントロジー）", "inorg": "関係方式（無機材料オントロジー）"}
    rows = []
    for r in merged.itertuples(index=False):
        d = r._asdict()
        for key, col in (("emb", "文埋め込み"), ("mof", "MOF オントロジー（全部）"), ("inorg", "無機オントロジー（全部）")):
            rows.append({"category": d["用途"], "n_docs": d["件数"], "method": key, "purity@k": merged.loc[merged.用途 == d["用途"], col].iloc[0], "expected_random": d["無作為"]})
    pt = pd.DataFrame(rows)
    purity_figure(pt, ["emb", "mof", "inorg"], names, out / "fig_usage_purity_ipc.png", title="用途（IPC から判定）のまとまり具合　UMAP・乱数種 5 通りの平均")

    # 2. 並べ図とコマ送り（座標は各実行の V-concat に重ねてあるので、そのまま並べられる）
    cb = pd.read_csv(run_b / "maps" / "map_coords.csv")
    cn = pd.read_csv(run_n / "maps" / "map_coords.csv")
    coords = pd.concat([
        cn[cn.method == "V-concat"].assign(method="emb"),
        cb[cb.method == "P2@dict"].assign(method="mof"),
        cn[cn.method == "P2@dict"].assign(method="inorg"),
    ])
    cats = list(merged.sort_values("件数", ascending=False).用途[:5])
    grid_figure(coords, labels, ["emb", "mof", "inorg"], names, "umap", cats, out / "fig_grid_ipc_umap.png", title="用途（IPC から判定）ごとの出具合（UMAP）")
    for k, c in enumerate(cats[:3]):
        morph_figure(coords, labels, "emb", "inorg", names, "umap", c, out / f"fig_morph_{k + 1}.png")

    # 3. ランドスケープ（新しい実行の中で、文埋め込みと無機材料オントロジーの関係方式を並べる）
    land = {}
    lnames = {"V-concat": names["emb"], "P2@dict": names["inorg"]}
    pairs = [cats[0:2], cats[2:4]]
    for k, g in enumerate(pairs):
        for red in ("umap", "mds"):
            land[f"usage{k + 1}_{red}"] = build_landscape(run_n, sec, onto_n, ["V-concat", "P2@dict"], lnames, red, out / f"fig_landscape{k + 1}_{red}.png", group_by="usage", groups=g, usage_labels=labels)

    # 4. MOF 文書での取りこぼし率
    cov = {"MOF オントロジー（MOF 文書を見て作成）": coverage(sec, onto_b), "無機材料オントロジー（MOF 文書を見ずに作成）": coverage(sec, onto_n)}
    summary = {"labels": "IPC", "labeled_docs": info_n["labeled_docs"], "mof_ontology": info_b, "inorganic_ontology": info_n, "coverage_zero_rate": cov,
               "concepts": {"mof": {t: sum(1 for c in onto_b.concepts.values() if c.top_type == t and c.concept_id not in onto_b.top_types) for t in onto_b.top_types},
                            "inorganic": {t: sum(1 for c in onto_n.concepts.values() if c.top_type == t and c.concept_id not in onto_n.top_types) for t in onto_n.top_types}},
               "landscape": land}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.set_option("display.width", 250)
    print(json.dumps({k: v for k, v in summary.items() if k != "landscape"}, ensure_ascii=False, indent=1))
    print(merged.to_string(index=False))


if __name__ == "__main__":
    main()
