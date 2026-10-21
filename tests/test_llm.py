"""LLM 抽出層のテスト。API は呼ばず、偽のクライアントで応答の扱いだけを確かめる。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ontomap import pipeline as pl
from ontomap.extract.llm import _schema, _system_prompt, build_llm_layer
from ontomap.ontology import Ontology

ROOT = Path(__file__).resolve().parent.parent


class FakeMessages:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        triples = [{
            "subject_id": "M_cooling_channel", "subject_text": "冷却流路", "subject_role": "手段", "predicate": "suppresses",
            "object_id": "P_temp_rise", "object_text": "温度上昇", "object_role": "課題", "negated": False, "speculative": False,
            "evidence": text.split("本文: ")[1][:10], "confidence": 0.9,
        }]
        return SimpleNamespace(
            stop_reason="end_turn", model=kw["model"],
            content=[SimpleNamespace(type="text", text=json.dumps({"triples": triples}, ensure_ascii=False))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def test_prompt_and_schema():
    o = Ontology.load(ROOT / "config" / "ontology_v0_1.yaml")
    sp = _system_prompt(o)
    assert "M_cooling_channel" in sp and "suppresses" in sp
    sc = _schema(o)
    assert sc["properties"]["triples"]["items"]["additionalProperties"] is False


def test_build_layer_with_fake_client(tmp_path):
    run = pl.open_run(ROOT / "config" / "experiment.yaml", "t", base=tmp_path)
    pl.step_data(run)
    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    info = build_llm_layer(run, limit=3, client=fake)
    assert info["n_docs"] == 3 and info["input_tokens"] == 300
    call = fake.beta.messages.calls[0]
    assert call["fallbacks"] == "default" and call["output_config"]["format"]["type"] == "json_schema"
    assert (run.root / "graphs" / "llm" / "triples.jsonl").exists()
    again = build_llm_layer(run, limit=3, client=fake)  # キャッシュがあれば再要求しない
    assert again["n_new_requests"] == 0
