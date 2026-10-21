"""近傍の「構造の似かた」を測る: 隣同士の文書で課題・解決手段がどれだけ共通し、用途がどれだけ違うか。

関係方式の狙い（用途が違っても課題と解決手段の構造が似た文書を近づける）に合わせた物差し。
共通度は 2 通りで測る（どちらかの方式に有利な物差しだけにしないため）:
- 概念: 評価対象の方式が使っていない別のオントロジーで、課題段落・解決手段段落の概念の重なり（Jaccard）
- 文: 課題段落どうし・解決手段段落どうしの文埋め込みの余弦
用途は IPC から判定（usage_eval.IPC_USAGE）。

python -m ontomap.patents.structure_eval --config config/experiment_mof648_inorg.yaml --yardstick-ontology config/ontology_mof_v0_1.yaml
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..extract.dictionary import find_mentions
from ..ontology import Ontology
from .usage_eval import ipc_usage_labels

ROOT = Path(__file__).resolve().parents[2]


def concept_sets(texts: list[str], onto: Ontology, top: str) -> list[frozenset[str]]:
    """段落ごとに、指定の上位型の概念集合。"""
    out = []
    for t in texts:
        t = unicodedata.normalize("NFKC", t or "")
        out.append(frozenset(c for _, _, _, c in find_mentions(t, onto) if onto.top_type(c) == top))
    return out


def to_mid(sets: list[frozenset[str]], onto: Ontology) -> list[frozenset[str]]:
    """概念を、上位型の直下の中間概念に置き換える（同じ種類の課題・手段を同じとみなす）。"""

    def mid(c: str) -> str:
        top, cur = onto.top_type(c), c
        while True:
            ps = onto.concepts[cur].parent_ids
            if not ps or top in ps:
                return cur
            cur = ps[0]

    return [frozenset(mid(c) for c in s) for s in sets]


def jaccard(a: frozenset, b: frozenset) -> float:
    """両方とも空なら NaN（比較できない）。"""
    if not a and not b:
        return float("nan")
    return len(a & b) / len(a | b)


def topk(S: np.ndarray, k: int) -> list[np.ndarray]:
    S = np.array(S, dtype=float)
    np.fill_diagonal(S, -np.inf)
    S[np.isnan(S)] = -np.inf
    return [np.argsort(-S[i], kind="stable")[:k] for i in range(S.shape[0])]


def evaluate(neigh: list[np.ndarray], P: list, M: list, Ep: np.ndarray, Em: np.ndarray, usage: list[set[str]]) -> dict[str, float]:
    jp, jm, cp, cm, cross, cross_struct = [], [], [], [], [], []
    for i, nb in enumerate(neigh):
        for j in nb:
            a, b = jaccard(P[i], P[j]), jaccard(M[i], M[j])
            jp.append(a)
            jm.append(b)
            cp.append(float(Ep[i] @ Ep[j]))
            cm.append(float(Em[i] @ Em[j]))
            if usage[i] and usage[j]:
                diff = not (usage[i] & usage[j])
                cross.append(diff)
                cross_struct.append(diff and bool(P[i] & P[j]) and bool(M[i] & M[j]))
    return {
        "課題の概念の重なり": float(np.nanmean(jp)),
        "手段の概念の重なり": float(np.nanmean(jm)),
        "課題の文の近さ": float(np.mean(cp)),
        "手段の文の近さ": float(np.mean(cm)),
        "用途が違う隣の割合": float(np.mean(cross)),
        "用途が違い課題と手段が共通する隣の割合": float(np.mean(cross_struct)),
        "用途が違う隣のうち課題と手段が共通する割合": float(np.sum(cross_struct) / max(np.sum(cross), 1)),
    }


def main() -> None:
    """エントリポイント。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--yardstick-ontology", required=True, help="評価対象の方式が使っていないオントロジー")
    ap.add_argument("--extra", nargs="*", default=[], help="追加で比べる実行の設定（P2@dict を取り出す）")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--level", choices=["mid", "concept"], default="mid", help="mid = 中間概念の単位（報告書の値）、concept = 概念 ID の単位")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / a.config).read_text(encoding="utf-8"))
    run = ROOT / "runs" / cfg["run_name"]
    sec_path = ROOT / (cfg.get("data") or cfg["corpus"])["documents_csv"]
    doc_ids = pd.read_csv(run / "data" / "documents.csv", dtype=str).doc_id.tolist()
    sec = pd.read_csv(sec_path, dtype=str, keep_default_na=False).set_index("doc_id").loc[doc_ids]
    yard = Ontology.load(ROOT / a.yardstick_ontology)
    P = concept_sets(list(sec.problem), yard, "P")
    M = concept_sets(list(sec.solution), yard, "M")
    if a.level == "mid":
        P, M = to_mid(P, yard), to_mid(M, yard)
    from ..similarity.baselines import EmbeddingModel

    m = EmbeddingModel(cfg["vectors"]["model"] if "vectors" in cfg and "model" in cfg["vectors"] else "cl-nagoya/ruri-v3-30m")
    Ep = m.encode([t or " " for t in sec.problem])
    Em = m.encode([t or " " for t in sec.solution])
    usage_map = ipc_usage_labels(sec_path, doc_ids)
    usage = [usage_map[d] for d in doc_ids]
    mats = {
        "文埋め込み（連結）": np.load(run / "similarity" / "similarity_V-concat.npy"),
        "文埋め込み（段落ごと）": np.load(run / "similarity" / "similarity_V-section.npy"),
        f"関係方式（{cfg['ontology'].split('/')[-1]}）": np.load(run / "similarity" / "similarity_P2__dict.npy"),
    }
    for e in a.extra:
        c2 = yaml.safe_load((ROOT / e).read_text(encoding="utf-8"))
        mats[f"関係方式（{c2['ontology'].split('/')[-1]}）"] = np.load(ROOT / "runs" / c2["run_name"] / "similarity" / "similarity_P2__dict.npy")
    rng = np.random.default_rng(0)
    rows = {name: evaluate(topk(S, a.k), P, M, Ep, Em, usage) for name, S in mats.items()}
    rows["無作為に選んだ隣"] = evaluate([rng.choice([j for j in range(len(doc_ids)) if j != i], a.k, replace=False) for i in range(len(doc_ids))], P, M, Ep, Em, usage)
    df = pd.DataFrame(rows).T.round(3)
    out = run / "report" / f"structure_eval_{a.level}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    info = {"yardstick_ontology": a.yardstick_ontology, "level": a.level, "k": a.k, "docs_with_problem_concepts": int(sum(bool(x) for x in P)), "docs_with_solution_concepts": int(sum(bool(x) for x in M)), "docs_with_ipc_usage": int(sum(bool(u) for u in usage))}
    print(json.dumps(info, ensure_ascii=False))
    pd.set_option("display.width", 250)
    print(df.to_string())


if __name__ == "__main__":
    main()
