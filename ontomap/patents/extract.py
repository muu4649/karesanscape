"""段落の構造を役割の手がかりにした辞書抽出（課題・解決手段・用途の三つ組）。

規則（設定ファイル extraction で値を変えられる）:
1. 概念は辞書の最長一致（extract.dictionary.find_mentions）で、段落ごとに拾う。
2. 役割は「出た段落」と「概念の上位型」の両方で決める。
   - 段落の想定型（課題段落=P、解決手段段落=M、用途段落=U）と上位型が一致 → その役割（一致）
   - 食い違い（例: 課題段落に出た M 型 = 従来技術の材料、解決手段段落に出た P 型 = 解く課題の言い直し）
     mismatch_policy: type（既定）= 上位型の役割を採り、三つ組の信頼度に cross_type_factor を掛ける
                      section = 段落の役割を採る / drop = その出現を捨てる
   - P/M/U 以外の上位型の概念は、概念集合（P1）には入れるが三つ組は作らない
3. 三つ組: 手段×課題 → addresses、手段×用途 → used_for、課題×用途 → arises_in（始点→終点）。
   信頼度は、同じ文 > 同じ段落の別の文 > 段落をまたぐ共起（値は confidence）。
   同じ (始点, 述語, 終点) は最も高い信頼度の出現を採る。1 文書の三つ組は max_triples_per_doc 件まで。
4. 否定: 同じ文の共起だけ、後ろ側の概念から読点・句点までに否定の手がかり（negation.cues）があれば negated。
   手がかりは「できない」「困難」など手段が課題を解けない言い回しに限る（「課題が生じない」は解決の記述なので否定にしない）。
   「ないよう」「ことなく」（negation.exclude）は除く。推量は扱わない（speculative=False）。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from ..extract.dictionary import find_mentions
from ..ontology import Ontology
from ..schema import TOP_TO_ROLE, ConceptMention, GraphLayer, Triple
from .data import PatentRecord

DEFAULT_EXTRACTION: dict = {
    "sections": {"problem": "P", "solution": "M", "use": "U"},  # 抽出する段落 → 想定する上位型
    "role_types": {"P": "課題", "M": "手段", "U": "用途"},
    "mismatch_policy": "type",
    "cross_type_factor": 0.8,
    "relations": {
        "addresses": {"subject": "手段", "object": "課題"},
        "used_for": {"subject": "手段", "object": "用途"},
        "arises_in": {"subject": "課題", "object": "用途"},
    },
    "confidence": {"sentence": 0.9, "section": 0.7, "cross_section": 0.5},
    "max_triples_per_doc": 30,
    "case_insensitive": True,
    "negation": {
        # 「手段では課題を解けない」型の言い回しだけを否定とみなす。「課題が生じない」は解決の記述なので否定にしない
        "cues": ["できない", "できず", "得ない", "困難", "不可能", "至らない"],
        "exclude": ["ないよう", "ことなく"],
    },
}
_SENT = re.compile(r"。|(?<!\d)\.(?!\d)")


def merged_config(cfg: dict | None) -> dict:
    """既定値に設定ファイルの値を重ねる（入れ子の dict は 1 段だけ合成）。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_EXTRACTION.items()}
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("sections", "relations", "role_types"):
            out[k].update(v)
        else:
            out[k] = v
    return out


@dataclass
class SectionMention:
    """段落内の概念の出現（位置は連結本文での位置）。"""

    cid: str
    start: int
    end: int
    surface: str
    top: str
    section: str
    sent: int
    role: str
    type_match: bool


