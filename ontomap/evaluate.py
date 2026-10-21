"""類似度品質の評価（計画書 7・8・10 章）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .corpus.generate import Truth


def _pair_values(S: np.ndarray, idx: dict[str, int], labels: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    i = labels.doc_id_a.map(idx).to_numpy()
    j = labels.doc_id_b.map(idx).to_numpy()
    return S[i, j], labels.score.to_numpy(dtype=float), i, j


def spearman_on(S: np.ndarray, idx: dict[str, int], labels: pd.DataFrame) -> tuple[float, int]:
    """比較可能（NaN でない）ペアだけで Spearman。返り値: (ρ, 使ったペア数)。"""
    s, y, _, _ = _pair_values(S, idx, labels)
    ok = ~np.isnan(s)
    if ok.sum() < 3:
        return float("nan"), int(ok.sum())
    return float(spearmanr(s[ok], y[ok]).statistic), int(ok.sum())


def ranking_metrics(S: np.ndarray, idx: dict[str, int], labels: pd.DataFrame, k: int = 10, relevant: int = 3, condensed: bool = False) -> dict[str, np.ndarray]:
    """問い合わせ文書ごとの nDCG@k と Recall@k。

    condensed=True のときは採点済みの候補だけで順位を作る（300 ペア評価用。未採点を 0 とみなさない）。
    """
    n = S.shape[0]
    rel = np.full((n, n), np.nan if condensed else 0.0)
    _, y, i, j = _pair_values(S, idx, labels)
    rel[i, j] = y
    rel[j, i] = y
    nd, rc, q = [], [], []
    for a in range(n):
        cand = np.array([b for b in range(n) if b != a and not np.isnan(rel[a, b])])
        if len(cand) < 2:
            continue
        g = rel[a, cand]
        if g.max() <= 0:
            continue
        s = np.nan_to_num(S[a, cand], nan=-np.inf)
        order = cand[np.argsort(-s, kind="stable")]
        gains = 2.0 ** rel[a, order][:k] - 1
        disc = 1 / np.log2(np.arange(2, len(gains) + 2))
        ideal = np.sort(2.0 ** g - 1)[::-1][:k]
        nd.append(float((gains * disc).sum() / (ideal * disc[: len(ideal)]).sum()))
        n_rel = int((g >= relevant).sum())
        if n_rel:
            rc.append(float((rel[a, order][:k] >= relevant).sum() / n_rel))
        q.append(a)
    return {"ndcg": np.array(nd), "recall": np.array(rc), "queries": np.array(q)}


def bootstrap_ci(S: np.ndarray, idx: dict[str, int], labels: pd.DataFrame, n_boot: int, seed: int, S_ref: np.ndarray | None = None) -> dict[str, float]:
    """文書単位のクラスタ・ブートストラップで Spearman の 95% 区間（S_ref があれば差の区間も）。"""
    rng = np.random.default_rng(seed)
    _, y, i, j = _pair_values(S, idx, labels)
    s = S[i, j]
    r = S_ref[i, j] if S_ref is not None else None
    docs = np.unique(np.concatenate([i, j]))
    by_doc: dict[int, list[int]] = {}
    for k, a in enumerate(i):
        by_doc.setdefault(int(a), []).append(k)
    vals, diffs = [], []
    for _ in range(n_boot):
        pick = rng.choice(docs, size=len(docs), replace=True)
        rows = np.concatenate([by_doc.get(int(d), []) for d in pick]).astype(int) if len(pick) else np.array([], int)
        if len(rows) < 10:
            continue
        ok = ~np.isnan(s[rows])
        if r is not None:
            ok &= ~np.isnan(r[rows])
        a = spearmanr(s[rows][ok], y[rows][ok]).statistic
        vals.append(a)
        if r is not None:
            diffs.append(a - spearmanr(r[rows][ok], y[rows][ok]).statistic)
    out = {"ci_low": float(np.nanpercentile(vals, 2.5)), "ci_high": float(np.nanpercentile(vals, 97.5))}
    if diffs:
        out.update({"diff_mean": float(np.nanmean(diffs)), "diff_ci_low": float(np.nanpercentile(diffs, 2.5)), "diff_ci_high": float(np.nanpercentile(diffs, 97.5))})
    return out


def query_bootstrap(a: np.ndarray, b: np.ndarray, n_boot: int, seed: int) -> dict[str, float]:
    """問い合わせ単位の指標（nDCG など）の差の 95% 区間。a, b は同じ問い合わせ順。"""
    rng = np.random.default_rng(seed)
    d = a - b
    if len(d) == 0:
        return {"diff_mean": float("nan"), "diff_ci_low": float("nan"), "diff_ci_high": float("nan")}
    bs = [rng.choice(d, size=len(d), replace=True).mean() for _ in range(n_boot)]
    return {"diff_mean": float(d.mean()), "diff_ci_low": float(np.percentile(bs, 2.5)), "diff_ci_high": float(np.percentile(bs, 97.5))}


def control_metrics(S: np.ndarray, idx: dict[str, int], truth: dict[str, Truth]) -> dict[str, float]:
    """H1（言い換え）と H2（関係反転）の直接指標。

    - para_mrr: 言い換え文書から見た元文書の逆順位の平均（H1、高いほど良い）
    - para_hit10: 元文書が言い換え文書の上位 10 件に入る率（H1）
    - inv_percentile: 元文書から見た反転文書の類似順位の百分位（H2、低いほど良い。1.0 = 最も近い）
    - para_beats_inv: 同じ元文書について sim(元, 言い換え) > sim(元, 反転) となる率（H1/H2 の識別）
    """
    n = S.shape[0]
    mrr, hit, perc, win = [], [], [], []
    by_src: dict[str, dict[str, str]] = {}
    for t in truth.values():
        if t.source_doc_id:
            by_src.setdefault(t.source_doc_id, {})["para" if t.kind == "paraphrase" else "inv"] = t.doc_id
    for src, c in by_src.items():
        s = idx[src]
        row = np.nan_to_num(S[s], nan=-np.inf).copy()
        row[s] = -np.inf
        if "para" in c:
            p = idx[c["para"]]
            prow = np.nan_to_num(S[p], nan=-np.inf).copy()
            prow[p] = -np.inf
            rank = int((prow > prow[s]).sum()) + 1
            mrr.append(1 / rank)
            hit.append(rank <= 10)
        if "inv" in c:
            v = idx[c["inv"]]
            perc.append(float((row < row[v]).sum() / (n - 1)))
        if "para" in c and "inv" in c:
            win.append(float(row[idx[c["para"]]] > row[idx[c["inv"]]]))
    return {
        "para_mrr": float(np.mean(mrr)) if mrr else float("nan"),
        "para_hit10": float(np.mean(hit)) if hit else float("nan"),
        "inv_percentile": float(np.mean(perc)) if perc else float("nan"),
        "para_beats_inv": float(np.mean(win)) if win else float("nan"),
    }


def coverage(S: np.ndarray) -> float:
    """比較可能なペアの割合。"""
    iu = np.triu_indices(S.shape[0], 1)
    return float((~np.isnan(S[iu])).mean())


def graph_quality(gold, pred, doc_ids: list[str]) -> dict[str, float]:
    """Gold graph を基準にした概念 F1 と三つ組 Precision/Recall/F1（ミクロ平均）。"""

    def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        return p, r, (2 * p * r / (p + r) if p + r else 0.0)

    ct = cf = cn = tt = tf = tn = 0
    rt = rf = rn = 0
    for d in doc_ids:
        gc = {m.concept_id for m in gold.concepts.get(d, [])}
        pc = {m.concept_id for m in pred.concepts.get(d, [])}
        ct += len(gc & pc); cf += len(pc - gc); cn += len(gc - pc)
        gt = {t.key() for t in gold.triples.get(d, [])}
        pt = {t.key() for t in pred.triples.get(d, [])}
        tt += len(gt & pt); tf += len(pt - gt); tn += len(gt - pt)
        gr = {(*t.key(), t.role, t.negated) for t in gold.triples.get(d, [])}
        pr = {(*t.key(), t.role, t.negated) for t in pred.triples.get(d, [])}
        rt += len(gr & pr); rf += len(pr - gr); rn += len(gr - pr)
    cp, cr, cf1 = prf(ct, cf, cn)
    tp_, tr_, tf1 = prf(tt, tf, tn)
    _, _, rf1 = prf(rt, rf, rn)
    return {"concept_precision": cp, "concept_recall": cr, "concept_f1": cf1, "triple_precision": tp_, "triple_recall": tr_, "triple_f1": tf1, "triple_f1_with_role_negation": rf1}
