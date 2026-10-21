"""実施手順（計画書 11 章）の ①〜⑧ をつなぐ。各工程の成果物は runs/<run_name>/ に保存する。"""
from __future__ import annotations

import hashlib
import json
import platform
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.spatial.distance import pdist, squareform

from . import evaluate as ev
from .corpus.generate import generate_sample_corpus, load_truth
from .extract.dictionary import build_dictionary_layer
from .labels import consensus_scores, sample_pairs, simulated_labels, write_blind_sheets
from .ontology import Ontology
from .reduce import align_to, distance_spearman, knn_overlap, normalized_stress, run_mds, run_umap, to_distance
from .schema import Document, GraphLayer, load_documents
from .similarity import baselines as bl
from .similarity.graph_sim import RelationConfig, TripleIndex, concept_set_similarity, node_set_similarity, relation_similarity, swap_endpoints
from .similarity.hybrid import combine, simplex_grid


@dataclass
class Run:
    """1 回の実験の状態。"""

    config: dict
    root: Path
    onto: Ontology
    docs: list[Document] = field(default_factory=list)
    sims: dict[str, np.ndarray] = field(default_factory=dict)
    layers: dict[str, GraphLayer] = field(default_factory=dict)
    triple_index: dict[str, TripleIndex] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def doc_ids(self) -> list[str]:
        return [d.doc_id for d in self.docs]

    @property
    def idx(self) -> dict[str, int]:
        return {d: i for i, d in enumerate(self.doc_ids)}


