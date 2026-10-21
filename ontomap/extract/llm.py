"""LLM graph（任意・別集計）: Claude にオントロジーの概念 ID で三つ組を抽出させる。

- API キーはコードに書かない。環境変数 ANTHROPIC_API_KEY（または .env、`ant auth login` のプロファイル）を使う。
- 応答は graphs/llm/raw/<doc_id>.json にキャッシュし、再実行しても二重に課金しない。
- トークン数と所要時間を graphs/llm/usage.csv に残す（計画書 8 章の「LLM token/費用」）。
- 拒否（stop_reason = refusal）に備え、サーバー側のフォールバックを有効にしている。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from ..ontology import Ontology
from ..schema import ROLES, ConceptMention, Document, GraphLayer, Triple

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _system_prompt(onto: Ontology) -> str:
    lines = [
        "あなたは技術文書から「課題・手段・作用・効果・対象」の関係を取り出す担当です。",
        "文書に書かれている関係だけを取り出し、書かれていない関係を推測で足さないでください。",
        "概念は下の一覧の concept_id で答えます。一覧に無い概念は concept_id を \"UNK:<本文の表記>\" にします。",
        "role は文書の中でその概念が担う役割です。同じ概念でも、手段として使われていれば「手段」、劣化や課題を受ける側なら「対象」です。",
        "否定（〜ない、損なわれる等）は negated、推量（おそれ、可能性等）は speculative に入れます。",
        "evidence には根拠となる本文の連続した部分をそのまま写します。",
        "",
        "## 述語",
    ]
    for p in onto.predicates.values():
        lines.append(f"- {p.predicate_id}: {p.label}")
    lines += ["", "## 概念（concept_id | 表記 | 別名 | 上位型）"]
    for c in onto.concepts.values():
        if c.concept_id in onto.top_types or not (c.aliases or c.concept_id in onto._leaf_ids()):
            continue
        lines.append(f"- {c.concept_id} | {c.preferred_label} | {'、'.join(c.aliases)} | {onto.top_types[c.top_type]}")
    return "\n".join(lines)


def _schema(onto: Ontology) -> dict:
    item = {
        "type": "object",
        "properties": {
            "subject_id": {"type": "string"},
            "subject_text": {"type": "string"},
            "subject_role": {"type": "string", "enum": list(ROLES)},
            "predicate": {"type": "string", "enum": list(onto.predicates)},
            "object_id": {"type": "string"},
            "object_text": {"type": "string"},
            "object_role": {"type": "string", "enum": list(ROLES)},
            "negated": {"type": "boolean"},
            "speculative": {"type": "boolean"},
            "evidence": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["subject_id", "subject_text", "subject_role", "predicate", "object_id", "object_text", "object_role", "negated", "speculative", "evidence", "confidence"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"triples": {"type": "array", "items": item}}, "required": ["triples"], "additionalProperties": False}


def _client():
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    import anthropic

    return anthropic.Anthropic()


def extract_one(client, doc: Document, system: str, schema: dict, model: str, effort: str) -> dict:
    """1 文書を抽出し、生の応答（JSON と使用量）を返す。"""
    t = time.perf_counter()
    resp = client.beta.messages.create(
        model=model,
        max_tokens=8000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"文書ID: {doc.doc_id}\n本文: {doc.text}"}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    sec = time.perf_counter() - t
    if resp.stop_reason == "refusal":
        return {"doc_id": doc.doc_id, "triples": [], "error": "refusal", "seconds": sec, "usage": {}}
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    u = resp.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    return {"doc_id": doc.doc_id, "model": resp.model, "triples": json.loads(text).get("triples", []), "seconds": sec, "usage": usage, "stop_reason": resp.stop_reason}


def to_layer(raw: dict[str, dict], docs: list[Document], onto: Ontology) -> GraphLayer:
    """生の応答を GraphLayer に直す。根拠の位置は本文から探す。"""
    triples, concepts = {}, {}
    for d in docs:
        r = raw.get(d.doc_id, {"triples": []})
        ts, ms = [], {}
        for x in r["triples"]:
            ev = x.get("evidence", "")
            a = d.text.find(ev) if ev else -1
            span = [a, a + len(ev)] if a >= 0 else [0, 0]
            t = Triple(
                doc_id=d.doc_id,
                subject_id=x["subject_id"],
                predicate=x["predicate"],
                object_id=x["object_id"],
                subject_role=x["subject_role"],
                object_role=x["object_role"],
                confidence=float(min(max(x.get("confidence", 0.5), 0.0), 1.0)),
                negated=bool(x.get("negated")),
                speculative=bool(x.get("speculative")),
                evidence_span=span,
                evidence_text=ev,
                subject_text=x.get("subject_text", ""),
                object_text=x.get("object_text", ""),
            )
            ts.append(t)
            for cid, surf in ((t.subject_id, t.subject_text), (t.object_id, t.object_text)):
                p = d.text.find(surf) if surf else -1
                ms[cid] = ConceptMention(d.doc_id, cid, max(p, 0), max(p, 0) + len(surf), surf)
        triples[d.doc_id] = ts
        concepts[d.doc_id] = list(ms.values())
    return GraphLayer("llm", triples, concepts)


def build_llm_layer(run, limit: int | None = None, client=None) -> dict:
    """全文書（または先頭 limit 件）を抽出し、graphs/llm/ に保存する。"""
    cfg = run.config["llm"]
    out = run.root / "graphs" / "llm"
    rawd = out / "raw"
    rawd.mkdir(parents=True, exist_ok=True)
    docs = run.docs[:limit] if limit else run.docs
    system, schema = _system_prompt(run.onto), _schema(run.onto)
    todo = [d for d in docs if not (rawd / f"{d.doc_id}.json").exists()]
    if todo:
        client = client or _client()
        with ThreadPoolExecutor(max_workers=cfg.get("max_concurrency", 4)) as ex:
            for r in ex.map(lambda d: extract_one(client, d, system, schema, cfg["model"], cfg.get("effort", "medium")), todo):
                (rawd / f"{r['doc_id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    raw = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in rawd.glob("*.json")}
    unknown = sum(1 for r in raw.values() for t in r["triples"] for c in (t["subject_id"], t["object_id"]) if not run.onto.is_known(c))
    to_layer(raw, run.docs, run.onto).save(run.root / "graphs")
    usage = pd.DataFrame([{"doc_id": k, "seconds": v.get("seconds", 0), **{f: v.get("usage", {}).get(f, 0) for f in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}} for k, v in raw.items()])
    usage.to_csv(out / "usage.csv", index=False)
    return {"n_docs": len(raw), "n_new_requests": len(todo), "unknown_concepts": unknown, "input_tokens": int(usage.input_tokens.sum()), "output_tokens": int(usage.output_tokens.sum()), "errors": sum(1 for r in raw.values() if r.get("error"))}
