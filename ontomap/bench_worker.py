"""計測用の子プロセス。1 手法ぶんを cold 1 回 → warm-up → 本計測 N 回で測り、JSON 行で返す。

別プロセスにするのは、モデルのロードや JIT を「初回（cold）」として切り分け、
手法ごとのピーク RSS を混ぜないため。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def main() -> None:
    """エントリポイント。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--method", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    a = ap.parse_args()
    t_start = time.perf_counter()

    import numpy as np

    from . import pipeline as pl
    from .extract.dictionary import build_dictionary_layer
    from .reduce import run_mds, run_umap, to_distance
    from .similarity import baselines as bl
    from .similarity.graph_sim import RelationConfig, concept_set_similarity, relation_similarity
    from .similarity.hybrid import combine

    try:
        import torch

        torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))
    except ImportError:
        pass
    run = pl.open_run(a.config, a.run)
    pl.ensure_docs(run)
    texts = [d.text for d in run.docs]
    cfg = run.config
    rc = RelationConfig.from_dict(cfg["relation_similarity"])
    h = cfg["hybrid"]
    seed = cfg["seed"]
    emit_rows = []

    def emit(stage: str, kind: str, rep: int, sec: float) -> None:
        emit_rows.append({"method": a.method, "stage": stage, "cold_or_warm": kind, "repeat": rep, "seconds": sec, "peak_ram_mb": _rss_mb(), "tokens": 0})

    emit("import", "cold", 0, time.perf_counter() - t_start)
    model = None
    if a.method in ("B2", "P3"):
        t = time.perf_counter()
        b2 = cfg["baselines"]["B2"]
        model = bl.EmbeddingModel(b2["model"], b2.get("device", "cpu"), b2.get("prefix", ""))
        emit("model_load", "cold", 0, time.perf_counter() - t)

    def features():
        if a.method == "B1":
            return bl.tfidf_features(texts, cfg["baselines"]["B1"]["ngram_range"])
        if a.method == "B2":
            return model.encode(texts)
        if a.method == "B3":
            b3 = cfg["baselines"]["B3"]
            return bl.doc2vec_features(texts, b3["vector_size"], b3["window"], b3["min_count"], b3["epochs"], seed, b3.get("tokenizer", "janome"))
        layer = build_dictionary_layer(run.docs, run.onto)
        if a.method == "P3":
            return (layer, model.encode(texts))
        return layer

    def pairwise(f):
        if a.method in ("B1", "B2", "B3"):
            return bl.cosine_matrix(f)
        if a.method == "P1":
            return concept_set_similarity(f, run.doc_ids, run.onto)
        if a.method == "P2":
            return relation_similarity(f, run.doc_ids, run.onto, rc)[0]
        layer, emb = f
        return combine([concept_set_similarity(layer, run.doc_ids, run.onto), relation_similarity(layer, run.doc_ids, run.onto, rc)[0], bl.cosine_matrix(emb)], [h["alpha"], h["beta"], h["gamma"]], h["normalization"])

    def one(kind: str, rep: int) -> None:
        t0 = time.perf_counter()
        f = features()
        t1 = time.perf_counter()
        S = pairwise(f)
        t2 = time.perf_counter()
        D, _ = to_distance(S)
        run_mds(D, seed, **cfg["reduce"]["mds"])
        t3 = time.perf_counter()
        run_umap(D, seed, **cfg["reduce"]["umap"])
        t4 = time.perf_counter()
        emit("features", kind, rep, t1 - t0)
        emit("pairwise", kind, rep, t2 - t1)
        emit("mds", kind, rep, t3 - t2)
        emit("umap", kind, rep, t4 - t3)
        emit("end_to_end_mds", kind, rep, t3 - t0)

    one("cold", 0)
    for k in range(a.warmup):
        one("warmup", k)
    for k in range(a.repeats):
        one("warm", k + 1)
    for r in emit_rows:
        print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
