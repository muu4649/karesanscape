"""ベースライン B1（文字 n-gram TF-IDF）・B2（文埋め込み）・B3（Doc2Vec）。"""
from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def tfidf_features(texts: list[str], ngram_range: tuple[int, int] = (2, 3)):
    """B1 の特徴量（疎行列）。"""
    vec = TfidfVectorizer(analyzer="char", ngram_range=tuple(ngram_range), sublinear_tf=True)
    return vec.fit_transform(texts)


def cosine_matrix(x) -> np.ndarray:
    """余弦類似度行列（対角 1）。"""
    s = cosine_similarity(x).astype(np.float32)
    np.fill_diagonal(s, 1.0)
    return s


class EmbeddingModel:
    """B2: 事前学習済みの文埋め込み（sentence-transformers）。モデル名と版を固定する。"""

    def __init__(self, model_name: str, device: str = "cpu", prefix: str = "") -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.prefix = prefix
        self.model = SentenceTransformer(model_name, device=device)

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """L2 正規化済みの埋め込み。"""
        return self.model.encode([self.prefix + t for t in texts], batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)

    def revision(self) -> str:
        """モデルの取得版（ローカルキャッシュのスナップショット名）。"""
        try:
            from huggingface_hub import scan_cache_dir

            for repo in scan_cache_dir().repos:
                if repo.repo_id == self.model_name:
                    return sorted(r.commit_hash for r in repo.revisions)[-1]
        except Exception:  # noqa: BLE001 - 版が取れなくても計算は続ける
            pass
        return "unknown"


def _tokenize(texts: list[str], tokenizer: str) -> list[list[str]]:
    if tokenizer == "janome":
        try:
            from janome.tokenizer import Tokenizer

            tk = Tokenizer(wakati=True)
            return [list(tk.tokenize(t)) for t in texts]
        except ImportError:
            pass
    return [[t[i : i + 2] for i in range(len(t) - 1)] for t in texts]


def doc2vec_features(texts: list[str], vector_size: int = 100, window: int = 5, min_count: int = 1, epochs: int = 100, seed: int = 42, tokenizer: str = "janome") -> np.ndarray:
    """B3: 200 件だけで学習する Doc2Vec（データ不足のため参考値）。workers=1 で再現性を保つ。"""
    from gensim.models.doc2vec import Doc2Vec, TaggedDocument

    toks = _tokenize(texts, tokenizer)
    corpus = [TaggedDocument(w, [i]) for i, w in enumerate(toks)]
    model = Doc2Vec(corpus, vector_size=vector_size, window=window, min_count=min_count, epochs=epochs, seed=seed, workers=1, dm=1)
    v = np.stack([model.dv[i] for i in range(len(texts))]).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
    return v
