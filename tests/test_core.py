"""中核部分の単体テスト（埋め込みモデルは使わない）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ontomap import evaluate as ev
from ontomap.corpus.generate import generate_sample_corpus, load_truth
from ontomap.extract.dictionary import DictionaryExtractor, find_mentions
from ontomap.labels import pair_split, simulated_score
from ontomap.ontology import Ontology
from ontomap.reduce import align_to, knn_overlap, normalized_stress, to_distance
from ontomap.schema import Document, GraphLayer, Triple, load_documents
from ontomap.similarity.graph_sim import RelationConfig, TripleIndex, concept_set_similarity, relation_similarity, swap_endpoints
from ontomap.similarity.hybrid import combine, normalize, simplex_grid

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def onto() -> Ontology:
    return Ontology.load(ROOT / "config" / "ontology_v0_1.yaml")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory, onto):
    d = tmp_path_factory.mktemp("run")
    info = generate_sample_corpus(onto, d, seed=42)
    return d, info


def _t(doc, s, p, o, sr, orl, neg=False):
    return Triple(doc, s, p, o, sr, orl, negated=neg)


# ---------- オントロジー ----------
def test_concept_similarity_rules(onto):
    assert onto.concept_sim("P_temp_rise", "P_temp_rise") == 1.0
    assert onto.concept_sim("P_temp_rise", "P_thermal_drift") == pytest.approx(2 / 3)  # 熱的課題の兄弟
    assert onto.concept_sim("P_temp_rise", "P_corrosion") == 0.0  # 上位型「課題」だけの共有は 0
    assert onto.concept_sim("M_cooling_channel", "P_temp_rise") == 0.0


def test_predicate_similarity(onto):
    assert onto.predicate_sim("suppresses", "suppresses") == 1.0
    assert onto.predicate_sim("suppresses", "prevents") == 0.7
    assert onto.predicate_sim("suppresses", "causes") == 0.0  # 極性が逆
    assert onto.predicate_sim("occurs_in", "leads_to") == 0.2


def test_ontology_json_roundtrip(onto, tmp_path):
    onto.export_json(tmp_path / "o.json")
    o2 = Ontology.load(tmp_path / "o.json")
    assert set(o2.concepts) == set(onto.concepts)
    assert o2.concept_sim("P_temp_rise", "P_thermal_drift") == onto.concept_sim("P_temp_rise", "P_thermal_drift")


# ---------- 見本コーパス ----------
def test_corpus_counts(corpus):
    d, info = corpus
    assert info["n_docs"] == 200 and info["n_real"] == 120
    assert info["n_paraphrase"] == 40 and info["n_inverted"] == 40
    docs = load_documents(d / "data" / "documents.csv")
    assert len({x.text for x in docs}) == 200
    truth = load_truth(d)
    assert all(truth[x.doc_id].source_doc_id for x in docs if x.source_type != "real")


def test_gold_graph_loaded(corpus):
    d, _ = corpus
    docs = load_documents(d / "data" / "documents.csv")
    g = GraphLayer.load(d / "graphs", "gold", [x.doc_id for x in docs])
    assert all(len(g.triples[x.doc_id]) >= 3 for x in docs)


# ---------- 辞書抽出 ----------
def test_longest_match(onto):
    text = "放熱板を設けて放熱を図る。熱拡散シートで熱拡散を行う。"
    found = [(s, c) for _, _, s, c in find_mentions(text, onto)]
    assert ("放熱板", "M_heat_sink") in found
    assert ("放熱", "A_heat_dissipation") in found
    assert ("熱拡散シート", "M_thermal_sheet") in found


def test_role_inversion_is_detected(onto):
    """手段の概念が課題を受ける側に回ったとき、役割を「対象」にする。"""
    doc = Document("X1", "inverted", "", "冷却流路において腐食が生じるという課題がある。腐食は冷却流路を劣化させる。", 0)
    triples, _ = DictionaryExtractor(onto).extract(doc)
    keys = {(t.subject_id, t.predicate, t.object_id, t.object_role) for t in triples}
    assert ("P_corrosion", "occurs_in", "M_cooling_channel", "対象") in keys
    assert ("P_corrosion", "degrades", "M_cooling_channel", "対象") in keys


def test_suppress_relation(onto):
    doc = Document("X2", "real", "", "電池モジュールでは温度上昇が生じる。冷却流路を設け、熱移動の促進を図ることで温度上昇を抑制する。", 0)
    triples, _ = DictionaryExtractor(onto).extract(doc)
    keys = {t.key() for t in triples}
    assert ("M_cooling_channel", "suppresses", "P_temp_rise") in keys
    assert ("P_temp_rise", "occurs_in", "T_module") in keys


# ---------- 関係類似度 ----------
def _layer(triples_by_doc):
    return GraphLayer("t", triples_by_doc, {d: [] for d in triples_by_doc})


def test_schema_only_match_scores_zero(onto):
    """課題→手段の形が同じでも、概念が無関係なら 0。"""
    L = _layer({
        "a": [_t("a", "M_cooling_channel", "suppresses", "P_temp_rise", "手段", "課題")],
        "b": [_t("b", "M_primer", "suppresses", "P_peeling", "手段", "課題")],
    })
    S, _ = relation_similarity(L, ["a", "b"], onto, RelationConfig())
    assert S[0, 1] == 0.0


def test_direction_and_role_matter(onto):
    base = [_t("a", "M_cooling_channel", "suppresses", "P_temp_rise", "手段", "課題")]
    same = [_t("b", "M_coolant_circulation", "suppresses", "P_temp_rise", "手段", "課題")]
    reverse = [_t("c", "M_cooling_channel", "causes", "P_temp_rise", "手段", "課題")]
    role = [_t("d", "P_temp_rise", "degrades", "M_cooling_channel", "課題", "対象")]
    L = _layer({"a": base, "b": same, "c": reverse, "d": role})
    S, _ = relation_similarity(L, list("abcd"), onto, RelationConfig())
    assert S[0, 1] > 0.5  # 兄弟の手段で同じ課題を抑える
    assert S[0, 2] == 0.0  # 抑制 vs 引き起こす
    assert S[0, 3] < S[0, 1]


def test_missing_graph_is_nan(onto):
    L = _layer({"a": [_t("a", "M_fan", "suppresses", "P_temp_rise", "手段", "課題")], "b": []})
    S, _ = relation_similarity(L, ["a", "b"], onto, RelationConfig())
    assert np.isnan(S[0, 1])
    P = concept_set_similarity(L, ["a", "b"], onto)
    assert np.isnan(P[0, 1]) and np.isnan(P[1, 0])  # b は概念が空なので比較不能


def test_swap_endpoints_is_deterministic(onto):
    L = _layer({"a": [_t("a", "M_fan", "suppresses", "P_temp_rise", "手段", "課題")] * 5})
    x = swap_endpoints(L, 7).triples["a"]
    y = swap_endpoints(L, 7).triples["a"]
    assert [t.subject_id for t in x] == [t.subject_id for t in y]


def test_triple_matrix_symmetric(corpus, onto):
    d, _ = corpus
    docs = load_documents(d / "data" / "documents.csv")
    ids = [x.doc_id for x in docs][:30]
    g = GraphLayer.load(d / "graphs", "gold", ids)
    ti = TripleIndex(g, ids, onto, RelationConfig())
    assert np.allclose(ti.matrix, ti.matrix.T)
    assert ti.matrix.min() >= 0 and ti.matrix.max() <= 1.0001


# ---------- ハイブリッド ----------
def test_combine_handles_nan():
    a = np.array([[1, 0.2, np.nan], [0.2, 1, 0.5], [np.nan, 0.5, 1]], dtype=np.float32)
    b = np.array([[1, 0.9, 0.1], [0.9, 1, 0.3], [0.1, 0.3, 1]], dtype=np.float32)
    c = combine([a, b], [0.5, 0.5])
    assert not np.isnan(c[0, 2])  # 欠損成分を除いて重みを割り直す
    assert c[0, 2] == pytest.approx(normalize(b)[0, 2])


def test_simplex_grid():
    g = simplex_grid(0.1)
    assert len(g) == 66 and all(abs(sum(w) - 1) < 1e-9 for w in g)


# ---------- 二次元化・評価 ----------
def test_distance_fill_and_alignment():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(30, 2))
    S = 1 - np.linalg.norm(X[:, None] - X[None], axis=2) / 10
    S[0, 1] = S[1, 0] = np.nan
    D, n_nan = to_distance(S)
    assert n_nan == 1 and not np.isnan(D).any()
    R = np.array([[0, -1], [1, 0]])
    _, Y, disp = align_to(X, X @ R * 3 + 5)
    assert disp < 1e-10
    assert knn_overlap(np.linalg.norm(X[:, None] - X[None], axis=2), X, 5) == pytest.approx(1.0)
    assert normalized_stress(np.linalg.norm(X[:, None] - X[None], axis=2), X * 2) < 1e-6


def test_ranking_metrics_perfect():
    S = np.array([[1, 0.9, 0.1], [0.9, 1, 0.2], [0.1, 0.2, 1]])
    lab = pd.DataFrame({"doc_id_a": ["a", "a", "b"], "doc_id_b": ["b", "c", "c"], "score": [3, 0, 0]})
    r = ev.ranking_metrics(S, {"a": 0, "b": 1, "c": 2}, lab, k=10, relevant=3)
    assert r["ndcg"].mean() == pytest.approx(1.0)
    assert r["recall"].mean() == pytest.approx(1.0)


def test_simulated_labels_and_split(corpus, onto):
    d, _ = corpus
    truth = load_truth(d)
    inv = next(t for t in truth.values() if t.kind.startswith("inverted"))
    para = next(t for t in truth.values() if t.kind == "paraphrase")
    assert simulated_score(truth[inv.source_doc_id], inv, onto) == 1
    assert simulated_score(truth[para.source_doc_id], para, onto) == 3
    assert pair_split("a", "b", {"a": "train", "b": "train"}) == "train"
    assert pair_split("a", "b", {"a": "train", "b": "eval"}) == "eval"


def test_sample_pairs_small_corpus():
    """ペア数が抽出件数より少なくても止まる。"""
    from ontomap.labels import sample_pairs

    S = np.eye(5, dtype=np.float32)
    df = sample_pairs({"B1": S, "B2": S}, 5, 100, 100, 100, seed=1)
    assert len(df) == 10 and not df.duplicated(["i", "j"]).any()


def test_unknown_predicate_raises(onto):
    L = _layer({"a": [_t("a", "M_fan", "no_such", "P_temp_rise", "手段", "課題")], "b": [_t("b", "M_fan", "suppresses", "P_temp_rise", "手段", "課題")]})
    with pytest.raises(ValueError):
        relation_similarity(L, ["a", "b"], onto, RelationConfig())


def test_neighbor_purity_and_robust_lim():
    from ontomap.snapshots import neighbor_purity, robust_lim

    xy = np.vstack([np.random.default_rng(0).normal(0, 0.1, (10, 2)), np.random.default_rng(1).normal(5, 0.1, (10, 2))])
    member = np.array([True] * 10 + [False] * 10)
    assert neighbor_purity(xy, member, k=5) == pytest.approx(1.0)
    cloud = np.random.default_rng(2).normal(0, 1, (200, 2))
    base = robust_lim(cloud)
    assert robust_lim(np.vstack([cloud, [[1e6, 1e6]]])) == pytest.approx(base, rel=0.05)  # 外れ点 1 つでは範囲が広がらない


def test_usage_labels(tmp_path):
    from ontomap.snapshots import usage_labels

    yaml_text = """version: t
