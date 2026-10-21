"""ベクトル方式: V-concat（連結本文の文埋め込み）・V-section（段落ごとの埋め込みの加重平均）・V-tfidf。"""
from __future__ import annotations

from typing import Protocol

import numpy as np

from ..similarity import baselines as bl
from .data import PatentRecord


class Encoder(Protocol):
    """文埋め込みの最小インターフェース（テストでは偽物を差し込む）。"""

    name: str
    max_seq_length: int

    def encode(self, texts: list[str]) -> np.ndarray: ...

    def token_lengths(self, texts: list[str]) -> list[int]: ...

    def revision(self) -> str: ...


class RuriEncoder:
    """sentence-transformers の文埋め込み（既定 cl-nagoya/ruri-v3-30m）。最大長を超える入力は切り詰められる。"""

    def __init__(self, model_name: str, device: str = "cpu", prefix: str = "", batch_size: int = 16, max_seq_length: int | None = None) -> None:
        self._m = bl.EmbeddingModel(model_name, device, prefix)
        self.name = model_name
        self.prefix = prefix
        self.batch_size = batch_size
        if max_seq_length:
            self._m.model.max_seq_length = int(max_seq_length)
        self.max_seq_length = int(self._m.model.max_seq_length)

    def encode(self, texts: list[str]) -> np.ndarray:
        """L2 正規化済みの埋め込み。"""
        return self._m.encode(texts, self.batch_size)

    def token_lengths(self, texts: list[str]) -> list[int]:
        """接頭辞・特殊トークン込みのトークン数（切り詰め前）。"""
        tok = self._m.model.tokenizer
        return [len(tok(self.prefix + t, add_special_tokens=True, truncation=False)["input_ids"]) for t in texts]

    def revision(self) -> str:
        return self._m.revision()


def encode_with_truncation(enc: Encoder, texts: list[str]) -> tuple[np.ndarray, dict]:
    """埋め込みと、最大長を超えて切り詰められた件数の記録。"""
    lens = enc.token_lengths(texts)
    over = [i for i, n in enumerate(lens) if n > enc.max_seq_length]
    info = {
        "max_seq_length": enc.max_seq_length,
        "n_texts": len(texts),
        "n_truncated": len(over),
        "max_tokens": int(max(lens)) if lens else 0,
        "median_tokens": float(np.median(lens)) if lens else 0.0,
        "truncated_index": over,
    }
    return enc.encode(texts), info


def concat_similarity(enc: Encoder, texts: list[str]) -> tuple[np.ndarray, np.ndarray, dict]:
    """V-concat: 連結本文の埋め込みの余弦。返り値は (類似度, 埋め込み, 切り詰めの記録)。"""
    emb, info = encode_with_truncation(enc, texts)
    return bl.cosine_matrix(emb), emb, info


def section_similarity(vectors: dict[str, np.ndarray], present: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    """V-section: 同じ段落どうしの余弦の加重平均。

    S[i, j] = Σ_s w_s·m_is·m_js·cos(e_is, e_js) / Σ_s w_s·m_is·m_js
    m は段落の有無。どちらかの段落が空ならその段落を除いて重みを割り直す。共通の段落が無ければ NaN。
    vectors[s] は L2 正規化済み（空の段落の行は何でもよい）。
    """
    keys = [k for k in weights if k in vectors]
    n = len(next(iter(present.values())))
    num = np.zeros((n, n), dtype=np.float64)
    den = np.zeros((n, n), dtype=np.float64)
    for k in keys:
        m = present[k].astype(np.float64)
        mm = m[:, None] * m[None, :]
        V = vectors[k].astype(np.float64)
        num += weights[k] * mm * (V @ V.T)
        den += weights[k] * mm
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    S = np.clip(S, -1.0, 1.0)
    diag = np.where(np.diag(den) > 0, 1.0, np.nan)
    np.fill_diagonal(S, diag)
    return S.astype(np.float32)


def section_vectors(enc: Encoder, recs: list[PatentRecord], keys: list[str]) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict]:
    """段落ごとに、空でない本文だけを埋め込む。返り値は (埋め込み, 有無, 段落ごとの切り詰め記録)。"""
    vecs, present, info = {}, {}, {}
    for k in keys:
        mask = np.array([bool(r.sections.get(k)) for r in recs])
        texts = [r.sections[k] for r in recs if r.sections.get(k)]
        if texts:
            e, inf = encode_with_truncation(enc, texts)
            full = np.zeros((len(recs), e.shape[1]), dtype=np.float32)
            full[mask] = e
        else:
            full, inf = np.zeros((len(recs), 1), dtype=np.float32), {"n_texts": 0, "n_truncated": 0}
        inf["truncated_doc_index"] = [int(np.flatnonzero(mask)[i]) for i in inf.get("truncated_index", [])]
        inf.pop("truncated_index", None)
        vecs[k], present[k], info[k] = full, mask, inf
    return vecs, present, info


def tfidf_similarity(texts: list[str], ngram_range: tuple[int, int] = (2, 3)) -> np.ndarray:
    """V-tfidf: 連結本文の文字 n-gram TF-IDF の余弦（参考）。"""
    return bl.cosine_matrix(bl.tfidf_features(texts, tuple(ngram_range)))
