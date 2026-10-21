"""人手評価の設計（計画書 7 章）: 300 ペアの層化抽出、盲検シート、取り込み、模擬ラベル。"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata
from sklearn.metrics import cohen_kappa_score

from .corpus.generate import Truth
from .ontology import Ontology


def _ranks(S: np.ndarray) -> np.ndarray:
    n = S.shape[0]
    iu = np.triu_indices(n, 1)
    v = np.nan_to_num(S[iu], nan=np.nanmedian(S[iu]))
    return rankdata(-v) / len(v)  # 0 に近いほど類似


def sample_pairs(sims: dict[str, np.ndarray], n_docs: int, n_similar: int, n_confusing: int, n_random: int, seed: int) -> pd.DataFrame:
    """類似 100・紛らわしい負例 100・無作為 100 を層化抽出する。

    類似候補は、どれか 1 手法に偏らないよう全手法の上位から順番に取る。
    紛らわしい負例は、文字の重なり（B1）が高いのに他手法の順位が大きく食い違うペア。
    """
    rng = random.Random(seed)
    iu = np.triu_indices(n_docs, 1)
    pairs = list(zip(iu[0].tolist(), iu[1].tolist()))
    # ペアが足りないときは層ごとの件数を比例で減らす（無限ループを防ぐ）
    total = n_similar + n_confusing + n_random
    if total > len(pairs):
        f = len(pairs) / total
        n_similar, n_confusing = int(n_similar * f), int(n_confusing * f)
        n_random = len(pairs) - n_similar - n_confusing
    ranks = {m: _ranks(S) for m, S in sims.items()}
    chosen: dict[tuple[int, int], str] = {}
    orders = {m: list(np.argsort(r, kind="stable")) for m, r in ranks.items()}
    ptr = {m: 0 for m in orders}
    methods = sorted(orders)
    while sum(v == "similar" for v in chosen.values()) < n_similar and any(ptr[m] < len(pairs) for m in methods):
        for m in methods:
            while ptr[m] < len(pairs) and pairs[orders[m][ptr[m]]] in chosen:
                ptr[m] += 1
            if ptr[m] < len(pairs):
                chosen[pairs[orders[m][ptr[m]]]] = "similar"
            if sum(v == "similar" for v in chosen.values()) >= n_similar:
                break
    lexical = ranks.get("B1", next(iter(ranks.values())))
    others = [r for m, r in ranks.items() if m != "B1"]
    disagreement = np.max(np.abs(np.stack(others) - lexical[None, :]), axis=0) if others else np.zeros_like(lexical)
    score = (1 - lexical) * disagreement
    for k in np.argsort(-score, kind="stable"):
        if sum(v == "confusing" for v in chosen.values()) >= n_confusing:
            break
        if pairs[k] not in chosen:
            chosen[pairs[k]] = "confusing"
    rest = [p for p in pairs if p not in chosen]
    for p in rng.sample(rest, min(n_random, len(rest))):
        chosen[p] = "random"
    rows = [{"i": a, "j": b, "stratum": s} for (a, b), s in chosen.items()]
    rng.shuffle(rows)
    return pd.DataFrame(rows)


def write_blind_sheets(sample: pd.DataFrame, doc_ids: list[str], texts: list[str], out_dir: Path, judges: list[str], seed: int) -> None:
    """注釈者ごとの盲検シート。手法名・順位・層は見せず、左右も無作為に入れ替える。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for jdx, judge in enumerate(judges):
        rng = random.Random(seed + jdx)
        rows = []
        for k, r in enumerate(sample.itertuples()):
            a, b = (r.i, r.j) if rng.random() < 0.5 else (r.j, r.i)
            rows.append({"pair_id": f"Q{k + 1:03d}", "doc_id_a": doc_ids[a], "doc_id_b": doc_ids[b], "text_a": texts[a], "text_b": texts[b], "score(0-3)": "", "rationale": ""})
        rng.shuffle(rows)
        pd.DataFrame(rows).to_csv(out_dir / f"annotation_sheet_{judge}.csv", index=False)
    (out_dir / "SCORING_GUIDE.md").write_text(
        "# 採点基準\n\n"
        "- 0: 無関係\n- 1: 対象・語彙のレベルで共通\n- 2: 課題または手段が近い\n- 3: 課題と手段の関係が実質的に近い\n\n"
        "2 人が独立に採点し、2 点以上ずれたペアは第三者か合議で裁定する。\n",
        encoding="utf-8",
    )