def load_config(path: str | Path) -> dict:
    """experiment.yaml を読む。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def open_run(config_path: str | Path, run_name: str | None = None, base: Path | None = None) -> Run:
    """設定を読み、runs/<run_name>/ を用意する。"""
    cfg = load_config(config_path)
    cfg["_config_path"] = str(Path(config_path).resolve())
    cfg_dir = Path(config_path).resolve().parent.parent
    name = run_name or cfg["run_name"]
    root = (base or cfg_dir / "runs") / name
    root.mkdir(parents=True, exist_ok=True)
    onto = Ontology.load(cfg_dir / cfg["ontology"])
    return Run(cfg, root, onto)


def _timed(run: Run, key: str):
    class _T:
        def __enter__(self):
            self.t = time.perf_counter()

        def __exit__(self, *a):
            run.timings[key] = time.perf_counter() - self.t

    return _T()


# ---------- ① データ確定 ----------
def step_data(run: Run) -> dict:
    """見本コーパスの生成、または既存 documents.csv の取り込み。文書群の train/eval を固定する。"""
    c = run.config["corpus"]
    data = run.root / "data"
    if c["generator"] == "sample":
        info = generate_sample_corpus(run.onto, run.root, seed=run.config["seed"], target_chars=tuple(c["target_chars"]))
    else:
        data.mkdir(parents=True, exist_ok=True)
        src = Path(c["documents_csv"])
        docs = load_documents(src)
        pd.DataFrame([d.__dict__ for d in docs]).to_csv(data / "documents.csv", index=False)
        run.onto.export_json(data / "ontology.json")
        info = {"n_docs": len(docs)}
    run.docs = load_documents(data / "documents.csv")
    dup = len(run.docs) - len({d.text for d in run.docs})
    info["duplicates"] = dup
    # 文書群: 見本ではメカニズム単位（制御文書は元と同じ群）。CSV では group_id 列か無作為。
    truth = load_truth(run.root)
    rng = random.Random(run.config["seed"])
    if truth:
        groups = {d: truth[d].mechanism_id for d in run.doc_ids}
    else:
        df = pd.read_csv(data / "documents.csv", dtype=str)
        groups = dict(zip(df.doc_id, df["group_id"])) if "group_id" in df else {d: d for d in run.doc_ids}
    uniq = sorted(set(groups.values()))
    rng.shuffle(uniq)
    n_train = int(round(len(uniq) * c["train_fraction"]))
    train = set(uniq[:n_train])
    pd.DataFrame({"doc_id": list(groups), "group_id": list(groups.values()), "split": ["train" if g in train else "eval" for g in groups.values()]}).to_csv(data / "doc_groups.csv", index=False)
    return info


def split_of(run: Run) -> dict[str, str]:
    df = pd.read_csv(run.root / "data" / "doc_groups.csv", dtype=str)
    return dict(zip(df.doc_id, df.split))


def ensure_docs(run: Run) -> None:
    if not run.docs:
        run.docs = load_documents(run.root / "data" / "documents.csv")


# ---------- ②③ グラフ層 ----------
def step_graphs(run: Run) -> dict:
    """Dictionary graph を作る。Gold graph は見本生成時に作成済み（実データでは人手で用意する）。"""
    ensure_docs(run)
    with _timed(run, "extract_dict"):
        layer = build_dictionary_layer(run.docs, run.onto)
    layer.save(run.root / "graphs")
    out = {}
    for name in run.config["layers"] + (["llm"] if (run.root / "graphs" / "llm" / "triples.jsonl").exists() else []):
        p = run.root / "graphs" / name / "triples.jsonl"
        if p.exists():
            run.layers[name] = GraphLayer.load(run.root / "graphs", name, run.doc_ids)
    if "gold" in run.layers:
        for name, L in run.layers.items():
            if name != "gold":
                out[name] = ev.graph_quality(run.layers["gold"], L, run.doc_ids)
    (run.root / "eval").mkdir(exist_ok=True)
    (run.root / "eval" / "graph_quality.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------- ④⑤ 類似度 ----------
def step_similarity(run: Run, skip: set[str] | None = None) -> list[str]:
    """B1/B2/B3 と、各層の P1/P2/P3/A1/A2 の 200×200 行列を similarity/ に保存する。"""
    ensure_docs(run)
    skip = skip or set()
    texts = [d.text for d in run.docs]
    cfg = run.config
    sd = run.root / "similarity"
    sd.mkdir(exist_ok=True)
    manifest: dict[str, dict] = {}

    with _timed(run, "B1"):
        run.sims["B1"] = bl.cosine_matrix(bl.tfidf_features(texts, cfg["baselines"]["B1"]["ngram_range"]))
    manifest["B1"] = {"kind": "baseline", **cfg["baselines"]["B1"]}
    if "B2" not in skip:
        b2 = cfg["baselines"]["B2"]
        with _timed(run, "B2_load"):
            model = bl.EmbeddingModel(b2["model"], b2.get("device", "cpu"), b2.get("prefix", ""))
        with _timed(run, "B2"):
            emb = model.encode(texts, b2.get("batch_size", 32))
            run.sims["B2"] = bl.cosine_matrix(emb)
        np.save(sd / "embeddings_B2.npy", emb)
        manifest["B2"] = {"kind": "baseline", **b2, "revision": model.revision(), "dim": int(emb.shape[1])}
    if "B3" not in skip:
        b3 = cfg["baselines"]["B3"]
        with _timed(run, "B3"):
            run.sims["B3"] = bl.cosine_matrix(bl.doc2vec_features(texts, b3["vector_size"], b3["window"], b3["min_count"], b3["epochs"], cfg["seed"], b3.get("tokenizer", "janome")))
        manifest["B3"] = {"kind": "baseline(reference)", **b3}

    rc = RelationConfig.from_dict(cfg["relation_similarity"])
    if not run.layers:
        for name in cfg["layers"] + ["llm"]:
            if (run.root / "graphs" / name / "triples.jsonl").exists():
                run.layers[name] = GraphLayer.load(run.root / "graphs", name, run.doc_ids)
    h = cfg["hybrid"]
    for name, L in run.layers.items():
        with _timed(run, f"P1@{name}"):
            run.sims[f"P1@{name}"] = concept_set_similarity(L, run.doc_ids, run.onto)
        with _timed(run, f"P2@{name}"):
            run.sims[f"P2@{name}"], run.triple_index[name] = relation_similarity(L, run.doc_ids, run.onto, rc)
        with _timed(run, f"A1@{name}"):
            run.sims[f"A1@{name}"] = node_set_similarity(L, run.doc_ids, run.onto, rc.role_mismatch)
        with _timed(run, f"A2@{name}"):
            run.sims[f"A2@{name}"], _ = relation_similarity(swap_endpoints(L, cfg["relation_similarity"]["shuffle_seed"]), run.doc_ids, run.onto, rc)
        emb_key = h.get("embedding_method", "B2")
        if emb_key in run.sims:
            run.sims[f"P3@{name}"] = combine([run.sims[f"P1@{name}"], run.sims[f"P2@{name}"], run.sims[emb_key]], [h["alpha"], h["beta"], h["gamma"]], h["normalization"])
        for m in ("P1", "P2", "A1", "A2", "P3"):
            if f"{m}@{name}" in run.sims:
                manifest[f"{m}@{name}"] = {"kind": "proposed" if m.startswith("P") else "ablation", "layer": name}
        manifest[f"P2@{name}"]["relation_config"] = cfg["relation_similarity"]
        if f"P3@{name}" in manifest:
            manifest[f"P3@{name}"]["hybrid"] = h

    for m, S in run.sims.items():
        np.save(sd / f"similarity_{m.replace('@', '__')}.npy", S.astype(np.float32))
        manifest[m]["coverage"] = ev.coverage(S)
        manifest[m]["seconds"] = run.timings.get(m)
    (sd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return list(run.sims)


def load_similarities(run: Run) -> None:
    """保存済みの行列を読む。"""
    for p in sorted((run.root / "similarity").glob("similarity_*.npy")):
        run.sims[p.stem.removeprefix("similarity_").replace("__", "@")] = np.load(p)


# ---------- ⑥ 二次元マップ ----------
def step_maps(run: Run, reference: str = "B2") -> pd.DataFrame:
    """全手法 × MDS/UMAP の座標と忠実度。座標は reference 手法の配置に Procrustes で重ねる。"""
    ensure_docs(run)
    cfg = run.config
    md = run.root / "maps"
    md.mkdir(exist_ok=True)
    (run.root / "eval").mkdir(exist_ok=True)
    seed = cfg["seed"]
    rows, fid = [], []
    raw: dict[tuple[str, str], np.ndarray] = {}
    dists: dict[str, np.ndarray] = {}
    for m, S in run.sims.items():
        D, n_nan = to_distance(S)
        dists[m] = D
        with _timed(run, f"mds:{m}"):
            raw[(m, "mds")] = run_mds(D, seed, **cfg["reduce"]["mds"])
        with _timed(run, f"umap:{m}"):
            raw[(m, "umap")] = run_umap(D, seed, **cfg["reduce"]["umap"])
        for red in ("mds", "umap"):
            Y = raw[(m, red)]
            rec = {"method": m, "reducer": red, "filled_nan_pairs": n_nan, "spearman_dist": distance_spearman(D, Y), "stress": normalized_stress(D, Y)}
            for k in cfg["evaluation"]["k_values"]:
                rec[f"knn@{k}"] = knn_overlap(D, Y, k)
            fid.append(rec)
    ref_m = reference if reference in run.sims else sorted(run.sims)[0]
    for red in ("mds", "umap"):
        ref_std, _, _ = align_to(raw[(ref_m, red)], raw[(ref_m, red)])
        for m in run.sims:
            _, Y, disp = align_to(raw[(ref_m, red)], raw[(m, red)])
            if m == ref_m:
                Y = ref_std
            Y = np.asarray(Y) * np.sqrt(len(run.doc_ids))  # 中心からの RMS 距離 = 1 の単位にそろえる
            for d, (x, y) in zip(run.doc_ids, Y):
                rows.append({"doc_id": d, "method": m, "reducer": red, "x": float(x), "y": float(y), "seed": seed})
            for r in fid:
                if r["method"] == m and r["reducer"] == red:
                    r["procrustes_disparity_to_ref"] = disp
    coords = pd.DataFrame(rows)
    coords.to_csv(md / "map_coords.csv", index=False)
    fdf = pd.DataFrame(fid)
    fdf.to_csv(run.root / "eval" / "map_fidelity.csv", index=False)
    np.savez_compressed(md / "_distances.npz", **{k.replace("@", "__"): v for k, v in dists.items()})
    return fdf


def movement_table(coords: pd.DataFrame, a: str, b: str, reducer: str, k: int = 10) -> pd.DataFrame:
    """手法 a → b で各文書がどれだけ動いたか。

    - shift: 重ねた座標上の移動距離（参照配置の標準化単位）
    - knn_jaccard: 2D での k 近傍の入れ替わり（1 = 同じ近傍、0 = 総入れ替え）
    """
    A = coords[(coords.method == a) & (coords.reducer == reducer)].set_index("doc_id")
    B = coords[(coords.method == b) & (coords.reducer == reducer)].set_index("doc_id").loc[A.index]
    XA, XB = A[["x", "y"]].to_numpy(), B[["x", "y"]].to_numpy()
    shift = np.linalg.norm(XA - XB, axis=1)
    DA, DB = squareform(pdist(XA)), squareform(pdist(XB))
    np.fill_diagonal(DA, np.inf)
    np.fill_diagonal(DB, np.inf)
    jac = []
    for i in range(len(XA)):
        na, nb = set(np.argsort(DA[i])[:k]), set(np.argsort(DB[i])[:k])
        jac.append(len(na & nb) / len(na | nb))
    return pd.DataFrame({"doc_id": A.index, "from": a, "to": b, "reducer": reducer, "shift": shift, "knn_jaccard": jac})


def step_movement(run: Run, pairs: list[tuple[str, str]] | None = None) -> pd.DataFrame:
    """主比較（B2 → P2 など）の文書ごとの移動量を maps/movement.csv に保存する。"""
    coords = pd.read_csv(run.root / "maps" / "map_coords.csv")
    methods = sorted(coords.method.unique())
    if pairs is None:
        pairs = [("B2", m) for m in methods if m != "B2"] if "B2" in methods else []
        pairs += [("B1", m) for m in methods if m.startswith("P2@")]
    out = pd.concat([movement_table(coords, a, b, r) for a, b in pairs for r in ("mds", "umap") if a in methods and b in methods], ignore_index=True) if pairs else pd.DataFrame()
    out.to_csv(run.root / "maps" / "movement.csv", index=False)
    return out


# ---------- ⑦ 人手評価の準備 ----------
def step_labels(run: Run) -> dict:
    """300 ペアの層化抽出と盲検シート。見本コーパスでは模擬ラベル（全ペア）も作る。"""
    ensure_docs(run)
    cfg = run.config
    ld = run.root / "labels"
    ld.mkdir(exist_ok=True)
    sp = cfg["evaluation"]["sample_pairs"]
    candidates = {m: S for m, S in run.sims.items() if m.split("@")[0] in ("B1", "B2", "P1", "P2")}
    sample = sample_pairs(candidates, len(run.docs), sp["similar"], sp["confusing"], sp["random"], cfg["seed"])
    sample["doc_id_a"] = [run.doc_ids[min(i, j)] for i, j in zip(sample.i, sample.j)]
    sample["doc_id_b"] = [run.doc_ids[max(i, j)] for i, j in zip(sample.i, sample.j)]
    sample[["doc_id_a", "doc_id_b", "stratum"]].to_csv(ld / "pair_sample.csv", index=False)
    write_blind_sheets(sample, run.doc_ids, [d.text for d in run.docs], ld / "blind", ["judge1", "judge2"], cfg["seed"])
    info = {"n_sample": len(sample), "strata": sample.stratum.value_counts().to_dict()}
    truth = load_truth(run.root)
    if truth:
        lab = simulated_labels(run.doc_ids, truth, run.onto, split_of(run))
        lab.to_csv(ld / "pair_labels_simulated.csv", index=False)
        info["simulated_score_counts"] = lab.score.value_counts().sort_index().to_dict()
    return info


def active_labels(run: Run) -> tuple[pd.DataFrame, str]:
    """人手ラベル（pair_labels.csv）があればそれを、無ければ模擬ラベルを使う。"""
    ld = run.root / "labels"
    if (ld / "pair_labels.csv").exists():
        return consensus_scores(pd.read_csv(ld / "pair_labels.csv")), "human"
    sim = pd.read_csv(ld / "pair_labels_simulated.csv")
    return sim[["doc_id_a", "doc_id_b", "score", "split"]], "simulated"


# ---------- 重み調整 ----------
def step_tune(run: Run) -> dict:
    """train 群のペアだけで P3 の重みを格子探索する（最終評価は eval 群のペア）。"""
    labels, src = active_labels(run)
    train = labels[labels.split == "train"]
    h = run.config["hybrid"]
    out = {"label_source": src, "n_train_pairs": int(len(train))}
    for name in run.layers or [m.split("@")[1] for m in run.sims if m.startswith("P2@")]:
        comps = [run.sims[f"P1@{name}"], run.sims[f"P2@{name}"], run.sims[h.get("embedding_method", "B2")]]
        best = (-np.inf, None)
        for w in simplex_grid(h["tune_step"]):
            rho, _ = ev.spearman_on(combine(comps, list(w), h["normalization"]), run.idx, train)
            if rho > best[0]:
                best = (rho, w)
        out[name] = {"weights": list(best[1]), "train_spearman": float(best[0])}
        run.sims[f"P3tuned@{name}"] = combine(comps, list(best[1]), h["normalization"])
        np.save(run.root / "similarity" / f"similarity_P3tuned__{name}.npy", run.sims[f"P3tuned@{name}"])
    (run.root / "eval" / "tuned_weights.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------- ⑧ 評価 ----------
def step_evaluate(run: Run) -> pd.DataFrame:
    """手法ごとの品質指標を、eval 群のペアについて、実文書だけ／全文書で別集計する。"""
    ensure_docs(run)
    cfg = run.config
    labels, src = active_labels(run)
    labels = labels[labels.split == "eval"]
    stype = {d.doc_id: d.source_type for d in run.docs}
    real_only = labels[(labels.doc_id_a.map(stype) == "real") & (labels.doc_id_b.map(stype) == "real")]
    truth = load_truth(run.root)
    ref = "B2" if "B2" in run.sims else "B1"
    nb = cfg["evaluation"]["bootstrap"]
    rows = []
    for subset_name, lab in (("all", labels), ("real_only", real_only)):
        ref_rank = ev.ranking_metrics(run.sims[ref], run.idx, lab, 10, cfg["evaluation"]["relevant_score"])
        for m, S in sorted(run.sims.items()):
            rho, n_used = ev.spearman_on(S, run.idx, lab)
            rk = ev.ranking_metrics(S, run.idx, lab, 10, cfg["evaluation"]["relevant_score"])
            rec = {"subset": subset_name, "method": m, "label_source": src, "n_pairs": n_used, "coverage": ev.coverage(S), "spearman": rho, "ndcg@10": float(rk["ndcg"].mean()) if len(rk["ndcg"]) else np.nan, "recall@10": float(rk["recall"].mean()) if len(rk["recall"]) else np.nan}
            ci = ev.bootstrap_ci(S, run.idx, lab, nb, cfg["seed"], S_ref=run.sims[ref] if m != ref else None)
            rec.update({"spearman_ci_low": ci["ci_low"], "spearman_ci_high": ci["ci_high"]})
            if m != ref:
                rec.update({f"spearman_minus_{ref}": ci["diff_mean"], f"spearman_minus_{ref}_ci_low": ci["diff_ci_low"], f"spearman_minus_{ref}_ci_high": ci["diff_ci_high"]})
                if len(rk["ndcg"]) == len(ref_rank["ndcg"]):
                    q = ev.query_bootstrap(rk["ndcg"], ref_rank["ndcg"], nb, cfg["seed"])
                    rec.update({f"ndcg_minus_{ref}": q["diff_mean"], f"ndcg_minus_{ref}_ci_low": q["diff_ci_low"], f"ndcg_minus_{ref}_ci_high": q["diff_ci_high"]})
            if truth and subset_name == "all":
                rec.update(ev.control_metrics(S, run.idx, truth))
            rows.append(rec)
    df = pd.DataFrame(rows)
    df.to_csv(run.root / "eval" / "similarity_quality.csv", index=False)
    # 300 ペアの抽出評価（採点済み候補だけで順位を作る）
    sample = pd.read_csv(run.root / "labels" / "pair_sample.csv")
    full, _ = active_labels(run)
    sl = sample.merge(full, on=["doc_id_a", "doc_id_b"])
    srows = []
    for m, S in sorted(run.sims.items()):
        rho, n_used = ev.spearman_on(S, run.idx, sl)
        rk = ev.ranking_metrics(S, run.idx, sl, 10, cfg["evaluation"]["relevant_score"], condensed=True)
        srows.append({"method": m, "n_pairs": n_used, "spearman": rho, "ndcg@10_condensed": float(rk["ndcg"].mean()) if len(rk["ndcg"]) else np.nan})
    pd.DataFrame(srows).to_csv(run.root / "eval" / "similarity_quality_300pairs.csv", index=False)
    return df


def step_label_preservation(run: Run) -> pd.DataFrame:
    """H3: 専門家（または模擬）スコア 3 のペアが、2D の 10 近傍に残る率。"""
    labels, src = active_labels(run)
    rel = labels[labels.score >= run.config["evaluation"]["relevant_score"]]
    coords = pd.read_csv(run.root / "maps" / "map_coords.csv")
    rows = []
    for (m, red), g in coords.groupby(["method", "reducer"]):
        g = g.set_index("doc_id").loc[run.doc_ids]
        D2 = squareform(pdist(g[["x", "y"]].to_numpy()))
        np.fill_diagonal(D2, np.inf)
        nn = {i: set(np.argsort(D2[i])[:10]) for i in range(len(run.doc_ids))}
        hit = [(run.idx[b] in nn[run.idx[a]]) or (run.idx[a] in nn[run.idx[b]]) for a, b in zip(rel.doc_id_a, rel.doc_id_b)]
        rows.append({"method": m, "reducer": red, "label_source": src, "relevant_pairs": len(hit), "relevant_in_2d_knn10": float(np.mean(hit)) if hit else np.nan})
    df = pd.DataFrame(rows)
    df.to_csv(run.root / "eval" / "map_label_preservation.csv", index=False)
    return df


def write_manifest(run: Run) -> None:
    """環境・設定・乱数種・各工程の時間を残す（再現性）。"""
    import importlib.metadata as md

    libs = {}
    for p in ["numpy", "scipy", "scikit-learn", "pandas", "umap-learn", "sentence-transformers", "transformers", "torch", "gensim", "janome", "anthropic"]:
        try:
            libs[p] = md.version(p)
        except md.PackageNotFoundError:
            libs[p] = None
    cfg_bytes = json.dumps({k: v for k, v in run.config.items() if not k.startswith("_")}, sort_keys=True, ensure_ascii=False).encode()
    info = {
        "run": run.root.name,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config_sha256": hashlib.sha256(cfg_bytes).hexdigest(),
        "config": run.config,
        "ontology_version": run.onto.version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "libraries": libs,
        "timings_seconds": run.timings,
    }
    (run.root / "manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
