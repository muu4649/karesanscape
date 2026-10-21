"""正解ラベルの無い特許での評価: 手法間の一致、比較できない文書、分類（IPC/FI）による弱い代理指標。"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
from scipy.stats import spearmanr


def comparable_rows(S: np.ndarray) -> np.ndarray:
    """他の文書と 1 つでも比べられる（対角以外に有限値がある）文書の真偽配列。"""
    F = np.isfinite(S).copy()
    np.fill_diagonal(F, False)
    return F.any(axis=1)


def _order(row: np.ndarray, i: int, tie: np.ndarray) -> np.ndarray:
    """類似度の高い順（自分を除く）。NaN は最下位、同点は固定の乱数順で崩す。"""
    v = np.where(np.isfinite(row), row, -np.inf).astype(np.float64)
    v[i] = -np.inf
    order = np.lexsort((tie, -v))
    return order[order != i]


def knn_agreement(Sa: np.ndarray, Sb: np.ndarray, k: int = 10, seed: int = 0) -> tuple[float, int]:
    """高次元の類似度行列どうしの k 近傍の重なり（|A∩B|/k の平均）。両手法で比べられる文書だけ。"""
    n = Sa.shape[0]
    tie = np.random.default_rng(seed).random(n)
    ok = comparable_rows(Sa) & comparable_rows(Sb)
    kk = min(k, n - 1)
    vals = []
    for i in np.flatnonzero(ok):
        a = set(_order(Sa[i], i, tie)[:kk].tolist())
        b = set(_order(Sb[i], i, tie)[:kk].tolist())
        vals.append(len(a & b) / kk)
    return (float(np.mean(vals)) if vals else float("nan")), len(vals)


def pair_spearman(Sa: np.ndarray, Sb: np.ndarray) -> tuple[float, int]:
    """全ペア類似度の Spearman（両手法で値のあるペアだけ）。"""
    iu = np.triu_indices(Sa.shape[0], 1)
    a, b = Sa[iu], Sb[iu]
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3 or np.ptp(a[ok]) == 0 or np.ptp(b[ok]) == 0:
        return float("nan"), int(ok.sum())
    return float(spearmanr(a[ok], b[ok]).statistic), int(ok.sum())


def method_agreement(sims: dict[str, np.ndarray], k: int = 10, seed: int = 0) -> pd.DataFrame:
    """全手法の組の k 近傍の重なりと Spearman（縦長の表）。"""
    rows = []
    for a, b in itertools.combinations(sorted(sims), 2):
        ov, nd = knn_agreement(sims[a], sims[b], k, seed)
        rho, npair = pair_spearman(sims[a], sims[b])
        rows.append({"method_a": a, "method_b": b, f"knn{k}_overlap": ov, "n_docs": nd, "spearman": rho, "n_pairs": npair})
    return pd.DataFrame(rows)


def coverage_table(sims: dict[str, np.ndarray], doc_ids: list[str]) -> pd.DataFrame:
    """手法ごとの比較できない文書数と、値のあるペアの割合。"""
    rows = []
    iu = np.triu_indices(len(doc_ids), 1)
    for m, S in sorted(sims.items()):
        ok = comparable_rows(S)
        rows.append({"method": m, "n_docs": len(doc_ids), "n_incomparable_docs": int((~ok).sum()), "pair_coverage": float(np.isfinite(S[iu]).mean()) if len(iu[0]) else float("nan"), "incomparable_doc_ids": ";".join(d for d, o in zip(doc_ids, ok) if not o)})
    return pd.DataFrame(rows)


def ndcg_queries(S: np.ndarray, groups: list[str], queries: np.ndarray, k: int, tie: np.ndarray) -> np.ndarray:
    """同じ分類のメイングループを「関連」とした二値の nDCG@k（問い合わせ文書ごと）。"""
    g = np.array(groups, dtype=object)
    disc = 1.0 / np.log2(np.arange(2, k + 2))
    out = []
    for i in queries:
        rel = (g == g[i]) & (g != "")
        rel[i] = False
        nrel = int(rel.sum())
        order = _order(S[i], i, tie)[:k]
        dcg = float((rel[order] * disc[: len(order)]).sum())
        idcg = float(disc[: min(k, nrel)].sum())
        out.append(dcg / idcg if idcg > 0 else np.nan)
    return np.array(out)


def proxy_ndcg(sims: dict[str, np.ndarray], groups: dict[str, list[str]], k: int = 10, seed: int = 0, n_random: int = 50) -> pd.DataFrame:
    """弱い代理指標: 筆頭 IPC/FI のメイングループが同じペアを「関連」とみなした nDCG@k。

    分類が同じでも課題・手段の関係が同じとは限らない。品質の根拠ではなく参考値として扱う。
    問い合わせは「関連文書が 1 件以上あり、全手法で比べられる文書」に共通化する。
    random_expected は類似度を無作為に並べたときの期待値（同じ問い合わせ集合）。
    """
    n = len(next(iter(sims.values())))
    rng = np.random.default_rng(seed)
    tie = rng.random(n)
    ok_all = np.logical_and.reduce([comparable_rows(S) for S in sims.values()])
    rows = []
    for scheme, gl in groups.items():
        g = np.array(gl, dtype=object)
        has_rel = np.array([(g[i] != "") and ((g == g[i]).sum() > 1) for i in range(n)])
        q = np.flatnonzero(has_rel & ok_all)
        rand = []
        for _ in range(n_random):
            R = rng.random((n, n))
            R = (R + R.T) / 2
            rand.append(np.nanmean(ndcg_queries(R, gl, q, k, tie)) if len(q) else np.nan)
        base = float(np.nanmean(rand)) if len(q) else float("nan")
        for m, S in sorted(sims.items()):
            v = ndcg_queries(S, gl, q, k, tie) if len(q) else np.array([])
            rows.append({"scheme": scheme, "method": m, f"ndcg@{k}": float(np.nanmean(v)) if len(v) else float("nan"), "random_expected": base, "n_queries": int(len(q)), "n_docs_with_group": int((g != "").sum()), "n_groups": int(len({x for x in gl if x})), "note": "弱い代理指標（分類が同じ≠課題・手段の関係が同じ）。品質の根拠にしない"})
    return pd.DataFrame(rows)