top_types: [{id: P, label: 課題}, {id: M, label: 手段}, {id: U, label: 用途}]
predicates: [{id: used_for, label: 用途, group: apply, polarity: 0}]
concepts:
  - {id: U_sep, label: ガス分離, parents: [U]}
  - {id: U_co2, label: 二酸化炭素の分離, aliases: [CO2分離], parents: [U_sep]}
  - {id: U_cat, label: 触媒, aliases: [触媒用途], parents: [U]}
"""
    (tmp_path / "o.yaml").write_text(yaml_text, encoding="utf-8")
    pd.DataFrame({"doc_id": ["a", "b"], "use_text": ["CO2分離に用いる。", "特になし"]}).to_csv(tmp_path / "s.csv", index=False)
    lab = usage_labels(tmp_path / "s.csv", Ontology.load(tmp_path / "o.yaml"))
    assert lab == {"a": {"ガス分離"}, "b": set()}


def test_landscape_helpers():
    from ontomap.landscape import _repel, _smooth, applicants_of, short_applicant, trajectory

    assert short_applicant("国立大学法人京都大学") == "京都大学"
    assert applicants_of("住友化学株式会社; トヨタ自動車株式会社") == ["住友化学", "トヨタ自動車"]
    xy = np.array([[0, 0], [1, 0], [2, 0], [3, 0], [4, 0], [5, 0], [6, 0], [7, 0]], dtype=float)
    years = np.array([2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018], dtype=float)
    pts, labs = trajectory(xy, years, 4)
    assert len(pts) == 4 and labs[0] == "2011–12" and labs[-1] == "2017–18"
    sm = _smooth(np.array([[0, 0], [1, 1], [2, 0], [3, 1]], dtype=float))
    assert sm[:, 1].min() >= 0 and sm[:, 1].max() <= 1  # 角削りは元の折れ線の外へ膨らまない
    pos = _repel(np.array([[0.0, 0.0], [0.01, 0.0]]), np.array([[0.2, 0.1], [0.2, 0.1]]))
    assert abs(pos[0] - pos[1]).max() >= 0.099


def test_structure_eval_helpers(tmp_path):
    from ontomap.patents.structure_eval import jaccard, to_mid

    yaml_text = """version: t
