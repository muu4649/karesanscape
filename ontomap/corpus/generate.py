"""見本コーパス（実文書の代わり 120 件 + 制御文書 80 件）と Gold graph を生成する。

注意: これは実験パイプラインを通すための合成データである。実文書の結果として扱わない。
計画書どおり、制御文書は生成後に人が論理を確認する（review_sheet.csv を出力する）。
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from ..ontology import Ontology
from ..schema import ConceptMention, Document, GraphLayer, Triple, save_documents
from .spec import DOMAINS, INVERTED_TEMPLATES, MECHANISMS, PARAPHRASE_TEMPLATES, PREFACES, REAL_TEMPLATES, SUFFIXES

SLOTS = ("T", "P", "M", "A", "E")


@dataclass
class Truth:
    """生成時の正解情報（手法からは見えない。模擬ラベルと誤差分析にだけ使う）。"""

    doc_id: str
    mechanism_id: str
    domain: str
    kind: str  # real / paraphrase / inverted_role / inverted_direction
    template_id: str
    source_doc_id: str
    slots: dict[str, str]
    problem: str
    solution: str | None
    target: str


def _surface(onto: Ontology, cid: str, mode: str, rng: random.Random, avoid: str | None = None) -> str:
    """概念の表記を選ぶ。mode=preferred / mixed / alias。"""
    c = onto.concepts[cid]
    if mode == "preferred" or not c.aliases:
        return c.preferred_label
    if mode == "alias":
        cands = [a for a in c.aliases if a != avoid] or c.aliases
        return rng.choice(cands)
    return c.preferred_label if rng.random() < 0.5 else rng.choice(c.aliases)


def _fit_length(core: str, domain: str, lo: int, hi: int, rng: random.Random) -> str:
    """前置き文・後置き文を足して lo〜hi 文字に寄せる。"""
    pres = [""] + PREFACES[domain]
    sufs = [""] + SUFFIXES
    combos = [(p, s) for p in pres for s in sufs]
    rng.shuffle(combos)
    target = (lo + hi) / 2
    best = min(combos, key=lambda ps: (not (lo <= len(ps[0] + core + ps[1]) <= hi), abs(len(ps[0] + core + ps[1]) - target)))
    return best[0] + core + best[1]


def _render(template: str, surf: dict[str, str]) -> str:
    return template.format(**surf)


def _find_all(text: str, s: str) -> list[int]:
    out, i = [], text.find(s)
    while i >= 0:
        out.append(i)
        i = text.find(s, i + 1)
    return out


def _gold_from_template(
    doc_id: str, text: str, triples_spec: list, slots: dict[str, str], surf: dict[str, str]
) -> list[Triple]:
    out = []
    for s_slot, pred, o_slot, s_role, o_role, neg, spec in triples_spec:
        s_pos = _find_all(text, surf[s_slot])
        o_pos = _find_all(text, surf[o_slot])
        if s_pos and o_pos:
            a = min(s_pos[0], o_pos[-1])
            b = max(s_pos[0] + len(surf[s_slot]), o_pos[-1] + len(surf[o_slot]))
        else:
            a, b = 0, len(text)
        out.append(
            Triple(
                doc_id=doc_id,
                subject_id=slots[s_slot],
                predicate=pred,
                object_id=slots[o_slot],
                subject_role=s_role,
                object_role=o_role,
                confidence=1.0,
                negated=neg,
                speculative=spec,
                evidence_span=[a, b],
                evidence_text=text[a:b],
                subject_text=surf[s_slot],
                object_text=surf[o_slot],
            )
        )
    return out


def _gold_mentions(doc_id: str, text: str, slots: dict[str, str], surf: dict[str, str]) -> list[ConceptMention]:
    out = []
    for k in SLOTS:
        for p in _find_all(text, surf[k]):
            out.append(ConceptMention(doc_id, slots[k], p, p + len(surf[k]), surf[k]))
    out.sort(key=lambda m: m.start)
    return out


def generate_sample_corpus(onto: Ontology, out_dir: Path, seed: int = 42, target_chars: tuple[int, int] = (90, 110)) -> dict:
    """data/ に documents.csv・ontology.json・_truth.jsonl・review_sheet.csv、graphs/gold/ に Gold graph を書く。"""
    rng = random.Random(seed)
    lo, hi = target_chars
    raw: list[dict] = []  # 一時 ID で作り、最後に並べ替えて D001.. を振る

    for mi, (mid, dom, T, P, M, A, E) in enumerate(MECHANISMS):
        slots = {"T": T, "P": P, "M": M, "A": A, "E": E}
        source_tmp = None
        source_surf = None
        for k in range(3):
            tid, tpl, spec = REAL_TEMPLATES[(mi + k) % len(REAL_TEMPLATES)]
            mode = "preferred" if k == 0 else "mixed"
            surf = {s: _surface(onto, slots[s], mode, rng) for s in SLOTS}
            text = _fit_length(_render(tpl, surf), dom, lo, hi, rng)
            tmp = f"{mid}-r{k}"
            raw.append(dict(tmp=tmp, mid=mid, dom=dom, kind="real", tid=tid, src="", slots=slots, surf=surf, text=text, spec=spec))
            if k == 0:
                source_tmp, source_surf = tmp, surf
        # 言い換え: 元文書と別の表記・別の文型
        tid, tpl, spec = PARAPHRASE_TEMPLATES[mi % len(PARAPHRASE_TEMPLATES)]
        surf = {s: _surface(onto, slots[s], "alias", rng, avoid=source_surf[s]) for s in SLOTS}
        text = _fit_length(_render(tpl, surf), dom, lo, hi, rng)
        raw.append(dict(tmp=f"{mid}-para", mid=mid, dom=dom, kind="paraphrase", tid=tid, src=source_tmp, slots=slots, surf=surf, text=text, spec=spec))
        # 関係反転: 元文書と同じ表記（語彙は共通）で関係だけ変える
        inv_kind = "role" if mi % 2 == 0 else "direction"
        tid, tpl, spec = INVERTED_TEMPLATES[inv_kind][(mi // 2) % 2]
        surf = dict(source_surf)
        text = _fit_length(_render(tpl, surf), dom, lo, hi, rng)
        raw.append(dict(tmp=f"{mid}-inv", mid=mid, dom=dom, kind=f"inverted_{inv_kind}", tid=tid, src=source_tmp, slots=slots, surf=surf, text=text, spec=spec))

    order = list(range(len(raw)))
    rng.shuffle(order)
    tmp_to_id = {raw[i]["tmp"]: f"D{n + 1:03d}" for n, i in enumerate(order)}

    docs: list[Document] = []
    truths: list[Truth] = []
    gold_t: dict[str, list[Triple]] = {}
    gold_c: dict[str, list[ConceptMention]] = {}
    for i in order:
        r = raw[i]
        did = tmp_to_id[r["tmp"]]
        src_id = tmp_to_id.get(r["src"], "")
        stype = {"real": "real", "paraphrase": "paraphrase"}.get(r["kind"], "inverted")
        docs.append(Document(did, stype, DOMAINS[r["dom"]], r["text"], len(r["text"]), src_id))
        sl = r["slots"]
        if r["kind"] == "inverted_role":
            problem, solution, target = sl["P"], None, sl["M"]
        elif r["kind"] == "inverted_direction":
            problem, solution, target = sl["P"], None, sl["T"]
        else:
            problem, solution, target = sl["P"], sl["M"], sl["T"]
        truths.append(Truth(did, r["mid"], r["dom"], r["kind"], r["tid"], src_id, sl, problem, solution, target))
        gold_t[did] = _gold_from_template(did, r["text"], r["spec"], sl, r["surf"])
        gold_c[did] = _gold_mentions(did, r["text"], sl, r["surf"])

    docs.sort(key=lambda d: d.doc_id)
    data = out_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    save_documents(docs, data / "documents.csv")
    onto.export_json(data / "ontology.json")
    with (data / "_truth.jsonl").open("w", encoding="utf-8") as f:
        for t in sorted(truths, key=lambda t: t.doc_id):
            f.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")
    GraphLayer("gold", gold_t, gold_c).save(out_dir / "graphs")

    # 制御文書の人手確認シート（計画書 2 章: 生成後、人手で論理を確認）
    by_id = {d.doc_id: d for d in docs}
    rows = []
    for t in sorted(truths, key=lambda t: t.doc_id):
        if t.kind == "real":
            continue
        rows.append(
            {
                "control_doc_id": t.doc_id,
                "control_type": t.kind,
                "source_doc_id": t.source_doc_id,
                "source_text": by_id[t.source_doc_id].text,
                "control_text": by_id[t.doc_id].text,
                "logic_ok(1/0)": "",
                "comment": "",
            }
        )
    pd.DataFrame(rows).to_csv(data / "review_sheet.csv", index=False)
    lengths = [d.char_count for d in docs]
    return {
        "n_docs": len(docs),
        "n_real": sum(d.source_type == "real" for d in docs),
        "n_paraphrase": sum(d.source_type == "paraphrase" for d in docs),
        "n_inverted": sum(d.source_type == "inverted" for d in docs),
        "chars_min": min(lengths),
        "chars_max": max(lengths),
        "chars_mean": sum(lengths) / len(lengths),
        "in_range": sum(lo <= x <= hi for x in lengths),
    }


def load_truth(run_dir: Path) -> dict[str, Truth]:
    """_truth.jsonl を読む（見本コーパスのときだけ存在する）。"""
    p = run_dir / "data" / "_truth.jsonl"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["doc_id"]] = Truth(**d)
    return out
