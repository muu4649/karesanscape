"""2 層のオントロジー（共通層＋マップごとの拡張層）のテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ontomap.app import engine, layers

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "config" / "ontology_inorganic_v1.yaml"
PID = "abcdefabcdef"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """計算は走らせず、取り込み済みのマップを 1 つ置く。"""
    monkeypatch.setenv("ONTOMAP_HOME", str(tmp_path))
    d = engine.pdir(PID)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": PID, "name": "試験", "ontology": "inorganic-1.0", "use_extension": True}), encoding="utf-8")
    rows = []
    for i in range(12):
        rows.append({"doc_id": f"D{i + 1:04d}", "title": f"文書{i}", "problem": "吸着容量が小さいという問題がある。",
                     "solution": "有機配位子とジルコニウムクラスターからなる骨格体を用いる。", "use_text": "ガス吸着に用いる。"})
    pd.DataFrame(rows).to_csv(d / "source.csv", index=False)
    monkeypatch.setattr(engine, "run_project", lambda pid: None)
    return d


def _mid(onto, top):
    mids = {p for c in onto.concepts.values() for p in c.parent_ids}
    return next(c.concept_id for c in onto.concepts.values() if c.concept_id in mids and c.top_type == top and c.concept_id not in onto.top_types)


def test_merged_adds_concept_and_alias():
    base = layers.base_dict(BASE)
    onto0 = layers.merged(base, None)
    parent = _mid(onto0, "M")
    leaf = next(c for c in onto0.concepts.values() if c.top_type == "M" and c.parent_ids and c.concept_id not in {p for x in onto0.concepts.values() for p in x.parent_ids})
    cid = layers.concept_id("骨格体", "M")
    ext = {"version": 1, "concepts": [{"id": cid, "label": "骨格体", "aliases": ["フレームワーク骨格"], "parent": parent, "top": "M"},
                                      {"id": "XM_missing", "label": "迷子", "aliases": [], "parent": "存在しない親", "top": "M"}],
           "aliases": [{"concept_id": leaf.concept_id, "label": leaf.preferred_label, "alias": "てすと別名"}], "ignored": []}
    onto = layers.merged(base, ext)
    assert onto.top_type(cid) == "M"
    assert onto.concepts[cid].parent_ids == [parent]
    assert "XM_missing" not in onto.concepts
    assert "てすと別名" in onto.concepts[leaf.concept_id].aliases
    hits = {c for _, _, _, c in layers.Matcher(onto).find("骨格体とフレームワーク骨格とてすと別名")}
    assert {cid, leaf.concept_id} <= hits
    # 共通層は書き換わらない
    assert cid not in layers.merged(base, None).concepts
    assert len(onto.concepts) == len(onto0.concepts) + 1


def test_matcher_equals_find_mentions():
    from ontomap.extract.dictionary import find_mentions

    onto = layers.merged(layers.base_dict(BASE), None)
    text = "金属有機構造体を含む吸着材を用い、二酸化炭素の分離性能を高める。触媒としても用いる。"
    fast = layers.Matcher(onto).find(text)
    slow = find_mentions(text, onto)
    assert fast == slow


def test_good_filters_noise():
    for bad in ["用途(", "金属(銅", "50wt%", "本発明", "特許文献1", "R1", "the", "炭素数1", "ある"]:
        assert not layers._good(bad), bad
    for ok in ["有機配位子", "テレフタル酸", "多価金属イオン", "Zn"]:
        assert layers._good(ok), ok


def test_adopt_validation_and_unadopt(project):
    onto = engine.project_ontology(PID, False)
    pm, pp = _mid(onto, "M"), _mid(onto, "P")
    engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": pm})
    with pytest.raises(ValueError):
        engine.adopt(PID, {"term": "別の語", "mode": "concept", "top": "M", "parent": pp})  # 上位型が合わない
    with pytest.raises(ValueError):
        engine.adopt(PID, {"term": "", "mode": "ignore"})
    engine.adopt(PID, {"term": "ジルコニウムクラスター", "mode": "alias", "concept_id": layers.concept_id("骨格体", "M")})  # 拡張層の概念にも別名を足せる
    engine.adopt(PID, {"term": "問題", "mode": "ignore"})
    ext = layers.load_extension(project)
    assert [c["label"] for c in ext["concepts"]] == ["骨格体"]
    assert ext["aliases"][0]["alias"] == "ジルコニウムクラスター"
    assert ext["ignored"] == ["問題"]
    engine.unadopt(PID, "concept", layers.concept_id("骨格体", "M"))
    ext = layers.load_extension(project)
    assert ext["concepts"] == [] and ext["aliases"] == []  # 概念を消すと、その概念への別名も消える
    engine.unadopt(PID, "ignore", "問題")
    assert layers.load_extension(project)["ignored"] == []


def test_coverage_improves_with_extension(project):
    onto = engine.project_ontology(PID, False)
    sec = pd.read_csv(project / "source.csv", dtype=str, keep_default_na=False)
    before = layers.coverage(sec, onto)["solution"]["rate"]
    engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": _mid(onto, "M")})
    after = layers.coverage(sec, engine.project_ontology(PID, True))["solution"]["rate"]
    assert after <= before
    if before > 0:
        assert after < before


def test_rebuild_bumps_version_only_on_change(project):
    onto = engine.project_ontology(PID, False)
    engine.rebuild(PID, True)
    assert layers.load_extension(project)["version"] == 0  # 中身が空のままなら版は上げない
    engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": _mid(onto, "M")})
    engine.rebuild(PID, True)
    assert layers.load_extension(project)["version"] == 1
    assert (project / "extension_v1.json").exists()
    engine.rebuild(PID, True)
    assert layers.load_extension(project)["version"] == 1  # 変わっていなければ同じ版
    engine.rebuild(PID, False)
    assert json.loads((project / "meta.json").read_text(encoding="utf-8"))["use_extension"] is False
    engine.adopt(PID, {"term": "問題", "mode": "ignore"})  # 無視だけの変更は版を上げない
    engine.rebuild(PID, True)
    assert layers.load_extension(project)["version"] == 1


def test_project_ontology_built_uses_snapshot(project):
    onto = engine.project_ontology(PID, False)
    cid = layers.concept_id("骨格体", "M")
    engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": _mid(onto, "M")})
    # 地図を描いた時点の控えが無ければ共通層だけ、下書きには入っている
    assert cid not in engine.project_ontology(PID, True, built=True).concepts
    assert cid in engine.project_ontology(PID, True).concepts
    (project / "extension_built.json").write_text(json.dumps(layers.load_extension(project), ensure_ascii=False), encoding="utf-8")
    assert cid in engine.project_ontology(PID, True, built=True).concepts


def test_candidates_skip_known_and_ignored(project):
    sec = pd.read_csv(project / "source.csv", dtype=str, keep_default_na=False)
    onto = engine.project_ontology(PID, False)
    terms = {c["term"] for c in layers.candidates(sec, onto, set())}
    known = {s for s, _ in onto.surface_forms()}
    assert not terms & known
    if terms:
        t = sorted(terms)[0]
        assert t not in {c["term"] for c in layers.candidates(sec, onto, {t})}


def test_parent_must_be_mid_or_top(project):
    onto = engine.project_ontology(PID, False)
    mids = {p for c in onto.concepts.values() for p in c.parent_ids}
    leaf = next(c.concept_id for c in onto.concepts.values() if c.top_type == "M" and c.concept_id not in mids and c.concept_id not in onto.top_types)
    with pytest.raises(ValueError):
        engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": leaf})
    engine.adopt(PID, {"term": "骨格体", "mode": "concept", "top": "M", "parent": "M"})  # 上位型の直下は置ける


def test_shadowed_reports_extension_term_covering_base_term():
    base_d = layers.base_dict(BASE)
    base = layers.merged(base_d, None)
    hits = [(s, c) for _, _, s, c in layers.Matcher(base).find("吸着量") if base.top_type(c) == "P"]
    assert hits, "共通層で「吸着量」が課題の概念として拾える前提"
    use = next(c.concept_id for c in base.concepts.values() if c.top_type == "U" and c.concept_id not in base.top_types and c.concept_id not in {p for x in base.concepts.values() for p in x.parent_ids})
    draft = layers.merged(base_d, {"version": 1, "concepts": [], "aliases": [{"concept_id": use, "label": base.label(use), "alias": "ガス吸着"}], "ignored": []})
    sec = pd.DataFrame([{"doc_id": "D0001", "problem": "ガス吸着量が小さい。", "solution": "", "use_text": ""},
                        {"doc_id": "D0002", "problem": "吸着量が小さい。", "solution": "", "use_text": ""}])
    r = layers.shadowed(sec, base, draft)
    assert r["n_docs"] == 1
    assert r["terms"][0]["term"] == "ガス吸着" and r["terms"][0]["docs"] == ["D0001"]
    assert layers.shadowed(sec, base, base)["n_docs"] == 0


def test_import_extracts_use_from_description(tmp_path, monkeypatch):
    """使い分け: 用途の段落（地図の計算）と用途の文（説明の確認）を両方作る。全文は保存しない。"""
    monkeypatch.setenv("ONTOMAP_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "run_project", lambda pid: None)
    desc = ("【発明を実施するための形態】\n【００１０】\n溶媒としてはエタノールを使用することができる。触媒の用途に応じて溶媒を選ぶ。\n"
            "【００１１】\n本発明の吸着材は、燃焼排ガスからの二酸化炭素の回収に好適に用いることができる。")
    rows = [{"発明の名称": f"吸着材{i}", "要約": "金属有機構造体からなる吸着材。", "用途": "（元の用途）" if i == 0 else "",
             "明細書": desc if i % 2 == 0 else "原料を混合し、加熱して乾燥させる。"} for i in range(12)]
    uid = engine.save_upload("t.csv", pd.DataFrame(rows).to_csv(index=False).encode("utf-8"))
    pv = engine.preview(uid)
    assert pv["mapping"].get("description") == "明細書"
    pid = engine.create_project("試験", uid, pv["mapping"], "inorganic-1.0")
    src = pd.read_csv(engine.pdir(pid) / "source.csv", dtype=str, keep_default_na=False)
    assert "description" not in src.columns  # 全文は保存しない
    assert src.loc[0, "use_text"] == "（元の用途）"  # 用途の列があればそれを地図に使う
    assert src.loc[2, "use_source"].startswith("用途の段落")  # 無ければ全文から段落を選ぶ
    assert "溶媒" in src.loc[2, "use_text"]  # 段落単位なので用途でない文も入る
    for k in (0, 2):
        assert "二酸化炭素の回収" in src.loc[k, "use_sentences"] and "溶媒" not in src.loc[k, "use_sentences"]
    assert src.loc[1, "use_text"] == "" and src.loc[1, "use_sentences"] == ""


def test_use_status_accepts_same_or_hierarchical_concept():
    onto = layers.merged(layers.base_dict(BASE), None)
    mids = {p for c in onto.concepts.values() for p in c.parent_ids}
    child = next(c for c in onto.concepts.values() if c.top_type == "U" and c.concept_id not in mids and c.parent_ids and c.parent_ids[0] not in onto.top_types)
    parent = child.parent_ids[0]
    other = next(c.concept_id for c in onto.concepts.values() if c.top_type == "U" and c.concept_id not in onto.top_types
                 and c.concept_id not in onto._ancestors[child.concept_id] and child.concept_id not in onto._ancestors[c.concept_id])
    assert engine.use_status(child.concept_id, {child.concept_id}, onto) == "ok"
    assert engine.use_status(child.concept_id, {parent}, onto) == "ok"  # 用途の文に上位の概念（例: 電池）
    assert engine.use_status(parent, {child.concept_id}, onto) == "ok"  # 用途の文に下位の概念
    assert engine.use_status(child.concept_id, {other}, onto) == "weak"
    assert engine.use_status(child.concept_id, set(), onto) == "weak"
    mcon = next(c.concept_id for c in onto.concepts.values() if c.top_type == "M" and c.concept_id not in onto.top_types)
    assert engine.use_status(mcon, set(), onto) is None  # 用途でない概念は判定しない


def test_modular_common_layer_with_extension(tmp_path, monkeypatch):
    """部品で切り替える共通層（tech-0.1）にも拡張層を重ねられ、足した概念はどの分野の文書でも使う。"""
    monkeypatch.setenv("ONTOMAP_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "run_project", lambda pid: None)
    d = engine.pdir(PID)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": PID, "name": "試験", "ontology": "tech-0.1", "use_extension": True}), encoding="utf-8")
    pd.DataFrame([{"doc_id": "D0001", "title": "t", "problem": "", "solution": "", "use_text": ""}]).to_csv(d / "source.csv", index=False)
    mo = engine.project_modular(PID, False)
    assert mo is not None and {"battery", "semicon", "inorganic"} <= set(mo.routes)
    assert mo.modules_for("H01M10/0525") == {"battery"} and mo.modules_for("H01L21/3065") == {"semicon"}
    engine.adopt(PID, {"term": "試験用の語", "mode": "concept", "top": "M", "parent": "M"})
    mo2 = engine.project_modular(PID, True)
    cid = layers.concept_id("試験用の語", "M")
    assert cid in mo2.onto.concepts and mo2.allowed("試験用の語", cid, {"semicon"}) and mo2.allowed("試験用の語", cid, {"battery"})
    assert engine.project_ontology(PID, True).concepts[cid].preferred_label == "試験用の語"
    assert engine.project_modular(PID, True, built=True).onto.concepts.get(cid) is None  # 描いた時点の控えにはまだ無い


def test_pack_islands_spreads_disconnected_groups():
    import numpy as np

    rng = np.random.default_rng(0)
    xy = np.vstack([rng.normal(0, 0.05, (60, 2)), rng.normal(0, 0.05, (40, 2)) + [10, 0]])
    S = np.zeros((100, 100))
    S[:60, :60] = 0.5
    S[60:, 60:] = 0.5
    out, n = engine._pack_islands(xy, S)
    z = engine._normalize(out)
    assert n == 2
    assert (np.percentile(z[:60], 90, axis=0) - np.percentile(z[:60], 10, axis=0)).min() > 0.2  # 島の中が読める広さになる
    one = rng.normal(0, 0.3, (100, 2))
    same, n1 = engine._pack_islands(one, None)
    assert n1 == 1 and np.allclose(same, one)  # 1 つの塊なら変えない


def test_similarity_summary_rules():
    """類似の根拠の要約文は、一般的な概念を除いた共通の概念からルールで作る。根拠が足りなければ推測しない。"""
    from collections import Counter

    onto = layers.merged(layers.base_dict(BASE), None)
    leaf = lambda top: [c.concept_id for c in onto.concepts.values() if c.top_type == top and not any(c.concept_id in x.parent_ids for x in onto.concepts.values())]  # noqa: E731
    p1, p2 = leaf("P")[:2]
    m1, m2 = leaf("M")[:2]
    u1, u2 = leaf("U")[:2]
    ctx = {"onto": onto, "generic": {m2}, "use_conf": {"A": {u1}, "B": set()}}
    roles = lambda p, m, u: {"課題": Counter(p), "手段": Counter(m), "用途": Counter(u)}  # noqa: E731
    r = engine._compare_roles(ctx, "A", "B", roles([p1], [m1], [u1]), roles([p1], [m1], [u2]), [{"score": 1.0, "generic": False}])
    assert onto.label(p1) in r["summary"] and onto.label(m1) in r["summary"] and "用途は異なります" in r["summary"]
    assert "要確認" in r["summary"]  # 相手の用途は用途を述べた文で確かめられない
    assert r["shared"]["problem"] == [onto.label(p1)] and r["only_b"]["use"] == [onto.label(u2)]
    r = engine._compare_roles(ctx, "A", "B", roles([], [m2], []), roles([], [m2], []), [{"score": 0.8, "generic": True}])
    assert "根拠が不足" in r["summary"] and onto.label(m2) not in r["shared"]["solution"]  # 一般的な概念だけの一致は説明しない
    r = engine._compare_roles(ctx, "A", "B", roles([], [], []), roles([], [], []), [])
    assert r["summary"].startswith("技術関係の一致はありません")


def test_stats_ipc_main_group():
    """統計の IPC メイングループの切り出し（例: A23L2/56 → A23L2、空白入りも可）。"""
    from ontomap.app.engine import _IPC_MAIN

    got = [(m.group(1) + m.group(2)) if (m := _IPC_MAIN.match(c)) else None for c in ["A23L2/56", "A61K31/167", "C12C 5/02", "A23L", "xx"]]
    assert got == ["A23L2", "A61K31", "C12C5", None, None]