top_types: [{id: P, label: 課題}, {id: M, label: 手段}]
predicates: [{id: addresses, label: 解決, group: reduce, polarity: -1}]
concepts:
  - {id: P_stab, label: 安定性, parents: [P]}
  - {id: P_water, label: 耐水性, parents: [P_stab]}
  - {id: P_heat, label: 耐熱性, parents: [P_stab]}
"""
    (tmp_path / "o.yaml").write_text(yaml_text, encoding="utf-8")
    o = Ontology.load(tmp_path / "o.yaml")
    a, b = to_mid([frozenset({"P_water"}), frozenset({"P_heat"})], o)
    assert a == b == frozenset({"P_stab"})  # 兄弟は同じ中間概念にまとまる
    assert np.isnan(jaccard(frozenset(), frozenset()))


# ---------- 部品（モジュール）を切り替える照合（ontomap/modular.py） ----------
def _modular_fixture():
    from ontomap.modular import compose

    upper = {"version": "t", "top_types": [{"id": "P", "label": "課題"}, {"id": "M", "label": "手段"}, {"id": "U", "label": "用途"}],
             "predicates": [{"id": "addresses", "label": "解決する", "group": "solve", "polarity": 1},
                            {"id": "used_for", "label": "用途", "group": "use", "polarity": 1},
                            {"id": "arises_in", "label": "生じる", "group": "use", "polarity": 1}],
             "concepts": [{"id": "UP_cost", "label": "コスト", "aliases": ["費用"], "parents": ["P"]}]}
    bat = {"concepts": [{"id": "BAT_cell", "label": "セル", "aliases": ["電池セル"], "parents": ["M"]},
                        {"id": "BAT_ev", "label": "電気自動車", "aliases": [], "parents": ["U"]}]}
    bio = {"concepts": [{"id": "BIO_cell", "label": "細胞", "aliases": ["セル"], "parents": ["M"]}]}
    return compose(upper, {"battery": bat, "bio": bio}, routes={"battery": ["H01M"], "bio": ["C12N"]})


def test_modular_routes_and_duplicate_ids():
    import pytest
    from ontomap.modular import compose

    mo = _modular_fixture()
    assert mo.modules_for("H01M10/0525; H01M4/36") == {"battery"}
    assert mo.modules_for("C12N5/00") == {"bio"}
    assert mo.modules_for("G06F") == {"battery", "bio"}  # どれにも当たらなければ全部品
    up = {"version": "t", "top_types": [{"id": "P", "label": "課題"}], "predicates": [], "concepts": []}
    with pytest.raises(ValueError):
        compose(up, {"a": {"concepts": [{"id": "X", "label": "x", "parents": ["P"]}]}, "b": {"concepts": [{"id": "X", "label": "y", "parents": ["P"]}]}})


def test_modular_scoped_extraction_uses_only_active_modules():
    from ontomap.modular import build_section_layer_scoped
    from ontomap.patents.data import PatentRecord

    mo = _modular_fixture()
    cfg = {"sections": {"problem": "P", "solution": "M", "use": "U"}, "role_types": {"P": "課題", "M": "手段", "U": "用途"},
           "relations": {"addresses": {"subject": "手段", "object": "課題"}, "used_for": {"subject": "手段", "object": "用途"},
                         "arises_in": {"subject": "課題", "object": "用途"}}}

    def rec(i, ipc):
        r = PatentRecord(i, {"problem": "コストが高い。", "solution": "セルを積層する。", "use": "電気自動車に用いる。"},
                         {"problem": "課題", "solution": "解決手段", "use": "用途"}, {"ipc": ipc, "fi": ""})
        r.build_text()
        return r

    recs = [rec("D1", "H01M10/0525"), rec("D2", "C12N5/00")]
    layer, stats = build_section_layer_scoped(recs, mo, cfg)
    c1 = {m.concept_id for m in layer.concepts["D1"]}
    c2 = {m.concept_id for m in layer.concepts["D2"]}
    assert "BAT_cell" in c1 and "BIO_cell" not in c1  # 電池の文書の「セル」は電池の概念
    assert "BIO_cell" in c2 and "BAT_cell" not in c2  # 生物の文書の「セル」は細胞
    assert "UP_cost" in c1 and "UP_cost" in c2  # 上層はどちらでも使う
    assert "BAT_ev" not in c2
    assert stats["routes"] == {"battery": 1, "bio": 1}


def test_modular_alias_scope():
    from ontomap.modular import compose

    upper = {"version": "t", "top_types": [{"id": "P", "label": "課題"}], "predicates": [],
             "concepts": [{"id": "UP_cap", "label": "放電容量", "aliases": ["容量"], "parents": ["P"]}]}
    mo = compose(upper, {"battery": {"concepts": []}, "semicon": {"concepts": []}}, routes={"battery": ["H01M"], "semicon": ["H01L"]},
                 alias_scopes=[{"concept": "UP_cap", "alias": "容量", "modules": ["battery"]}])
    assert mo.allowed("容量", "UP_cap", {"battery"})
    assert not mo.allowed("容量", "UP_cap", {"semicon"})  # 半導体の文書では「容量」を放電容量にしない
    assert mo.allowed("放電容量", "UP_cap", {"semicon"})  # 表示名は制限しない