def import_labels(sheet_paths: list[Path], adjudication: Path | None, split_of: dict[str, str]) -> tuple[pd.DataFrame, dict]:
    """採点済みシートを pair_labels.csv 形式にまとめ、注釈者一致（重み付き κ）を返す。"""
    frames = []
    for p in sheet_paths:
        judge = p.stem.replace("annotation_sheet_", "")
        df = pd.read_csv(p, dtype={"score(0-3)": "float"})
        df = df.rename(columns={"score(0-3)": "score"})
        df["judge_id"] = judge
        frames.append(df[["doc_id_a", "doc_id_b", "judge_id", "score", "rationale"]])
    lab = pd.concat(frames)
    lab[["doc_id_a", "doc_id_b"]] = np.sort(lab[["doc_id_a", "doc_id_b"]].to_numpy(), axis=1)
    wide = lab.pivot_table(index=["doc_id_a", "doc_id_b"], columns="judge_id", values="score")
    stats = {}
    if wide.shape[1] >= 2:
        a, b = wide.columns[:2]
        ok = wide[[a, b]].dropna()
        stats["weighted_kappa"] = float(cohen_kappa_score(ok[a].astype(int), ok[b].astype(int), weights="quadratic"))
        stats["n_disagree_ge2"] = int((abs(ok[a] - ok[b]) >= 2).sum())
    if adjudication is not None and adjudication.exists():
        adj = pd.read_csv(adjudication)
        adj["judge_id"] = "adjudicated"
        lab = pd.concat([lab, adj[["doc_id_a", "doc_id_b", "judge_id", "score", "rationale"]]])
    lab["split"] = [pair_split(a, b, split_of) for a, b in zip(lab.doc_id_a, lab.doc_id_b)]
    return lab, stats


def pair_split(a: str, b: str, split_of: dict[str, str]) -> str:
    """両方が train 群なら train、それ以外は eval（重み調整と最終評価のペアを分ける）。"""
    return "train" if split_of.get(a) == "train" and split_of.get(b) == "train" else "eval"


def consensus_scores(labels: pd.DataFrame) -> pd.DataFrame:
    """裁定があれば裁定値、無ければ注釈者の平均。"""
    adj = labels[labels.judge_id == "adjudicated"].set_index(["doc_id_a", "doc_id_b"])["score"]
    mean = labels[labels.judge_id != "adjudicated"].groupby(["doc_id_a", "doc_id_b"])["score"].mean()
    out = mean.copy()
    out.loc[adj.index] = adj
    split = labels.groupby(["doc_id_a", "doc_id_b"])["split"].first()
    return pd.DataFrame({"score": out, "split": split}).reset_index()


def simulated_score(a: Truth, b: Truth, onto: Ontology) -> int:
    """見本コーパス用の模擬専門家スコア（0〜3）。生成時の正解構造から機械的に決める。

    人の判断ではない。パイプラインの動作確認用であり、仮説の検証には使えない。
    """
    if a.mechanism_id == b.mechanism_id and (a.kind.startswith("inverted") != b.kind.startswith("inverted")):
        return 1  # 語彙共通・関係違い
    same_p = a.problem == b.problem
    same_s = a.solution is not None and a.solution == b.solution
    if same_p and same_s:
        return 3
    rel_p = onto.concept_sim(a.problem, b.problem) >= 0.5
    rel_s = a.solution is not None and b.solution is not None and onto.concept_sim(a.solution, b.solution) >= 0.5
    if same_p or same_s or (rel_p and rel_s):
        return 2
    if set(a.slots.values()) & set(b.slots.values()) or a.domain == b.domain:
        return 1
    return 0


def simulated_labels(doc_ids: list[str], truth: dict[str, Truth], onto: Ontology, split_of: dict[str, str]) -> pd.DataFrame:
    """全 19,900 ペアの模擬ラベル（judge_id=simulated）。"""
    rows = []
    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            a, b = doc_ids[i], doc_ids[j]
            rows.append({"doc_id_a": a, "doc_id_b": b, "judge_id": "simulated", "score": simulated_score(truth[a], truth[b], onto), "rationale": "generated-structure", "split": pair_split(a, b, split_of)})
    return pd.DataFrame(rows)
