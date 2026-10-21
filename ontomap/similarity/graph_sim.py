"""オントロジー型の類似度: P1（概念集合）・P2（関係グラフ）・A1（エッジ除去）・A2（始点終点の入替）。

どの手法も、比較できない文書（概念・三つ組が無い）を含むペアは NaN にする。
強制的に高類似・低類似にしないため（コンセプト説明書 8 章）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from ..ontology import Ontology
from ..schema import GraphLayer, Triple


def concept_idf(doc_concepts: list[set[str]]) -> dict[str, float]:
    """概念の出現文書数にもとづく idf。"""
    n = len(doc_concepts)
    df: dict[str, int] = {}
    for s in doc_concepts:
        for c in s:
            df[c] = df.get(c, 0) + 1
    return {c: math.log((n + 1) / (v + 1)) + 1.0 for c, v in df.items()}


def _bma(sim: np.ndarray, wa: np.ndarray, wb: np.ndarray) -> float:
    """重み付きの best-match average。"""
    return float((wa @ sim.max(axis=1) + wb @ sim.max(axis=0)) / (wa.sum() + wb.sum()))


def _concept_sub(onto: Ontology, a: list[str], b: list[str]) -> np.ndarray:
    """概念 a × 概念 b の類似度。オントロジーに無い概念は表記が同じときだけ 1。"""
    full = onto.concept_sim_matrix()
    ia = np.array([onto.index.get(c, -1) for c in a], dtype=np.int64)
    ib = np.array([onto.index.get(c, -1) for c in b], dtype=np.int64)
    known = (ia[:, None] >= 0) & (ib[None, :] >= 0)
    same = np.array(a, dtype=object)[:, None] == np.array(b, dtype=object)[None, :]
    return np.where(known, full[ia.clip(0)[:, None], ib.clip(0)[None, :]], same.astype(np.float32)).astype(np.float32)


def concept_set_similarity(layer: GraphLayer, doc_ids: list[str], onto: Ontology) -> np.ndarray:
    """P1: 概念 ID の加重集合（idf）を、階層距離つきで best-match average する。エッジは使わない。"""
    sets = [sorted({m.concept_id for m in layer.concepts.get(d, [])} | {c for t in layer.triples.get(d, []) for c in (t.subject_id, t.object_id)}) for d in doc_ids]
    idf = concept_idf([set(s) for s in sets])
    w = [np.array([idf[c] for c in s], dtype=np.float32) for s in sets]
    n = len(doc_ids)
    S = np.full((n, n), np.nan, dtype=np.float32)
    for i in range(n):
        if not sets[i]:
            continue
        S[i, i] = 1.0
        for j in range(i + 1, n):
            if not sets[j]:
                continue
            S[i, j] = S[j, i] = _bma(_concept_sub(onto, sets[i], sets[j]), w[i], w[j])
    return S


def node_set_similarity(layer: GraphLayer, doc_ids: list[str], onto: Ontology, role_mismatch: float) -> np.ndarray:
    """A1: P2 のグラフからエッジを除き、(概念, 役割) のノード集合だけで比べる。"""
    nodes = []
    for d in doc_ids:
        s = set()
        for t in layer.triples.get(d, []):
            s.add((t.subject_id, t.subject_role))
            s.add((t.object_id, t.object_role))
        nodes.append(sorted(s))
    idf = concept_idf([{c for c, _ in s} for s in nodes])
    n = len(doc_ids)
    S = np.full((n, n), np.nan, dtype=np.float32)
    for i in range(n):
        if not nodes[i]:
            continue
        S[i, i] = 1.0
        wa = np.array([idf[c] for c, _ in nodes[i]], dtype=np.float32)
        for j in range(i + 1, n):
            if not nodes[j]:
                continue
            wb = np.array([idf[c] for c, _ in nodes[j]], dtype=np.float32)
            sim = _concept_sub(onto, [c for c, _ in nodes[i]], [c for c, _ in nodes[j]])
            ra = np.array([r for _, r in nodes[i]])[:, None]
            rb = np.array([r for _, r in nodes[j]])[None, :]
            sim = sim * np.where(ra == rb, 1.0, role_mismatch)
            S[i, j] = S[j, i] = _bma(sim, wa, wb)
    return S


@dataclass
class RelationConfig:
    """P2 の設定（実験前に固定する値）。"""

    w_subject: float = 1.0
    w_predicate: float = 1.0
    w_object: float = 1.0
    epsilon: float = 0.01
    min_endpoint_sim: float = 0.3
    role_mismatch: float = 0.3
    negation_mismatch: float = 0.2
    speculation_mismatch: float = 0.8
    missing_penalty: float = 0.7
    aggregation: str = "assignment"
    predicate_weights: dict[str, float] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "RelationConfig":
        w = d.get("weights", {})
        return cls(
            w_subject=w.get("subject", 1.0),
            w_predicate=w.get("predicate", 1.0),
            w_object=w.get("object", 1.0),
            epsilon=d.get("epsilon", 0.01),
            min_endpoint_sim=d.get("min_endpoint_sim", 0.3),
            role_mismatch=d.get("role_mismatch", 0.3),
            negation_mismatch=d.get("negation_mismatch", 0.2),
            speculation_mismatch=d.get("speculation_mismatch", 0.8),
            missing_penalty=d.get("missing_penalty", 0.7),
            aggregation=d.get("aggregation", "assignment"),
            predicate_weights=d.get("predicate_weights"),
        )


class TripleIndex:
    """全文書の三つ組を配列にまとめ、三つ組 × 三つ組の類似度を一括で計算する。"""

    def __init__(self, layer: GraphLayer, doc_ids: list[str], onto: Ontology, cfg: RelationConfig) -> None:
        self.onto = onto
        self.cfg = cfg
        self.triples: list[Triple] = []
        self.offsets: list[tuple[int, int]] = []
        for d in doc_ids:
            a = len(self.triples)
            self.triples.extend(layer.triples.get(d, []))
            self.offsets.append((a, len(self.triples)))
        idf = concept_idf([{c for t in layer.triples.get(d, []) for c in (t.subject_id, t.object_id)} for d in doc_ids])
        pw = cfg.predicate_weights or {}
        self.weights = np.array(
            [t.confidence * (idf.get(t.subject_id, 1.0) + idf.get(t.object_id, 1.0)) / 2 * pw.get(t.predicate, 1.0) for t in self.triples],
            dtype=np.float32,
        )
        self._prepare()

    def _prepare(self) -> None:
        """三つ組ごとの属性を配列にしておく（行ごとの計算で使い回す）。"""
        T, onto = self.triples, self.onto
        unknown = sorted({t.predicate for t in T if t.predicate not in onto.predicate_index})
        if unknown:
            raise ValueError(f"オントロジーに無い述語: {unknown}")
        self._su = np.array([onto.index.get(t.subject_id, -1) for t in T], dtype=np.int64)
        self._ob = np.array([onto.index.get(t.object_id, -1) for t in T], dtype=np.int64)
        self._pr = np.array([onto.predicate_index[t.predicate] for t in T], dtype=np.int64)
        self._su_lab = np.array([t.subject_id for t in T], dtype=object)
        self._ob_lab = np.array([t.object_id for t in T], dtype=object)
        self._rs = np.array([t.subject_role for t in T], dtype=object)
        self._ro = np.array([t.object_role for t in T], dtype=object)
        self._ng = np.array([t.negated for t in T], dtype=bool)
        self._sp = np.array([t.speculative for t in T], dtype=bool)
        self._cache: tuple[int, np.ndarray] | None = None

    def rows(self, a0: int, a1: int) -> np.ndarray:
        """三つ組 a0〜a1 と全三つ組の類似度（形 (a1-a0, N)、float32）。

        s_triple(t, t') = g(sim(u,u'), sim(r,r'), sim(v,v')) × 役割・否定・推量・欠損の係数。
        全組合せの行列を持たず、文書 1 件ぶんの行だけを計算する（件数が増えてもメモリが増えにくい）。
        """
        cfg = self.cfg
        cm = self.onto.concept_sim_matrix()
        pm = self.onto.predicate_sim_matrix()
        sl = slice(a0, a1)

        def endpoint(idx: np.ndarray, lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            a, b = idx[sl][:, None], idx[None, :]
            known = (a >= 0) & (b >= 0)
            v = np.where(known, cm[a.clip(0), b.clip(0)], (lab[sl][:, None] == lab[None, :]).astype(np.float32))
            return v.astype(np.float32), ~known

        s_u, miss_u = endpoint(self._su, self._su_lab)
        s_v, miss_v = endpoint(self._ob, self._ob_lab)
        s_r = pm[self._pr[sl][:, None], self._pr[None, :]]
        eps = np.float32(cfg.epsilon)
        w = cfg.w_subject + cfg.w_predicate + cfg.w_object
        g = np.exp((cfg.w_subject * np.log(np.maximum(s_u, eps)) + cfg.w_predicate * np.log(np.maximum(s_r, eps)) + cfg.w_object * np.log(np.maximum(s_v, eps))) / w).astype(np.float32)
        g[np.maximum(s_u, s_v) < cfg.min_endpoint_sim] = 0.0  # 形だけの一致に点を与えない
        g[s_r <= 0.0] = 0.0  # 極性が逆の述語（抑制 vs 引き起こす）は一致とみなさない
        g *= np.where(self._rs[sl][:, None] == self._rs[None, :], 1.0, cfg.role_mismatch).astype(np.float32)
        g *= np.where(self._ro[sl][:, None] == self._ro[None, :], 1.0, cfg.role_mismatch).astype(np.float32)
        g *= np.where(self._ng[sl][:, None] == self._ng[None, :], 1.0, cfg.negation_mismatch).astype(np.float32)
        g *= np.where(self._sp[sl][:, None] == self._sp[None, :], 1.0, cfg.speculation_mismatch).astype(np.float32)
        g *= np.where(miss_u | miss_v, cfg.missing_penalty, 1.0).astype(np.float32)
        return g

    def doc_rows(self, i: int) -> np.ndarray:
        """文書 i の三つ組の行（直前の 1 文書ぶんを覚えておく）。"""
        if self._cache is None or self._cache[0] != i:
            a0, a1 = self.offsets[i]
            self._cache = (i, self.rows(a0, a1))
        return self._cache[1]

    @property
    def matrix(self) -> np.ndarray:
        """全三つ組 × 全三つ組の行列（小さいデータの確認用。大きいデータでは使わない）。"""
        n = len(self.triples)
        return self.rows(0, n) if n else np.zeros((0, 0), dtype=np.float32)

    def doc_pair(self, i: int, j: int) -> tuple[float, list[tuple[int, int, float]]]:
        """文書 i, j の関係類似度と、対応づけた三つ組（大域インデックス, スコア）。"""
        a0, a1 = self.offsets[i]
        b0, b1 = self.offsets[j]
        if a0 == a1 or b0 == b1:
            return float("nan"), []
        sub = self.doc_rows(i)[:, b0:b1]
        wa = self.weights[a0:a1]
        wb = self.weights[b0:b1]
        denom = float(wa.sum() + wb.sum())
        pairw = wa[:, None] + wb[None, :]
        if self.cfg.aggregation == "best_match":
            score = float((wa @ sub.max(axis=1) + wb @ sub.max(axis=0)) / denom)
            ri = sub.argmax(axis=1)
            matches = [(a0 + k, b0 + int(ri[k]), float(sub[k, ri[k]])) for k in range(a1 - a0)]
            return score, matches
        r, c = linear_sum_assignment(-(sub * pairw))
        score = float((sub[r, c] * pairw[r, c]).sum() / denom)
        matches = [(a0 + int(x), b0 + int(y), float(sub[x, y])) for x, y in zip(r, c)]
        return score, matches


def relation_similarity(layer: GraphLayer, doc_ids: list[str], onto: Ontology, cfg: RelationConfig) -> tuple[np.ndarray, TripleIndex]:
    """P2: 型付き三つ組の意味対応づけ＋関係方向・役割の一致。"""
    idx = TripleIndex(layer, doc_ids, onto, cfg)
    n = len(doc_ids)
    S = np.full((n, n), np.nan, dtype=np.float32)
    for i in range(n):
        if idx.offsets[i][0] == idx.offsets[i][1]:
            continue
        S[i, i] = 1.0
        for j in range(i + 1, n):
            v, _ = idx.doc_pair(i, j)
            S[i, j] = S[j, i] = v
    return S, idx


def swap_endpoints(layer: GraphLayer, seed: int) -> GraphLayer:
    """A2: 三つ組ごとに確率 0.5 で始点・終点（と役割）を入れ替えた層。方向と役割の寄与を測る対照。"""
    rng = random.Random(seed)
    out = {}
    for d, ts in layer.triples.items():
        new = []
        for t in ts:
            if rng.random() < 0.5:
                t = Triple(**{**t.__dict__, "subject_id": t.object_id, "object_id": t.subject_id, "subject_role": t.object_role, "object_role": t.subject_role, "subject_text": t.object_text, "object_text": t.subject_text})
            new.append(t)
        out[d] = new
    return GraphLayer(layer.name + "_swapped", out, layer.concepts)
