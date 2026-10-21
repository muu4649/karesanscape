"""P3: S = α S_concept + β S_relation + γ S_embedding（同じ尺度に正規化してから加重統合）。"""
from __future__ import annotations

import itertools

import numpy as np
from scipy.stats import rankdata


def normalize(S: np.ndarray, method: str = "rank") -> np.ndarray:
    """対角を除いた値を 0〜1 にそろえる。NaN は NaN のまま。"""
    out = np.full_like(S, np.nan, dtype=np.float32)
    n = S.shape[0]
    iu = np.triu_indices(n, 1)
    v = S[iu]
    ok = ~np.isnan(v)
    if ok.sum() == 0:
        return out
    if method == "rank":
        r = np.full(v.shape, np.nan)
        r[ok] = (rankdata(v[ok]) - 1) / max(ok.sum() - 1, 1)
    else:
        lo, hi = np.nanmin(v), np.nanmax(v)
        r = (v - lo) / (hi - lo + 1e-12)
    out[iu] = r
    out[(iu[1], iu[0])] = r
    np.fill_diagonal(out, 1.0)
    return out


def combine(components: list[np.ndarray], weights: list[float], method: str = "rank") -> np.ndarray:
    """欠損（NaN）の成分を除き、残りの重みで割り直して統合する。"""
    normed = [normalize(c, method) for c in components]
    stack = np.stack(normed)
    w = np.array(weights, dtype=np.float32)[:, None, None] * ~np.isnan(stack)
    num = np.nansum(stack * w, axis=0)
    den = w.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 0, num / den, np.nan).astype(np.float32)
    np.fill_diagonal(out, 1.0)
    return out


def simplex_grid(step: float, k: int = 3) -> list[tuple[float, ...]]:
    """和が 1 になる重みの格子。"""
    n = int(round(1 / step))
    return [tuple(x * step for x in c) for c in itertools.product(range(n + 1), repeat=k) if sum(c) == n]
