"""オントロジーの 2 層構成。

- 共通層: 同梱（または追加）したオントロジー。版を固定し、マップごとには変えない。
- 拡張層: マップごとの追加分（projects/<id>/extension.json）。取り込んだ文書から出した「語の候補」を、
  人が採用したものだけを入れる。自動では足さない。
  - 新しい概念: 既存の中間概念（または上位型）の下に置く
  - 別名: 既存の概念に別の言い方を足す
描き直すたびに拡張層の版を上げ、使った版の写しを extension_v<n>.json に残す。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..extract.dictionary import find_mentions
from ..ontology import Ontology

SECTION_TOP = {"problem": "P", "solution": "M", "use_text": "U"}
SECTION_LABEL = {"problem": "課題", "solution": "解決手段", "use_text": "用途"}
TOP_LABEL = {"P": "課題", "M": "解決手段", "U": "用途"}
# 特許の段落によく出るが、課題・手段・用途の中身を表さない語
STOP = set("""本発明 発明 本開示 開示 前記 上記 下記 当該 該 本願 本件 課題 目的 手段 効果 用途 提供 特徴 実施形態 実施例 一態様 態様 構成 範囲 以上 以下 未満 種類 少なくとも 場合 方法 工程 ステップ
製造方法 製法 方式 装置 システム もの こと ため よう それ これ 他 その他 一種 一方 他方 各種 種々 複数 単数 全体 一部 部分 本 該当 好適 好ましく 特定 所定 所望 公知 従来 従来技術 背景技術 技術 分野 文献 特許文献
非特許文献 記載 参照 例 例えば 具体 具体的 結果 観点 点 面 際 時 後 前 中 間 上 下 内 外 量 数 値 比 率 割合 程度 範囲内 状態 条件 性質 特性 性能 機能 作用 用 化 性 度 系 型 式 剤 材 体 物 品 層 膜 部 項 請求項 図 表 同 等 等々
本明細書 明細書 一実施形態 実施形態 形態 通り 任意 単独 用語 対象 外部 傾向 クラス 状況 現状 実情 背景 事情 主目的 当技術分野 先行技術 余地 限界 欠点 理想的 商業的 特異的
所望 一般式 構造式 化学式 下記式 以下式 上記式 各々 それぞれ 1種 2種 ほか""".split())
# 英語の機能語・文献の書式
_EN_STOP = {"et", "al", "al.", "al.,", "and", "or", "of", "the", "in", "for", "to", "with", "by", "on", "j.", "j", "vol", "no", "pp", "fig", "table", "organic", "chem", "soc"}
_NOUNISH = ("名詞",)
_BAD_NOUN = ("非自立", "代名詞", "副詞可能")


def base_dict(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def empty_extension() -> dict:
    return {"version": 0, "concepts": [], "aliases": [], "ignored": []}


def load_extension(pdir: Path) -> dict:
    p = pdir / "extension.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return empty_extension()


def save_extension(pdir: Path, ext: dict) -> None:
    tmp = pdir / "extension.json.tmp"
    tmp.write_text(json.dumps(ext, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(pdir / "extension.json")


def concept_id(label: str, top: str) -> str:
    return f"X{top}_" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]


def merged(base: dict, ext: dict | None) -> Ontology:
    """共通層に拡張層（新しい概念・別名）を重ねたオントロジー。"""
    return Ontology(merged_data(base, ext))


def merged_data(base: dict, ext: dict | None) -> dict:
    """merged の dict 版。"""
    data = copy.deepcopy(base)
    if ext:
        ids = {c["id"] for c in data["concepts"]} | {t["id"] for t in data["top_types"]}
        for c in ext.get("concepts", []):
            if c["id"] in ids or c.get("parent") not in ids:
                continue
            data["concepts"].append({"id": c["id"], "label": c["label"], "aliases": list(c.get("aliases", [])), "parents": [c["parent"]]})
            ids.add(c["id"])
        by_id = {c["id"]: c for c in data["concepts"]}
        for a in ext.get("aliases", []):
            c = by_id.get(a["concept_id"])
            if c is not None and a["alias"] not in c.setdefault("aliases", []) and a["alias"] != c["label"]:
                c["aliases"].append(a["alias"])
    return data


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


class Matcher:
    """オントロジーの表記の最長一致（先頭の文字で引ける索引を使う。find_mentions と同じ結果を速く出す）。"""

    def __init__(self, onto: Ontology) -> None:
        self.index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for s, cid in onto.surface_forms():  # 長い順
            if s:
                self.index[s[0]].append((s, cid))

    def find(self, text: str) -> list[tuple[int, int, str, str]]:
        out, i, n = [], 0, len(text)
        while i < n:
            hit = None
            for s, cid in self.index.get(text[i], ()):
                if text.startswith(s, i):
                    hit = (i, i + len(s), s, cid)
                    break
            if hit:
                out.append(hit)
                i = hit[1]
            else:
                i += 1
        return out


def coverage(sections: pd.DataFrame, onto: Ontology) -> dict[str, dict]:
    """段落ごとに、対応する上位型の概念が 1 つも拾えない文書の割合（空の段落は除く）。"""
    out = {}
    for col, top in SECTION_TOP.items():
        texts = [_norm(t) for t in sections.get(col, pd.Series(dtype=str)) if str(t).strip()]
        m = Matcher(onto)
        zero = sum(1 for t in texts if not any(onto.top_type(c) == top for _, _, _, c in m.find(t)))
        out[col] = {"label": SECTION_LABEL[col], "n": len(texts), "zero": zero, "rate": round(zero / len(texts), 3) if texts else None}
    return out


def shadowed(sections: pd.DataFrame, base: Ontology, draft: Ontology, limit: int = 50) -> dict:
    """拡張層の語が最長一致で共通層の語を覆い、段落の型の概念が拾えなくなった箇所。
    例: 用途の別名「ガス吸着」を足すと、課題の段落の「ガス吸着量」が用途の語として拾われ、課題の概念「吸着性能」が消える。"""
    by_term: dict[str, dict] = {}
    n_docs = 0
    mb, md = Matcher(base), Matcher(draft)
    new = set(draft.surface_forms()) - set(base.surface_forms())  # 拡張層で増えた（表記, 概念）
    for _, row in sections.iterrows():
        hit_doc = False
        for col, top in SECTION_TOP.items():
            text = _norm(row.get(col, ""))
            if not text.strip():
                continue
            dh = md.find(text)
            for a, b, s, cid in mb.find(text):
                if base.top_type(cid) != top:
                    continue
                cover = [(da, db, ds, dc) for da, db, ds, dc in dh if da < b and a < db]
                if any(dc == cid or (draft.top_type(dc) == top and (da, db) == (a, b)) for da, db, ds, dc in cover):
                    continue
                for da, db, ds, dc in cover:
                    if (ds, dc) not in new:  # 共通層どうしの連鎖は、原因の語のほうで数える
                        continue
                    r = by_term.setdefault(ds, {"term": ds, "as": draft.label(dc), "as_top": draft.top_type(dc), "hidden": Counter(), "docs": set(), "sections": Counter()})
                    r["hidden"][base.label(cid)] += 1
                    r["docs"].add(row["doc_id"])
                    r["sections"][SECTION_LABEL[col]] += 1
                    hit_doc = True
        n_docs += hit_doc
    rows = sorted(by_term.values(), key=lambda r: -len(r["docs"]))[:limit]
    return {"n_docs": n_docs, "terms": [{"term": r["term"], "as": r["as"], "as_top": r["as_top"], "n_docs": len(r["docs"]), "docs": sorted(r["docs"])[:5],
                                         "hidden": [{"label": k, "n": v} for k, v in r["hidden"].most_common(4)], "sections": dict(r["sections"])} for r in rows]}


def _chunks(tokens) -> list[tuple[str, int, int, list[str]]]:
    """連続する名詞（接頭詞を含む）を 1 語にまとめる。返り値は (語, 開始, 終了, 各トークンの品詞細分類)。"""
    out, cur, kinds, start, pos = [], [], [], None, 0
    for t in tokens:
        s = t.surface
        pos_ = t.part_of_speech.split(",")
        ok = (pos_[0] in _NOUNISH and pos_[1] not in _BAD_NOUN) or (pos_[0] == "接頭詞" and not cur)
        if ok:
            if start is None:
                start = pos
            cur.append(s)
            kinds.append(pos_[1])
        else:
            if cur:
                out.append(("".join(cur), start, pos, kinds))
            cur, kinds, start = [], [], None
        pos += len(s)
    if cur:
        out.append(("".join(cur), start, pos, kinds))
    return out


# 単独で出たときは中身を表さない品詞（動作・評価の語。例: 形成、選択、必要、問題）
_WEAK_SINGLE = {"サ変接続", "形容動詞語幹", "ナイ形容詞語幹", "副詞可能", "数", "接尾"}


def _good(term: str, kinds: list[str] | None = None) -> bool:
    if len(term) < 2 or term in STOP:
        return False
    if re.search(r"[\"'{}\[\]:;=/\\<>|#@$^&*]", term):  # 書式の残り（JSON など）
        return False
    if re.search(r"(特許文献|非特許文献|図|表|式|実施例|比較例|段落)\d*$", term) or term.startswith(("本発明", "本実施", "本開示", "上記", "前記", "下記")):
        return False
    if term.lower().rstrip(".,") in _EN_STOP or term.lower() in _EN_STOP:
        return False
    if re.fullmatch(r"(第|R|R\d|X|Y|Z|n|m)?\d+[a-z]?", term) or re.fullmatch(r"[A-Z]\d+", term):  # 第1、R1 など
        return False
    if term.count("(") != term.count(")") or term.count("（") != term.count("）") or re.search(r"\d+\s*(wt|mol|vol)?%", term):
        return False
    if re.match(r"炭素数\d", term) or term.endswith("等"):
        return False
    if re.fullmatch(r"[A-Za-z][a-z]+", term) and len(term) > 2:  # 英単語 1 語（化学記号は残す）
        return False
    if kinds is not None and len(kinds) == 1 and kinds[0] in _WEAK_SINGLE:
        return False
    if kinds is not None and all(k in _WEAK_SINGLE for k in kinds):
        return False
    if re.fullmatch(r"[\d.,%~〜\-ー・()（）\[\]【】]+", term):
        return False
    if re.fullmatch(r"[A-Za-z]", term) or re.fullmatch(r"[ぁ-ん]+", term):
        return False
    if re.search(r"^\d", term) and len(term) < 4:
        return False
    return True


def candidates(sections: pd.DataFrame, onto: Ontology, ignored: set[str], top_n: int = 60, min_df: int = 3) -> list[dict]:
    """取り込んだ文書の課題・解決手段・用途の段落から、今のオントロジーで拾えないよく出る語を並べる。"""
    from janome.tokenizer import Tokenizer

    tk = Tokenizer()
    matcher = Matcher(onto)
    known = {_norm(s).lower() for s, _ in onto.surface_forms()}
    nk: dict[str, int] = {}
    df: dict[str, Counter] = {c: Counter() for c in SECTION_TOP}
    examples: dict[tuple[str, str], list] = defaultdict(list)
    n = len(sections)
    for _, row in sections.iterrows():
        for col in SECTION_TOP:
            text = _norm(row.get(col, ""))[:4000]
            if not text.strip():
                continue
            covered = [(a, b) for a, b, _, _ in matcher.find(text)]
            seen = set()
            for term, a, b, kinds in _chunks(tk.tokenize(text)):
                if not _good(term, kinds) or term.lower() in known or term in ignored:
                    continue
                if any(a < cb and ca < b for ca, cb in covered):
                    continue
                if term in seen:
                    continue
                seen.add(term)
                nk[term] = len(kinds)
                df[col][term] += 1
                if len(examples[(col, term)]) < 3:
                    examples[(col, term)].append({"doc_id": row["doc_id"], "text": text[max(0, a - 30): b + 30]})
    total = Counter()
    for col in SECTION_TOP:
        total.update(df[col])
    out = []
    floor = max(min_df, int(math.ceil(n * 0.005)))
    for col, cnt in df.items():
        rows = []
        for term, k in cnt.items():
            if k < floor:
                continue
            share = k / total[term]
            if share < 0.5:  # 他の段落のほうに多く出る語は、この段落の候補にしない
                continue
            score = k * (1.0 + 0.5 * min(3, nk.get(term, 1) - 1))  # 複合語を優先する
            rows.append({"term": term, "section": col, "section_label": SECTION_LABEL[col], "top": SECTION_TOP[col], "df": k, "share": round(share, 2), "score": round(score, 1), "examples": examples[(col, term)]})
        rows.sort(key=lambda r: (-r["score"], r["term"]))
        out.extend(rows[:top_n])
    return out


def suggest_targets(cands: list[dict], onto: Ontology, encoder) -> None:
    """候補ごとに、置き場所（中間概念）と別名先（末端の概念）の候補を、名前の文埋め込みの近さで 3 つずつ付ける。"""
    if not cands:
        return
    pool = defaultdict(lambda: {"mid": [], "leaf": []})
    for c in onto.concepts.values():
        if c.concept_id in onto.top_types or c.top_type not in TOP_LABEL:
            continue
        kind = "mid" if c.concept_id in {p for x in onto.concepts.values() for p in x.parent_ids} else "leaf"
        pool[c.top_type][kind].append(c)
    terms = [c["term"] for c in cands]
    labels = sorted({c.preferred_label for t in pool.values() for k in t.values() for c in k})
    vecs = encoder.encode(terms + labels)
    tv, lv = vecs[: len(terms)], {lab: vecs[len(terms) + i] for i, lab in enumerate(labels)}
    for c, v in zip(cands, tv):
        for kind, key in (("mid", "parent_suggestions"), ("leaf", "alias_suggestions")):
            items = pool[c["top"]][kind]
            if not items:
                c[key] = []
                continue
            sims = np.array([float(v @ lv[x.preferred_label]) for x in items])
            order = np.argsort(-sims)[:3]
            c[key] = [{"id": items[i].concept_id, "label": items[i].preferred_label, "sim": round(float(sims[i]), 3)} for i in order]
        c["parent_suggestions"].append({"id": c["top"], "label": f"{TOP_LABEL[c['top']]}（上位型の直下）", "sim": None})


def concept_choices(onto: Ontology) -> dict[str, list[dict]]:
    """画面の選択肢: 上位型ごとの中間概念と末端の概念。"""
    mids = {p for x in onto.concepts.values() for p in x.parent_ids}
    out: dict[str, list[dict]] = {t: [] for t in TOP_LABEL}
    for c in sorted(onto.concepts.values(), key=lambda x: x.preferred_label):
        if c.concept_id in onto.top_types or c.top_type not in TOP_LABEL:
            continue
        out[c.top_type].append({"id": c.concept_id, "label": c.preferred_label, "mid": c.concept_id in mids})
    return out