class _Forms:
    """find_mentions に渡す表記表。本文に含まれる表記だけに絞る（結果は同じで速い）。"""

    def __init__(self, forms: list[tuple[str, str]]) -> None:
        self._forms = forms

    def surface_forms(self) -> list[tuple[str, str]]:
        return self._forms


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENT.finditer(text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


class SectionExtractor:
    """段落付き特許から概念出現と三つ組を作る。"""

    def __init__(self, onto: Ontology, cfg: dict | None = None) -> None:
        self.onto = onto
        self.cfg = merged_config(cfg)
        missing = [p for p in self.cfg["relations"] if p not in onto.predicates]
        if missing:
            raise ValueError(f"オントロジーに無い述語: {missing}（relations の述語 ID をオントロジーに合わせてください）")
        ci = self.cfg["case_insensitive"]
        forms = []
        for s, cid in onto.surface_forms():
            n = unicodedata.normalize("NFKC", s)
            forms.append((n.lower() if ci else n, cid))
        forms.sort(key=lambda x: (-len(x[0]), x[0]))
        self.forms = forms
        self.stats_roles: Counter = Counter()
        self.stats_triples: Counter = Counter()
        self.n_capped = 0

    def role_of(self, top: str, section: str) -> tuple[str | None, bool, str]:
        """(役割, 上位型と段落が一致したか, 判定の種類)。役割 None は出現を捨てる。"""
        rt = self.cfg["role_types"]
        expected = self.cfg["sections"][section]
        if top == expected:
            return rt[top], True, "match"
        if top not in rt:
            return TOP_TO_ROLE.get(top, self.onto.top_types.get(top, top)), False, "other_type"
        pol = self.cfg["mismatch_policy"]
        if pol == "drop":
            return None, False, "dropped"
        if pol == "section":
            return rt[expected], False, "mismatch_section_role"
        return rt[top], False, "mismatch_type_role"

    def mentions(self, rec: PatentRecord) -> list[SectionMention]:
        """抽出対象の段落ごとに概念を拾い、役割を付ける。"""
        out: list[SectionMention] = []
        sent_base = 0
        for key in self.cfg["sections"]:
            body = rec.sections.get(key, "")
            if not body:
                continue
            off = rec.offsets[key]
            probe = body.lower() if self.cfg["case_insensitive"] and len(body.lower()) == len(body) else body
            forms = _Forms([f for f in self.forms if f[0] in probe])
            spans = _sentence_spans(body)
            for a, b, _s, cid in find_mentions(probe, forms):
                top = self.onto.top_type(cid)
                role, match, kind = self.role_of(top, key)
                self.stats_roles[(key, top, kind)] += 1
                if role is None:
                    continue
                k = next(i for i, (x, y) in enumerate(spans) if x <= a < y)
                out.append(SectionMention(cid, off + a, off + b, body[a:b], top, key, sent_base + k, role, match))
            sent_base += len(spans)
        return out

    def _negated(self, text: str, later_end: int) -> bool:
        neg = self.cfg["negation"]
        j = later_end
        while j < len(text) and text[j] not in "、。,\n":
            j += 1
        clause = text[later_end:j]
        if any(x in clause for x in neg.get("exclude", [])):
            return False
        return any(c in clause for c in neg.get("cues", []))

    def extract(self, rec: PatentRecord) -> tuple[list[Triple], list[ConceptMention]]:
        """1 文書の三つ組と概念出現。"""
        ms = self.mentions(rec)
        concepts = [ConceptMention(rec.doc_id, m.cid, m.start, m.end, m.surface) for m in ms]
        conf = self.cfg["confidence"]
        xf = self.cfg["cross_type_factor"]
        text = rec.text
        best: dict[tuple[str, str, str], tuple[float, int, Triple, str]] = {}
        for pred, spec in self.cfg["relations"].items():
            subs = [m for m in ms if m.role == spec["subject"]]
            objs = [m for m in ms if m.role == spec["object"]]
            for s in subs:
                for o in objs:
                    if s.cid == o.cid:
                        continue
                    if s.sent == o.sent:
                        prox = "sentence"
                    elif s.section == o.section:
                        prox = "section"
                    else:
                        prox = "cross_section"
                    c = conf[prox] * (1.0 if s.type_match else xf) * (1.0 if o.type_match else xf)
                    neg = prox == "sentence" and self._negated(text, max(s.end, o.end))
                    a, b = min(s.start, o.start), max(s.end, o.end)
                    if prox == "sentence":
                        ev = text[a:b]
                    else:
                        h = rec.headings
                        ev = f"［{h[s.section]}］{s.surface} … ［{h[o.section]}］{o.surface}"
                    t = Triple(rec.doc_id, s.cid, pred, o.cid, s.role, o.role, round(c, 4), neg, False, [a, b], ev, s.surface, o.surface)
                    key = (s.cid, pred, o.cid)
                    rank = {"sentence": 0, "section": 1, "cross_section": 2}[prox]
                    old = best.get(key)
                    if old is None or (c, -rank) > (old[0], -old[1]):
                        best[key] = (c, rank, t, prox)
        items = sorted(best.values(), key=lambda x: (-x[0], x[1], x[2].evidence_span[0]))
        cap = self.cfg["max_triples_per_doc"]
        if cap and len(items) > cap:
            self.n_capped += 1
            items = items[:cap]
        for c, _r, t, prox in items:
            self.stats_triples[(t.predicate, prox, t.negated)] += 1
        return [x[2] for x in items], concepts


def build_section_layer(recs: list[PatentRecord], onto: Ontology, cfg: dict | None = None, name: str = "dict") -> tuple[GraphLayer, dict]:
    """全文書を抽出して GraphLayer（name="dict"）にし、役割付け・三つ組の集計を返す。"""
    ex = SectionExtractor(onto, cfg)
    triples, concepts = {}, {}
    for r in recs:
        t, c = ex.extract(r)
        triples[r.doc_id] = t
        concepts[r.doc_id] = c
    stats = {
        "config": ex.cfg,
        "roles": [{"section": s, "top_type": tp, "decision": k, "count": v} for (s, tp, k), v in sorted(ex.stats_roles.items())],
        "triples": [{"predicate": p, "proximity": x, "negated": n, "count": v} for (p, x, n), v in sorted(ex.stats_triples.items())],
        "docs_capped": ex.n_capped,
        "no_concepts": [d for d in concepts if not concepts[d]],
        "no_triples": [d for d in triples if not triples[d]],
    }
    return GraphLayer(name, triples, concepts), stats
