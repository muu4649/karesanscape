"""Dictionary graph: 辞書の最長一致と、格助詞・動詞の手がかり規則で三つ組を抽出する。

学習は使わない。規則は特定の文型に合わせず、特許・技術文の一般的な言い回しを手がかりにする。
そのため誤りを含む。誤りの大きさは Gold graph との概念 F1・三つ組 F1 で測る。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..ontology import Ontology
from ..schema import TOP_TO_ROLE, ConceptMention, Document, GraphLayer, Triple

SUPPRESS = ["抑え込", "抑制", "抑え", "低減", "軽減", "減らす", "減ら", "解消", "防止", "防ぐ", "防ぎ"]
PREVENT = {"防止", "防ぐ", "防ぎ"}
CAUSE = ["引き起こ", "招く", "招き", "原因とな", "増大"]
DEGRADE_TARGET = ["劣化させ", "劣化する"]
IMPAIR = ["損な", "低下し", "低下する", "失われ"]
OCCUR_AFTER = ("が生じ", "が発生", "が起こ", "が問題", "が進行", "が起き")
LOCATION_AFTER = ("において", "における", "では", "で起こる", "で発生", "自体の", "で")
INSTRUMENT_AFTER = ("によって", "により", "を用いて", "を用い", "を設け", "を備え", "を配置", "を採用", "を組み込", "を使", "が")
CAUSE_SUBJECT_AFTER = ("に伴", "の副作用", "は", "が", "を設けると")
NEGATION = ("ない", "にくい", "損なわれ", "失われ", "ず", "できなく")
SPECULATION = ("おそれ", "可能性", "と考えられ", "かもしれ", "と推測")


@dataclass
class Mention:
    """抽出時の内部表現。"""

    cid: str
    start: int
    end: int
    surface: str
    top: str
    sent: int
    role: str = ""
    is_location: bool = False


def find_mentions(text: str, onto: Ontology) -> list[tuple[int, int, str, str]]:
    """最長一致で概念の出現を拾う（重なりなし）。"""
    forms = onto.surface_forms()
    out = []
    i = 0
    while i < len(text):
        hit = None
        for s, cid in forms:
            if text.startswith(s, i):
                hit = (i, i + len(s), s, cid)
                break
        if hit:
            out.append(hit)
            i = hit[1]
        else:
            i += 1
    return out


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer("。", text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _clause_end(text: str, pos: int) -> int:
    """pos から読点・句点までの位置。"""
    for j in range(pos, len(text)):
        if text[j] in "、。":
            return j
    return len(text)


class DictionaryExtractor:
    """辞書＋規則の抽出器。"""

    def __init__(self, onto: Ontology) -> None:
        self.onto = onto

    def extract(self, doc: Document) -> tuple[list[Triple], list[ConceptMention]]:
        """1 文書から三つ組と概念出現を返す。"""
        text = doc.text
        sents = _sentences(text)

        def sent_of(pos: int) -> int:
            for k, (a, b) in enumerate(sents):
                if a <= pos < b:
                    return k
            return len(sents) - 1

        ms = [
            Mention(cid, a, b, s, self.onto.top_type(cid), sent_of(a))
            for a, b, s, cid in find_mentions(text, self.onto)
        ]
        concepts = [ConceptMention(doc.doc_id, m.cid, m.start, m.end, m.surface) for m in ms]
        for m in ms:
            m.role = TOP_TO_ROLE[m.top]

        def after(m: Mention, n: int = 8) -> str:
            return text[m.end : m.end + n]

        triples: list[Triple] = []
        seen: set[tuple[str, str, str]] = set()

        def add(s: Mention, pred: str, o: Mention, conf: float, s_role: str | None = None, o_role: str | None = None, neg=False, spec=False) -> None:
            if s.cid == o.cid:
                return
            key = (s.cid, pred, o.cid)
            if key in seen:
                return
            seen.add(key)
            a, b = min(s.start, o.start), max(s.end, o.end)
            triples.append(
                Triple(
                    doc_id=doc.doc_id,
                    subject_id=s.cid,
                    predicate=pred,
                    object_id=o.cid,
                    subject_role=s_role or s.role,
                    object_role=o_role or o.role,
                    confidence=conf,
                    negated=neg,
                    speculative=spec,
                    evidence_span=[a, b],
                    evidence_text=text[a:b],
                    subject_text=s.surface,
                    object_text=o.surface,
                )
            )

        # --- 1. 課題の発生場所（occurs_in）。場所になった概念は役割を「対象」にする ---
        for k, p in enumerate(ms):
            if p.top != "P":
                continue
            prev = [m for m in ms[:k] if m.sent == p.sent and m.top != "P"]
            loc = None
            if prev:
                cand = prev[-1]
                gap = text[cand.end : p.start]
                if gap in ("の", "における", "において", "では", "で起こる", "自体の", "で", "に設けた") or after(p, 4).startswith(OCCUR_AFTER):
                    if gap.startswith(LOCATION_AFTER) or gap in ("の", "に設けた") or gap.endswith("で"):
                        loc = cand
                if loc is None and after(p).startswith(OCCUR_AFTER):
                    for c in reversed(prev):
                        if after(c).startswith(LOCATION_AFTER):
                            loc = c
                            break
            if loc is not None and loc.top in ("T", "M"):
                loc.is_location = True
                loc.role = "対象"
                add(p, "occurs_in", loc, 0.9 if loc.top == "T" else 0.8, "課題", "対象")

        # 同じ概念の別出現にも「対象」役割を伝える（例: 劣化を受ける手段）
        for m in ms:
            if any(o.cid == m.cid and o.is_location for o in ms) and m.top == "M":
                m.role = "対象"

        def instrument_in(sent: int, before: int | None = None) -> Mention | None:
            cands = [m for m in ms if m.sent == sent and m.top == "M" and m.role == "手段" and (before is None or m.start < before)]
            marked = [m for m in cands if after(m).startswith(INSTRUMENT_AFTER)]
            pick = marked or cands
            return pick[-1] if pick else None

        def last_instrument(before: int) -> Mention | None:
            cands = [m for m in ms if m.top == "M" and m.role == "手段" and m.start < before]
            return cands[-1] if cands else None

        def object_before(pos: int, sent: int) -> Mention | None:
            cands = [m for m in ms if m.sent == sent and m.end <= pos]
            for m in reversed(cands):
                gap = text[m.end : pos]
                if gap in ("を", "が", "は", "を一層", "も"):
                    return m
            return None

        # --- 2. 抑制・防止 ---
        for word in SUPPRESS:
            for mt in re.finditer(word, text):
                pos = mt.start()
                if any(m.start <= pos < m.end for m in ms):  # 概念語の一部（例: 低反射コーティング）は手がかりにしない
                    continue
                si = sent_of(pos)
                obj = object_before(pos, si)
                if obj is None or obj.top not in ("P",):
                    continue
                subj = instrument_in(si, before=pos)
                conf = 0.9
                if subj is None:
                    acts = [m for m in ms if m.sent == si and m.top == "A" and m.start < pos]
                    subj = acts[-1] if acts else last_instrument(pos)
                    conf = 0.6
                if subj is None:
                    continue
                clause = text[pos : _clause_end(text, pos) + 1]
                add(subj, "prevents" if word in PREVENT else "suppresses", obj, conf,
                    neg=clause.startswith(word) and any(n in clause for n in NEGATION[:1]) and "ないよう" not in clause)

        # --- 3. 課題が対象を劣化させる / 作用を損なう ---
        for word in DEGRADE_TARGET + IMPAIR:
            for mt in re.finditer(word, text):
                pos = mt.start()
                si = sent_of(pos)
                obj = object_before(pos, si)
                if obj is None:
                    continue
                probs = [m for m in ms if m.top == "P" and m.start < pos and m.sent in (si, si - 1)]
                if not probs:
                    continue
                subj = probs[-1]
                if word in DEGRADE_TARGET and obj.top in ("M", "T"):
                    obj.role = "対象"
                    add(subj, "degrades", obj, 0.8, "課題", "対象")
                elif word in IMPAIR and obj.top == "A":
                    add(subj, "suppresses", obj, 0.7, "課題", "作用")

        # --- 4. 引き起こす ---
        for word in CAUSE:
            for mt in re.finditer(word, text):
                pos = mt.start()
                si = sent_of(pos)
                before = [m for m in ms if m.sent == si and m.start < pos]
                prob = next((m for m in reversed(before) if m.top == "P"), None)
                if prob is None:
                    continue
                subj = None
                for m in reversed(before):
                    if m is prob or m.top not in ("M", "A"):
                        continue
                    if after(m, 6).startswith(CAUSE_SUBJECT_AFTER):
                        subj = m
                        break
                if subj is None:
                    continue
                add(subj, "causes", prob, 0.8)

        # --- 5. 手段が作用を生む ---
        for a in ms:
            if a.top != "A":
                continue
            if any(t.object_id == a.cid and t.predicate == "suppresses" and t.subject_role == "課題" for t in triples):
                continue
            inst = instrument_in(a.sent, before=a.start)
            if inst is not None:
                add(inst, "performs", a, 0.8)
            else:
                li = last_instrument(a.start)
                if li is not None:
                    add(li, "performs", a, 0.5)

        # --- 6. 効果 ---
        for e in ms:
            if e.top != "E":
                continue
            clause = text[e.end : _clause_end(text, e.end) + 1]
            neg = any(n in clause for n in NEGATION)
            spec = any(s in clause for s in SPECULATION) or any(s in text[e.end : sents[e.sent][1]] for s in SPECULATION)
            acts = [m for m in ms if m.sent == e.sent and m.top == "A" and m.start < e.start]
            subj = acts[-1] if acts else None
            conf = 0.7
            if subj is None:
                subj = instrument_in(e.sent, before=e.start) or last_instrument(e.start)
                conf = 0.6
            if subj is None:
                acts_all = [m for m in ms if m.top == "A" and m.start < e.start]
                subj = acts_all[-1] if acts_all else None
                conf = 0.5
            if subj is not None:
                add(subj, "leads_to", e, conf, neg=neg, spec=spec)

        # --- 7. 手段の適用先 ---
        for k, m in enumerate(ms):
            if m.top != "M":
                continue
            nxt = ms[k + 1] if k + 1 < len(ms) else None
            prv = ms[k - 1] if k > 0 else None
            if nxt is not None and nxt.top == "T" and text[m.end : nxt.start] in ("を採用した", "を用いた", "を備えた", "を有する"):
                add(m, "applied_to", nxt, 0.9)
            elif prv is not None and prv.top == "T" and text[prv.end : m.start] in ("に", "に設けた"):
                add(m, "applied_to", prv, 0.8, s_role=m.role)
        return triples, concepts


def build_dictionary_layer(docs: list[Document], onto: Ontology) -> GraphLayer:
    """全文書を抽出して層にする。"""
    ex = DictionaryExtractor(onto)
    triples, concepts = {}, {}
    for d in docs:
        t, c = ex.extract(d)
        triples[d.doc_id] = t
        concepts[d.doc_id] = c
    return GraphLayer("dict", triples, concepts)
