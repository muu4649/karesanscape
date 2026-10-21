"""特許（MOF/PCP）での比較実験を通しで実行する。

    python -m ontomap.patents.run --config config/experiment_mof.yaml

runs/<run_name>/ に、既存の実験と同じ構成で成果物を置く:
    data/        documents.csv（見出し付き連結本文）, sections.csv（段落・書誌）, ontology.json, load_info.json
    graphs/dict/ concepts.jsonl, triples.jsonl（段落の構造を手がかりにした辞書抽出）
    similarity/  similarity_<手法>.npy, manifest.json
    maps/        map_coords.csv, movement.csv, viewer.html
    eval/        method_agreement.csv, coverage.csv, map_fidelity.csv, proxy_ipc_ndcg.csv, role_assignment.csv,
                 triple_stats.csv, embedding_truncation.json, summary.json
    labels/      pair_sample.csv, blind/annotation_sheet_*.csv（300 ペアの盲検シート）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .. import evaluate as ev
from .. import pipeline as pl
from ..labels import sample_pairs, write_blind_sheets
from ..schema import save_documents
from ..similarity.graph_sim import RelationConfig, concept_set_similarity, node_set_similarity, relation_similarity, swap_endpoints
from ..similarity.hybrid import combine
from . import metrics as mt
from .data import load_patent_csv, sections_frame, to_documents
from .extract import build_section_layer
from .vectors import Encoder, RuriEncoder, concat_similarity, section_similarity, section_vectors, tfidf_similarity

LAYER = "dict"
MIN_DOCS = 5  # これより少ないと UMAP のスペクトル初期化が失敗する


def _json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=_default), encoding="utf-8")


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def project_root(run: pl.Run) -> Path:
    """設定ファイルの 1 つ上（config/ の親）をプロジェクト直下とみなす。"""
    return Path(run.config["_config_path"]).parent.parent


def step_data(run: pl.Run):
    """CSV を読み、data/ に書く。返り値は (PatentRecord の一覧, 読み込みの記録)。"""
    cfg = run.config["data"]
    src = Path(cfg["documents_csv"])
    src = src if src.is_absolute() else project_root(run) / src
    recs, info = load_patent_csv(src, cfg)
    run.docs = to_documents(recs)
    d = run.root / "data"
    d.mkdir(parents=True, exist_ok=True)
    save_documents(run.docs, d / "documents.csv")
    sections_frame(recs).to_csv(d / "sections.csv", index=False)
    run.onto.export_json(d / "ontology.json")
    _json(d / "load_info.json", info)
    return recs, info


def step_graph(run: pl.Run, recs) -> dict:
    """段落付き辞書抽出の層を graphs/dict/ に保存し、役割付け・三つ組の集計を eval/ に書く。"""
    with pl._timed(run, "extract_dict"):
        layer, stats = build_section_layer(recs, run.onto, run.config.get("extraction"), LAYER)
    layer.save(run.root / "graphs")
    run.layers[LAYER] = layer
    e = run.root / "eval"
    e.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(stats["roles"]).to_csv(e / "role_assignment.csv", index=False)
    pd.DataFrame(stats["triples"]).to_csv(e / "triple_stats.csv", index=False)
    n_tr = [len(v) for v in layer.triples.values()]
    n_cm = [len({m.concept_id for m in v}) for v in layer.concepts.values()]
    out = {
        "n_docs": len(recs),
        "n_no_concepts": len(stats["no_concepts"]),
        "n_no_triples": len(stats["no_triples"]),
        "no_concepts": stats["no_concepts"],
        "no_triples": stats["no_triples"],
        "docs_capped_at_max_triples": stats["docs_capped"],
        "triples_per_doc": {"mean": float(np.mean(n_tr)) if n_tr else 0.0, "max": int(max(n_tr)) if n_tr else 0, "total": int(sum(n_tr))},
        "distinct_concepts_per_doc": {"mean": float(np.mean(n_cm)) if n_cm else 0.0, "max": int(max(n_cm)) if n_cm else 0},
        "rules": stats["config"],
    }
    _json(e / "extraction.json", out)
    return out


def step_vectors(run: pl.Run, recs, encoder: Encoder | None = None) -> dict:
    """V-concat / V-section / V-tfidf。切り詰めの記録を eval/embedding_truncation.json に書く。"""
    v = run.config["vectors"]
    sd = run.root / "similarity"
    sd.mkdir(parents=True, exist_ok=True)
    texts = [d.text for d in run.docs]
    if encoder is None:
        with pl._timed(run, "embedding_load"):
            encoder = RuriEncoder(v["model"], v.get("device", "cpu"), v.get("prefix", ""), v.get("batch_size", 8), v.get("max_seq_length"))
    with pl._timed(run, "V-concat"):
        run.sims["V-concat"], emb, trunc_concat = concat_similarity(encoder, texts)
    np.save(sd / "embeddings_V-concat.npy", emb)
    weights = v.get("section_weights") or {"problem": 1.0, "solution": 1.0, "use": 1.0}
    with pl._timed(run, "V-section"):
        vecs, present, trunc_sec = section_vectors(encoder, recs, list(weights))
        run.sims["V-section"] = section_similarity(vecs, present, weights)
    with pl._timed(run, "V-tfidf"):
        run.sims["V-tfidf"] = tfidf_similarity(texts, tuple(v.get("tfidf_ngram_range", (2, 3))))
    trunc_concat["truncated_doc_ids"] = [run.doc_ids[i] for i in trunc_concat.pop("truncated_index")]
    info = {"model": encoder.name, "revision": encoder.revision(), "concat": trunc_concat, "sections": trunc_sec, "section_presence": {k: int(m.sum()) for k, m in present.items()}}
    _json(run.root / "eval" / "embedding_truncation.json", info)
    return {"encoder": encoder.name, "revision": info["revision"], "dim": int(emb.shape[1]), "weights": weights, "truncation": info}


def step_ontology_sims(run: pl.Run) -> None:
    """P1 / P2 / A1 / A2 / P3（既存の graph_sim・hybrid を使う）。"""
    cfg = run.config
    L = run.layers[LAYER]
    rc = RelationConfig.from_dict(cfg["relation_similarity"])
    ids = run.doc_ids
    with pl._timed(run, f"P1@{LAYER}"):
        run.sims[f"P1@{LAYER}"] = concept_set_similarity(L, ids, run.onto)
    with pl._timed(run, f"P2@{LAYER}"):
        run.sims[f"P2@{LAYER}"], run.triple_index[LAYER] = relation_similarity(L, ids, run.onto, rc)
    with pl._timed(run, f"A1@{LAYER}"):
        run.sims[f"A1@{LAYER}"] = node_set_similarity(L, ids, run.onto, rc.role_mismatch)
    with pl._timed(run, f"A2@{LAYER}"):
        run.sims[f"A2@{LAYER}"], _ = relation_similarity(swap_endpoints(L, cfg["relation_similarity"]["shuffle_seed"]), ids, run.onto, rc)
    h = cfg["hybrid"]
    run.sims[f"P3@{LAYER}"] = combine([run.sims[f"P1@{LAYER}"], run.sims[f"P2@{LAYER}"], run.sims[h.get("embedding_method", "V-concat")]], [h["alpha"], h["beta"], h["gamma"]], h["normalization"])


def save_similarities(run: pl.Run, vec_info: dict) -> dict:
    """similarity/ に行列と manifest.json を書く。"""
    cfg = run.config
    sd = run.root / "similarity"
    kinds = {
        "V-concat": {"kind": "vector", "definition": "見出し付き連結本文（要約＋課題＋解決手段＋用途）の文埋め込みの余弦", "model": vec_info["encoder"], "revision": vec_info["revision"], "dim": vec_info["dim"], "n_truncated": vec_info["truncation"]["concat"]["n_truncated"]},
        "V-section": {"kind": "vector", "definition": "課題・解決手段・用途を別々に埋め込み、同じ段落どうしの余弦を加重平均（空の段落は除いて割り直す）", "weights": vec_info["weights"], "model": vec_info["encoder"]},
        "V-tfidf": {"kind": "vector(reference)", "definition": "連結本文の文字 n-gram TF-IDF の余弦", "ngram_range": cfg["vectors"].get("tfidf_ngram_range", [2, 3])},
        f"P1@{LAYER}": {"kind": "proposed", "layer": LAYER, "definition": "概念集合（idf 重み・階層つき best-match average）"},
        f"P2@{LAYER}": {"kind": "proposed", "layer": LAYER, "definition": "課題・解決手段・用途の三つ組の対応づけ", "relation_config": cfg["relation_similarity"]},
        f"A1@{LAYER}": {"kind": "ablation", "layer": LAYER, "definition": "P2 からエッジを除いた (概念, 役割) のノード集合"},
        f"A2@{LAYER}": {"kind": "ablation", "layer": LAYER, "definition": "三つ組の始点・終点を確率 0.5 で入れ替えた P2"},
        f"P3@{LAYER}": {"kind": "proposed", "layer": LAYER, "definition": "rank 正規化した P1・P2・V-concat の加重和（欠損成分は除いて割り直す）", "hybrid": cfg["hybrid"]},
    }
    manifest = {}
    for m, S in run.sims.items():
        np.save(sd / f"similarity_{m.replace('@', '__')}.npy", S.astype(np.float32))
        manifest[m] = {**kinds.get(m, {"kind": "other"}), "coverage": ev.coverage(S), "seconds": run.timings.get(m)}
    _json(sd / "manifest.json", manifest)
    return manifest


def step_maps(run: pl.Run) -> pd.DataFrame:
    """MDS/UMAP の座標と 2D 忠実度（pipeline.step_maps を使う）。文書数が少ないときは UMAP の近傍数を下げる。"""
    r = run.config["reduce"]
    n = len(run.docs)
    if r["umap"].get("n_neighbors", 15) >= n:
        r["umap"]["n_neighbors"] = max(2, n - 1)
    (run.root / "eval").mkdir(exist_ok=True)
    return pl.step_maps(run, reference=r.get("reference", "V-concat"))


def step_movement(run: pl.Run) -> pd.DataFrame:
    """手法間の移動（pipeline.movement_table）を maps/movement.csv に書く。"""
    pairs = [tuple(p) for p in run.config.get("movement_pairs", [["V-concat", f"P2@{LAYER}"]])]
    return pl.step_movement(run, pairs)


def _pair_counts(n_docs: int, sp: dict) -> tuple[int, int, int]:
    """文書が少なくペアが足りないときは層ごとの件数を減らす（sample_pairs が終わらなくなるのを防ぐ）。"""
    total = n_docs * (n_docs - 1) // 2
    s, c, r = sp["similar"], sp["confusing"], sp["random"]
    if s + c + r <= total:
        return s, c, r
    s = c = total // 3
    return s, c, max(0, total - s - c)


def step_labels(run: pl.Run) -> dict:
    """300 ペアの層化抽出と盲検シート（labels.sample_pairs / write_blind_sheets を流用）。

    「紛らわしい負例」は文字の重なり（V-tfidf）が高いのに他手法の順位が食い違うペア。
    """
    ld = run.root / "labels"
    ld.mkdir(exist_ok=True)
    sp = run.config["evaluation"]["sample_pairs"]
    s, c, r = _pair_counts(len(run.docs), sp)
    cands = {"B1": run.sims["V-tfidf"]}  # sample_pairs は "B1" を文字の重なりとして使う
    cands.update({m: run.sims[m] for m in ("V-concat", "V-section", f"P1@{LAYER}", f"P2@{LAYER}") if m in run.sims})
    sample = sample_pairs(cands, len(run.docs), s, c, r, run.config["seed"])
    sample["doc_id_a"] = [run.doc_ids[min(i, j)] for i, j in zip(sample.i, sample.j)]
    sample["doc_id_b"] = [run.doc_ids[max(i, j)] for i, j in zip(sample.i, sample.j)]
    sample[["doc_id_a", "doc_id_b", "stratum"]].to_csv(ld / "pair_sample.csv", index=False)
    write_blind_sheets(sample, run.doc_ids, [d.text for d in run.docs], ld / "blind", ["judge1", "judge2"], run.config["seed"])
    return {"n_sample": int(len(sample)), "strata": {k: int(v) for k, v in sample.stratum.value_counts().items()}}


def step_evaluate(run: pl.Run, recs) -> dict:
    """手法間の一致・比較できない文書・代理指標を eval/ に書く。"""
    e = run.root / "eval"
    ecfg = run.config["evaluation"]
    seed = run.config["seed"]
    agree = mt.method_agreement(run.sims, ecfg.get("agreement_k", 10), seed)
    agree.to_csv(e / "method_agreement.csv", index=False)
    cov = mt.coverage_table(run.sims, run.doc_ids)
    cov.to_csv(e / "coverage.csv", index=False)
    groups = {"ipc_main_group": [r.meta.get("ipc_main_group", "") for r in recs], "fi_main_group": [r.meta.get("fi_main_group", "") for r in recs]}
    proxy = mt.proxy_ndcg(run.sims, groups, ecfg.get("proxy_k", 10), seed, ecfg.get("proxy_random_repeats", 50))
    proxy.to_csv(e / "proxy_ipc_ndcg.csv", index=False)
    return {"agreement": agree, "coverage": cov, "proxy": proxy}


def summarize(run: pl.Run, info: dict, gstats: dict, vec_info: dict, fid: pd.DataFrame, evals: dict, labels: dict) -> dict:
    """主要な数値を eval/summary.json にまとめる。"""
    k = run.config["evaluation"].get("agreement_k", 10)
    ag = evals["agreement"]
    focus = [("V-concat", f"P2@{LAYER}"), ("V-concat", f"P1@{LAYER}"), ("V-concat", "V-section"), ("V-concat", "V-tfidf"), ("V-section", f"P2@{LAYER}"), (f"P1@{LAYER}", f"P2@{LAYER}"), (f"A2@{LAYER}", f"P2@{LAYER}"), (f"A1@{LAYER}", f"P2@{LAYER}")]
    pick = []
    for a, b in focus:
        row = ag[((ag.method_a == a) & (ag.method_b == b)) | ((ag.method_a == b) & (ag.method_b == a))]
        if len(row):
            r = row.iloc[0]
            pick.append({"pair": f"{a} vs {b}", f"knn{k}_overlap": r[f"knn{k}_overlap"], "spearman": r["spearman"], "n_docs": int(r["n_docs"])})
    fcols = [c for c in ("method", "reducer", "knn@10", "spearman_dist", "stress", "filled_nan_pairs") if c in fid]
    out = {
        "n_docs": info["n_docs"],
        "load": {k2: info[k2] for k2 in ("n_rows", "duplicate_doc_id_dropped", "empty_dropped", "empty_sections", "duplicate_text", "missing_columns")},
        "incomparable": {"no_concepts": gstats["n_no_concepts"], "no_triples": gstats["n_no_triples"], "by_method": evals["coverage"][["method", "n_incomparable_docs", "pair_coverage"]].to_dict("records")},
        "triples_per_doc": gstats["triples_per_doc"],
        "embedding_truncation": {"concat": vec_info["truncation"]["concat"]["n_truncated"], "sections": {s: v["n_truncated"] for s, v in vec_info["truncation"]["sections"].items()}, "max_seq_length": vec_info["truncation"]["concat"]["max_seq_length"]},
        "method_agreement_highdim": pick,
        "map_fidelity": fid[fcols].round(4).to_dict("records"),
        "proxy_ndcg_weak": evals["proxy"].drop(columns=["note"]).round(4).to_dict("records"),
        "proxy_note": "筆頭 IPC/FI のメイングループの一致を「関連」とみなした弱い代理指標。分類が同じでも課題・手段の関係が同じとは限らないため、品質の根拠ではなく参考値。",
        "labels": labels,
    }
    _json(run.root / "eval" / "summary.json", out)
    return out


def run_patents(config_path: str | Path, run_name: str | None = None, base: Path | None = None, encoder: Encoder | None = None, build_html: bool = True) -> dict:
    """通しで実行し、eval/summary.json の中身を返す。encoder を渡すと文埋め込みを差し替える（テスト用）。"""
    run = pl.open_run(config_path, run_name, base)
    steps = []

    def log(msg: str) -> None:
        steps.append(msg)
        print(f"== {msg}", flush=True)

    log("データ読み込み")
    recs, info = step_data(run)
    if len(recs) < MIN_DOCS:
        raise ValueError(f"文書が {len(recs)} 件しかありません（UMAP のスペクトル初期化のため {MIN_DOCS} 件以上必要）")
    log(f"抽出（{len(recs)} 件）")
    gstats = step_graph(run, recs)
    log("ベクトル方式")
    vec_info = step_vectors(run, recs, encoder)
    log("オントロジー関係方式")
    step_ontology_sims(run)
    save_similarities(run, vec_info)
    log("二次元化と忠実度")
    fid = step_maps(run)
    log("手法間の移動")
    step_movement(run)
    log("評価")
    evals = step_evaluate(run, recs)
    log("盲検シート")
    labels = step_labels(run)
    summary = summarize(run, info, gstats, vec_info, fid, evals, labels)
    pl.write_manifest(run)
    if build_html:
        log("ビューア")
        from ..viewer import build_viewer

        build_viewer(run)
    print(f"完了: {run.root}")
    return summary


def main(argv: list[str] | None = None) -> int:
    """コマンドラインの入口。"""
    ap = argparse.ArgumentParser(description="MOF/PCP 特許で、段落ベクトル方式とオントロジー関係方式を比べる")
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", default=None, help="run 名（既定は設定ファイルの run_name）")
    ap.add_argument("--no-viewer", action="store_true")
    a = ap.parse_args(argv)
    summary = run_patents(a.config, a.run, build_html=not a.no_viewer)
    print(json.dumps({k: summary[k] for k in ("n_docs", "incomparable", "embedding_truncation", "method_agreement_highdim")}, ensure_ascii=False, indent=2, default=_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
