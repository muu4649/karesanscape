"""使用者が育てる共通層（ontomap/app/common.py）のテスト。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from ontomap.app import common, engine, layers


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOMAP_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "run_project", lambda pid: None)
    return tmp_path


def _mid(cid: str, top: str) -> str:
    cs = common.concepts(cid)
    kids = {c["parent"] for c in cs}
    return next(c["id"] for c in cs if c["top"] == top and c["id"] in kids and c["module"] == "upper")


def test_create_edit_check_publish(home):
    m = common.create("inorganic-1.0", "無機材料（社内版）")
    cid = m["id"]
    st = common.state(cid)
    assert st["check"]["errors"] == [] and st["n_concepts"] == 236 and st["modules"][0]["id"] == "upper"
    parent = _mid(cid, "M")
    common.edit_concept(cid, {"op": "add", "module": "upper", "label": "試験材料", "aliases": ["テスト材料"], "parent": parent})
    new = next(c for c in common.concepts(cid, q="試験材料"))
    assert new["parent"] == parent and new["aliases"] == ["テスト材料"]
    common.edit_concept(cid, {"op": "edit", "id": new["id"], "aliases": ["テスト材料", "試験用材料"]})
    assert common.concepts(cid, q="試験用材料")[0]["id"] == new["id"]
    with pytest.raises(ValueError):
        common.edit_concept(cid, {"op": "delete", "id": parent})  # 子を持つ概念は消せない
    # 部品を足して、上層と親子でない重なりを作ると検査で止まる
    common.set_module(cid, {"op": "add", "id": "bat", "label": "電池", "routes": "H01M, H02J7"})
    res = common.edit_concept(cid, {"op": "add", "module": "bat", "label": "テスト材料", "parent": "M"})
    assert any("全分野共通" in e for e in res["errors"])
    with pytest.raises(ValueError):
        common.publish(cid)
    # 上層の概念の子にすれば細分化として通る
    bad = next(c for c in common.concepts(cid, module="bat"))
    res = common.edit_concept(cid, {"op": "edit", "id": bad["id"], "parent": new["id"]})
    assert res["errors"] == []
    m = common.publish(cid, "最初の版")
    assert m["versions"][-1]["version"] == 1
    oid = f"common:{cid}@v1"
    assert any(o["id"] == oid for o in engine.ontologies())
    assert engine.is_modular(oid) and engine.module_labels(oid)["bat"] == "電池"
    assert common.state(cid)["dirty"] is False


def test_alias_scope_and_short_alias(home):
    cid = common.create("tech-0.1", "")["id"]
    st = common.state(cid)
    assert {m["id"] for m in st["modules"]} >= {"upper", "battery", "semicon", "inorganic"}
    assert len(st["alias_scopes"]) >= 20
    c = common.concepts(cid, q="放電容量")[0]
    common.set_scope(cid, {"op": "remove", "concept": c["id"], "alias": "容量"})
    assert not any(r["alias"] == "容量" for r in common.state(cid)["alias_scopes"])
    common.set_scope(cid, {"op": "add", "concept": c["id"], "alias": "容量", "modules": ["battery", "inorganic"], "reason": "試験"})
    assert any(r["alias"] == "容量" for r in common.state(cid)["alias_scopes"])
    with pytest.raises(ValueError):
        common.set_scope(cid, {"op": "add", "concept": c["id"], "alias": "放電容量", "modules": ["battery"]})  # 表示名は限れない
    res = common.edit_concept(cid, {"op": "edit", "id": c["id"], "aliases": [*c["aliases"], "Q"]})
    assert any("1〜2 文字" in e for e in res["errors"])


def _project(home, oid: str, text: str = "試験語を用いる。") -> str:
    pid = "abcdefabcdef"
    d = engine.pdir(pid)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": pid, "name": "試験マップ", "ontology": oid, "use_extension": True}), encoding="utf-8")
    pd.DataFrame([{"doc_id": f"D{i:04d}", "title": "t", "ipc": "H01M10/0525", "fi": "", "problem": "", "solution": text, "use_text": ""}
                  for i in range(1, 13)]).to_csv(d / "source.csv", index=False)
    return pid


def test_promotion_and_upgrade(home):
    cid = common.create("inorganic-1.0", "格上げ試験")["id"]
    common.publish(cid)
    pid = _project(home, f"common:{cid}@v1")
    parent = _mid(cid, "M")
    engine.adopt(pid, {"term": "試験語", "mode": "concept", "top": "M", "parent": parent})
    pr = common.promotions(cid)
    assert pr and pr[0]["term"] == "試験語" and pr[0]["n_maps"] == 1 and pr[0]["target"] == parent
    common.promote(cid, {"term": "試験語", "mode": "concept", "module": "upper", "parent": parent})
    assert common.promotions(cid) == []  # 共通層に入った語は候補から消える
    common.publish(cid, "試験語を格上げ")
    up = engine.common_upgrade(pid)
    assert up["current"] == 1 and up["latest"] == 2
    engine.rebuild(pid, True, upgrade_common=True)
    meta = json.loads((engine.pdir(pid) / "meta.json").read_text(encoding="utf-8"))
    assert meta["ontology"] == f"common:{cid}@v2"
    ext = layers.load_extension(engine.pdir(pid))
    assert ext["concepts"] == [] and ext["removed_by_upgrade"][0]["term"] == "試験語"  # 共通層に入った語は拡張層から外す
    assert engine.common_upgrade(pid)["latest"] == 2 and engine.common_upgrade(pid)["current"] == 2


def test_corpus_candidates_for_new_module(home):
    cid = common.create("inorganic-1.0", "")["id"]
    common.set_module(cid, {"op": "add", "id": "bat", "label": "電池", "routes": "H01M"})
    pid = _project(home, "inorganic-1.0", "セル同士をバスバーで接続する。")
    res = common.corpus_candidates(cid, pid, "bat")
    assert res["n_docs"] == 12
    assert any(c["term"] == "バスバー" for c in res["candidates"])


CSV_SAMPLE = """```
型,分野,中間概念,概念,別名
課題,電池,安全性の課題,内部短絡,短絡|ショート|Si
課題,,製造の課題,コスト,費用|低コスト
解決手段,電池,電池の部材,セパレータ,セパレーター|隔離膜|費用
解決手段,電池,電池の部材,セパレータ,微多孔膜
解決手段,半導体,成膜,原子層堆積,ALD|原子層成長
用途,,機器・移動体,電気自動車,EV|電動車両
用途,半導体,素子,半導体メモリ,メモリ|DRAM
解決手段,,,構造,
振り分け,電池,,,H01M|H02J7
振り分け,半導体,,,H01L|H10B
```"""


def test_import_csv_builds_modules_and_reports_fixes(home):
    res = common.import_csv(CSV_SAMPLE.encode("utf-8"), "CSV 試験", "draft.csv")
    cid = res["id"]
    assert res["check"]["errors"] == []
    labels = {m["label"]: m for m in res["modules"]}
    assert labels["電池"]["routes"] == ["H01M", "H02J7"] and labels["半導体"]["routes"] == ["H01L", "H10B"]
    msgs = " / ".join(n["msg"] for n in res["notes"])
    assert "「Si」" in msgs  # 1〜2 文字の英数字は外す
    assert "「構造」" in msgs  # 意味の薄い語は外す
    assert "まとめた" in msgs  # 同じ概念の行はまとめる
    assert "費用" in msgs  # 共通の層（コスト）と重なる部品の別名は外す
    sep = common.concepts(cid, q="セパレータ")[0]
    assert sep["module"] != "upper" and "微多孔膜" in sep["aliases"] and "費用" not in sep["aliases"]
    assert common.concepts(cid, q="コスト")[0]["module"] == "upper"
    # 書き出して取り込み直しても同じ中身になる
    out = common.export_csv(cid)
    res2 = common.import_csv(out.encode("utf-8"), "再取り込み", "export.csv")
    a = {(c["label"], tuple(sorted(c["aliases"])), c["top"]) for c in common.concepts(cid)}
    b = {(c["label"], tuple(sorted(c["aliases"])), c["top"]) for c in common.concepts(res2["id"])}
    assert a == b
    common.publish(cid)
    assert engine.module_labels(f"common:{cid}@v1")[labels["電池"]["id"]] == "電池"


def test_import_csv_rejects_bad_header(home):
    with pytest.raises(ValueError):
        common.import_csv("a,b\n1,2\n".encode("utf-8"))


def test_prompt_and_trial(home):
    p = common.build_prompt("リチウムイオン二次電池", [{"label": "電池", "hint": "セル・電極・電解液"}], 150, "【課題】サイクル特性を向上する。")
    assert "リチウムイオン二次電池" in p and "150" in p and "振り分け" in p and "サイクル特性を向上する" in p and "{{" not in p
    p0 = common.build_prompt("触媒", [], 100, "")
    assert "空にする（分野に分けない）" in p0 and "{{" not in p0
    cid = common.import_csv(CSV_SAMPLE.encode("utf-8"), "試し当て", "t.csv")["id"]
    pid = _project(home, "inorganic-1.0", "セパレータの内部短絡を防ぐ。")
    r = common.trial(cid, pid)
    assert r["n_docs"] == 12 and r["draft"]["solution"]["rate"] == 0.0 and r["current"]["solution"]["rate"] is not None
    assert r["routes"] == {"電池": 12}
