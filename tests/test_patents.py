"""ontomap.patents（段落付き特許での比較）のテスト。文埋め込みは偽物（文字 2-gram のハッシュ）を使う。"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from ontomap.ontology import Ontology
from ontomap.patents import metrics as mt
from ontomap.patents.data import first_class, load_patent_csv, to_documents
from ontomap.patents.extract import SectionExtractor, build_section_layer
from ontomap.patents.vectors import section_similarity
from ontomap.similarity.graph_sim import RelationConfig, concept_set_similarity, node_set_similarity, relation_similarity
from ontomap.similarity.hybrid import combine

FIX = Path(__file__).parent / "fixtures"
CSV = FIX / "documents_sections_mini.csv"
ONTO = FIX / "ontology_mof_mini.yaml"
PROJECT = Path(__file__).resolve().parent.parent


class FakeEncoder:
    """文字 2-gram を 64 次元にハッシュする決定的な埋め込み。max_seq_length は切り詰めの記録を試すため小さく取る。"""

    name = "fake-bigram"
    max_seq_length = 40

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for k in range(len(t) - 1):
                out[i, zlib.crc32(t[k : k + 2].encode()) % 64] += 1.0
        out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-12
        return out

    def token_lengths(self, texts: list[str]) -> list[int]:
        return [len(t) + 2 for t in texts]

    def revision(self) -> str:
        return "test"


@pytest.fixture(scope="module")
def onto() -> Ontology:
    return Ontology.load(ONTO)


@pytest.fixture(scope="module")
def recs():
    r, _ = load_patent_csv(CSV)
    return r


@pytest.fixture(scope="module")
def layer(recs, onto):
    L, stats = build_section_layer(recs, onto)
    return L, stats


def _triples(L, doc):
    return {(t.subject_id, t.predicate, t.object_id): t for t in L.triples[doc]}


# ---------- 読み込み ----------
def test_load_sections(recs):
    assert [r.doc_id for r in recs] == ["D1", "D2", "D3", "D4", "D5", "D6"]
    d1 = recs[0]
    assert d1.text.startswith("【要約】")
    assert "【課題】従来の金属有機構造体" in d1.text and "【用途】二酸化炭素の回収" in d1.text
    assert "【0005】" not in d1.text and "【０００５】" not in d1.text  # 段落番号は除く
    assert "選択図" not in d1.text
    # 段落の開始位置が連結本文と合う
    for k, off in d1.offsets.items():
        assert d1.text[off : off + len(d1.sections[k])] == d1.sections[k]
    # NFKC で全角英数をそろえる
    assert recs[1].sections["solution"].startswith("UiO-66") and recs[1].sections["use"].startswith("CO2回収")
    # 空の段落は見出しごと入れない
    assert recs[3].sections["use"] == "" and "【用途】" not in recs[3].text
    docs = to_documents(recs)
    assert docs[0].tech_domain == "B01J" and docs[0].char_count == len(docs[0].text)


def test_first_class():
    assert first_class("B01J20/22;C07F7/00") == ("B01J", "B01J20", "B01J20/22")
    assert first_class("B01J 20/22") == ("B01J", "B01J20", "B01J20/22")
    assert first_class("B01J20/22,A")[1] == "B01J20"
    assert first_class("ＢＯ１Ｊ") == ("", "", "")
    assert first_class("") == ("", "", "")


def test_load_info_counts(tmp_path):
    df = pd.read_csv(CSV, dtype=str, keep_default_na=False)
    empty = df.iloc[[0]].copy()
    empty["doc_id"] = "D7"
    for c in ("abstract", "problem", "solution", "use_text"):
        empty[c] = ""
    p = tmp_path / "x.csv"
    pd.concat([df, df.iloc[[0]], empty]).to_csv(p, index=False)
    r, info = load_patent_csv(p)
    assert len(r) == 6 and info["duplicate_doc_id_dropped"] == 1 and info["empty_dropped"] == ["D7"]
    assert info["empty_sections"]["use"] == 1


# ---------- 役割付け ----------
def test_roles_by_section_and_type(recs, onto):
    ex = SectionExtractor(onto)
    ms = {(m.cid, m.section): m for m in ex.mentions(recs[0])}
    # 課題段落の P 型 → 課題（一致）
    assert ms[("P_water", "problem")].role == "課題" and ms[("P_water", "problem")].type_match
    # 課題段落の M 型（従来の材料）→ 上位型を採って手段、食い違いとして記録
    assert ms[("M_mof", "problem")].role == "手段" and not ms[("M_mof", "problem")].type_match
    # 解決手段段落の P 型（解く課題の言い直し）→ 課題、食い違い
    assert ms[("P_water", "solution")].role == "課題" and not ms[("P_water", "solution")].type_match
    assert ms[("M_zr", "solution")].role == "手段" and ms[("M_zr", "solution")].type_match
    assert ms[("U_co2", "use")].role == "用途"
    # 位置は連結本文での位置
    m = ms[("M_zr", "solution")]
    assert recs[0].text[m.start : m.end] == "ジルコニウム系MOF"


def test_mismatch_policies(recs, onto):
    drop = SectionExtractor(onto, {"mismatch_policy": "drop"})
    assert not any(m.cid == "M_mof" for m in drop.mentions(recs[0]))
    sec = SectionExtractor(onto, {"mismatch_policy": "section"})
    m = next(m for m in sec.mentions(recs[0]) if m.cid == "M_mof")
    assert m.role == "課題"


def test_unknown_predicate_raises(onto):
    with pytest.raises(ValueError):
        SectionExtractor(onto, {"relations": {"no_such_pred": {"subject": "手段", "object": "課題"}}})


# ---------- 三つ組 ----------
def test_triples_rules(layer):
    L, stats = layer
    t1 = _triples(L, "D1")
    # 手段×課題 → addresses。同じ文（解決手段段落の P は食い違い 0.8）が段落またぎ（0.5）より優先
    a = t1[("M_zr", "addresses", "P_water")]
    assert a.subject_role == "手段" and a.object_role == "課題"
    assert a.confidence == pytest.approx(0.9 * 0.8)
    assert not a.negated
    # 手段×用途 → used_for（段落またぎ）、課題×用途 → arises_in（課題段落の P は一致なので 0.5）
    assert t1[("M_zr", "used_for", "U_co2")].confidence == pytest.approx(0.5)
    assert t1[("P_water", "arises_in", "U_co2")].confidence == pytest.approx(0.5)
    # 課題段落の M（食い違い）×課題段落の P は同じ文 0.9×0.8
    assert t1[("M_mof", "addresses", "P_water")].confidence == pytest.approx(0.72)
    # 同じ段落・別の文: D6 の M_zr（2 文目）× P_heat（1 文目、食い違い）= 0.7×0.8
    t6 = _triples(L, "D6")
    assert t6[("M_zr", "addresses", "P_heat")].confidence == pytest.approx(0.56)
    # 否定:「熱安定性を確保できない」
    assert t6[("M_mof", "addresses", "P_heat")].negated
    # 用途が空なら used_for / arises_in は無い
    assert all(t.predicate == "addresses" for t in L.triples["D4"]) and L.triples["D4"]
    # 概念も三つ組も無い文書
    assert stats["no_concepts"] == ["D5"] and stats["no_triples"] == ["D5"]


def test_triple_cap(recs, onto):
    ex = SectionExtractor(onto, {"max_triples_per_doc": 2})
    t, _ = ex.extract(recs[0])
    assert len(t) == 2 and ex.n_capped == 1
    assert t[0].confidence >= t[1].confidence


# ---------- 類似度 ----------
def test_section_similarity_renormalizes():
    v = {"problem": np.array([[1, 0], [1, 0], [0, 1]], float), "use": np.array([[1, 0], [0, 1], [0, 0]], float)}
    present = {"problem": np.array([True, True, False]), "use": np.array([True, True, False])}
    S = section_similarity(v, present, {"problem": 1.0, "use": 3.0})
    assert S[0, 1] == pytest.approx((1 * 1 + 3 * 0) / 4)
    assert np.isnan(S[0, 2]) and np.isnan(S[2, 2]) and S[0, 0] == 1.0
    # use が片方だけ空 → problem だけで割り直す
    present["use"] = np.array([True, False, False])
    S = section_similarity(v, present, {"problem": 1.0, "use": 3.0})
    assert S[0, 1] == pytest.approx(1.0)


def test_similarity_matrices(layer, recs, onto):
    L, _ = layer
    ids = [r.doc_id for r in recs]
    rc = RelationConfig.from_dict(yaml.safe_load((PROJECT / "config" / "experiment_mof.yaml").read_text(encoding="utf-8"))["relation_similarity"])
    P1 = concept_set_similarity(L, ids, onto)
    P2, _ = relation_similarity(L, ids, onto, rc)
    A1 = node_set_similarity(L, ids, onto, rc.role_mismatch)
    emb = FakeEncoder().encode([r.text for r in recs])
    V = emb @ emb.T
    P3 = combine([P1, P2, V], [0.3, 0.5, 0.2])
    d5 = ids.index("D5")
    for S in (P1, P2, A1, P3):
        assert S.shape == (6, 6)
        f = S[np.isfinite(S)]
        assert f.min() >= -1e-6 and f.max() <= 1 + 1e-6
        assert np.allclose(np.nan_to_num(S, nan=-9), np.nan_to_num(S.T, nan=-9))
    for S in (P1, P2, A1):
        assert np.isnan(S[d5]).all()  # 比較できない文書は NaN（対角も）
        assert np.isfinite(np.delete(np.delete(S, d5, 0), d5, 1)).all()
    assert np.isfinite(P3).all()  # P3 は埋め込み成分で埋まる
    # 似た構造の D1–D2 は、構造の違う D1–D3 より P2 が高い
    assert P2[0, 1] > P2[0, 2]


# ---------- 評価 ----------
def test_agreement_and_coverage():
    rng = np.random.default_rng(0)
    X = rng.random((12, 5))
    S = np.corrcoef(X)
    ov, n = mt.knn_agreement(S, S, 5)
    assert ov == pytest.approx(1.0) and n == 12
    rho, _ = mt.pair_spearman(S, S)
    assert rho == pytest.approx(1.0)
    T = S.copy()
    T[3, :] = T[:, 3] = np.nan
    ov, n = mt.knn_agreement(S, T, 5)
    assert n == 11
    cov = mt.coverage_table({"a": S, "b": T}, [f"d{i}" for i in range(12)]).set_index("method")
    assert cov.loc["b", "n_incomparable_docs"] == 1 and cov.loc["b", "incomparable_doc_ids"] == "d3"


def test_proxy_ndcg():
    groups = ["g1", "g1", "g2", "g2", ""]
    S = np.array([[1, .9, .1, .1, .5], [.9, 1, .1, .1, .5], [.1, .1, 1, .8, .5], [.1, .1, .8, 1, .5], [.5, .5, .5, .5, 1]])
    df = mt.proxy_ndcg({"good": S, "bad": 1 - S + np.eye(5)}, {"ipc": groups}, k=2, n_random=5).set_index("method")
    assert df.loc["good", "ndcg@2"] == pytest.approx(1.0)
    assert df.loc["bad", "ndcg@2"] < 0.5
    assert df.loc["good", "n_queries"] == 4  # 分類なしの文書は問い合わせにしない


# ---------- 通し ----------
def test_end_to_end(tmp_path):
    from ontomap.patents.run import run_patents

    cfg = yaml.safe_load((PROJECT / "config" / "experiment_mof.yaml").read_text(encoding="utf-8"))
    cfg.update({"run_name": "mini", "ontology": str(ONTO)})
    cfg["data"]["documents_csv"] = str(CSV)
    cfg["reduce"]["umap"]["n_neighbors"] = 3
    cfg["reduce"]["mds"]["n_init"] = 1
    (tmp_path / "config").mkdir()
    cp = tmp_path / "config" / "exp.yaml"
    cp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    summary = run_patents(cp, encoder=FakeEncoder())
    root = tmp_path / "runs" / "mini"
    for p in ("data/documents.csv", "data/sections.csv", "graphs/dict/triples.jsonl", "graphs/dict/concepts.jsonl", "similarity/manifest.json", "maps/map_coords.csv", "maps/movement.csv", "maps/viewer.html", "eval/map_fidelity.csv", "eval/method_agreement.csv", "eval/proxy_ipc_ndcg.csv", "eval/coverage.csv", "eval/summary.json", "labels/pair_sample.csv", "labels/blind/annotation_sheet_judge1.csv", "manifest.json"):
        assert (root / p).exists(), p
    assert summary["incomparable"]["no_concepts"] == 1 and summary["incomparable"]["no_triples"] == 1
    assert summary["embedding_truncation"]["concat"] == 6  # 偽の最大長 40 を全件が超える
    methods = set(pd.read_csv(root / "maps" / "map_coords.csv").method)
    assert {"V-concat", "V-section", "V-tfidf", "P1@dict", "P2@dict", "A1@dict", "A2@dict", "P3@dict"} <= methods
    mv = pd.read_csv(root / "maps" / "movement.csv")
    assert ((mv["from"] == "V-concat") & (mv["to"] == "P2@dict")).any()
    html = (root / "maps" / "viewer.html").read_text(encoding="utf-8")
    assert '"defaultA":"V-concat"' in html and '"defaultB":"P2@dict"' in html and "解決手段" in html
    assert len(pd.read_csv(root / "labels" / "pair_sample.csv")) == 15  # 6 件 = 15 ペアに収まる
    json.loads((root / "eval" / "summary.json").read_text(encoding="utf-8"))


# ---------- 用途の文の切り出し（ontomap/patents/use_extract.py） ----------
def test_use_extract_picks_use_sentence_not_material_usage():
    from ontomap.patents.use_extract import extract_use

    desc = "\n".join([
        "【技術分野】", "【０００１】", "本発明は、ガス吸着材に関する。",
        "【発明を実施するための形態】", "【００１０】",
        "溶媒としてはＮ，Ｎ－ジメチルホルムアミドを使用することができる。",
        "【００１１】", "本発明の金属有機構造体は、二酸化炭素の分離回収に好適に用いることができ、例えば燃焼排ガスの処理に有用である。",
        "【実施例】", "【００２０】", "実施例１の吸着材を排ガス処理に用いた。",
    ])
    text, src, picked = extract_use(desc, "ガス吸着材")
    assert "燃焼排ガス" in text
    assert "ジメチルホルムアミド" not in text  # 材料の使い方は用途ではない
    assert "実施例１" not in text  # 実施例の段落は使わない
    assert src.startswith("用途の文") and "【0011】" in src


def test_use_extract_generic_statement_and_title_hint():
    from ontomap.patents.use_extract import extract_use

    generic = "【産業上の利用可能性】\n【００５０】\n本発明は、産業上有用である。"
    text, src, _ = extract_use(generic, "金属錯体")
    assert text == "" and src == "なし"  # 中身の語が無い定型文は採らない
    text, src, _ = extract_use(generic, "水素貯蔵用の金属錯体")
    assert text == "水素貯蔵用" and src.startswith("手がかり")
    text, _, _ = extract_use(generic, "金属有機構造体形成用配位子")
    assert text == ""  # 「形成用」は用途ではない


def test_use_extract_bracket_format_and_merge():
    from ontomap.patents.use_extract import merge, split_paras

    head = "[0001] \n　本発明は、除去材に関する。\n\n[0002] \n［背景技術］ 従来から脱臭剤が用いられている。"
    tail = "[0002] \n重なった段落\n[0003] \n続きの段落。"
    paras = split_paras(merge(head, tail))
    assert [p[1] for p in paras] == ["【0001】", "【0002】", "【0003】"]
    assert paras[1][0] == "背景技術" and paras[1][2].startswith("従来から")
    assert "重なった段落" not in "".join(p[2] for p in paras)
