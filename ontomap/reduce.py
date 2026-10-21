"""二次元化（Metric MDS / UMAP）と、配置の忠実度・手法間の移動量。"""
from __future__ import annotations

import warnings

import numpy as np
from scipy.spatial import procrustes
from scipy.spatial.distance import pdist, squareform
from scipy.stats import spearmanr


def to_distance(S: np.ndarray) -> tuple[np.ndarray, int]:
    """D = 1 − S。NaN（比較不能）は手法の中央値で埋め、埋めた数を返す。"""
    S = S.astype(np.float64).copy()
    n = S.shape[0]
    iu = np.triu_indices(n, 1)
    v = S[iu]
    n_nan = int(np.isnan(v).sum())
    if n_nan:
        med = float(np.nanmedian(v)) if (~np.isnan(v)).any() else 0.0
        S[np.isnan(S)] = med
    S = (S + S.T) / 2
    D = np.clip(1.0 - S, 0.0, None)
    np.fill_diagonal(D, 0.0)
    return D, n_nan


def run_mds(D: np.ndarray, seed: int, n_init: int = 4, max_iter: int = 300, init: str = "classical_mds") -> np.ndarray:
    """Metric MDS（事前計算距離）。"""
    from sklearn.manifold import MDS

    kw = dict(n_components=2, metric_mds=True, metric="precomputed", n_init=n_init, max_iter=max_iter, random_state=seed, normalized_stress="auto")
    try:
        m = MDS(init=init, **kw)
    except TypeError:
        m = MDS(**kw)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return m.fit_transform(D)


def run_umap(D: np.ndarray, seed: int, n_neighbors: int = 15, min_dist: float = 0.1) -> np.ndarray:
    """UMAP(metric='precomputed')。random_state を固定する。"""
    import umap

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = umap.UMAP(n_components=2, metric="precomputed", n_neighbors=n_neighbors, min_dist=min_dist, random_state=seed)
        return r.fit_transform(D)


def knn_overlap(D: np.ndarray, Y: np.ndarray, k: int) -> float:
    """元の距離での k 近傍と、2D での k 近傍の重なり率（平均）。"""
    D2 = squareform(pdist(Y))
    n = D.shape[0]
    tot = 0.0
    for i in range(n):
        a = set(np.argsort(np.where(np.arange(n) == i, np.inf, D[i]), kind="stable")[:k])
        b = set(np.argsort(np.where(np.arange(n) == i, np.inf, D2[i]), kind="stable")[:k])
        tot += len(a & b) / k
    return tot / n


def distance_spearman(D: np.ndarray, Y: np.ndarray) -> float:
    """全ペア距離の Spearman 相関。"""
    iu = np.triu_indices(D.shape[0], 1)
    return float(spearmanr(D[iu], pdist(Y)).statistic)


def normalized_stress(D: np.ndarray, Y: np.ndarray) -> float:
    """Kruskal の stress-1（2D 距離を最適に拡大縮小したうえで）。"""
    iu = np.triu_indices(D.shape[0], 1)
    d = D[iu]
    e = pdist(Y)
    b = float((d * e).sum() / ((e * e).sum() + 1e-12))
    return float(np.sqrt(((d - b * e) ** 2).sum() / ((d * d).sum() + 1e-12)))


def align_to(reference: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Procrustes で Y を reference に重ねる（回転・反転・拡大・平行移動）。

    返り値: (標準化した reference, 重ねた Y, 不一致度 disparity)。
    MDS/UMAP の向きは任意なので、手法間で点の移動を比べる前に必ず重ねる。
    """
    ref, aligned, disparity = procrustes(reference, Y)
    return ref, aligned, float(disparity)
