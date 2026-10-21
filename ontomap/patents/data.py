"""documents_sections.csv（段落付きの特許）の読み込み。

1 行 = 1 特許。要約・課題・解決手段・用途の段落を見出し付きで連結した本文を schema.Document にし、
段落ごとの本文は PatentRecord.sections に持つ。表記ゆれ（全角英数など）は NFKC でそろえる。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..schema import Document

# 既定の段落（キー → CSV の列名・見出し）。連結本文はこの順に並べる。
DEFAULT_SECTIONS: dict[str, dict[str, str]] = {
    "abstract": {"column": "abstract", "heading": "要約"},
    "problem": {"column": "problem", "heading": "課題"},
    "solution": {"column": "solution", "heading": "解決手段"},
    "use": {"column": "use_text", "heading": "用途"},
}
META_COLUMNS = ("pub_number", "app_number", "title", "applicants", "filing_date", "pub_date", "ipc", "fi", "claim1", "use_source", "problem_source", "solution_source")

_PARA_NO = re.compile(r"【\d{1,5}】")
_SELECT_FIG = re.compile(r"【選択図】.*$", re.S)
_CLASS = re.compile(r"([A-H]\d{2}[A-Z])\s*(\d{1,4})\s*/\s*(\d{1,6})")


@dataclass
class PatentRecord:
    """1 特許。sections はキー → 整形済みの段落本文（空なら ""）。"""

    doc_id: str
    sections: dict[str, str]
    headings: dict[str, str]
    meta: dict[str, str] = field(default_factory=dict)
    text: str = ""
    offsets: dict[str, int] = field(default_factory=dict)  # 段落本文の、連結本文内での開始位置

    def build_text(self) -> None:
        """見出し付きで連結した本文と、各段落の開始位置を作る。空の段落は入れない。"""
        parts, pos, self.offsets = [], 0, {}
        for key, body in self.sections.items():
            if not body:
                continue
            head = f"【{self.headings[key]}】"
            if parts:
                parts.append("\n")
                pos += 1
            parts.append(head)
            pos += len(head)
            self.offsets[key] = pos
            parts.append(body)
            pos += len(body)
        self.text = "".join(parts)


def clean_text(s: str, normalize: str | None = "NFKC") -> str:
    """段落番号【0001】・選択図の記載を除き、Unicode 正規化と空白の整理をする。"""
    if not isinstance(s, str):
        return ""
    if normalize:
        s = unicodedata.normalize(normalize, s)
    s = _SELECT_FIG.sub("", s)
    s = _PARA_NO.sub("", s)
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\s*\n\s*", "", s)
    return s.strip()


def first_class(codes: str) -> tuple[str, str, str]:
    """分類記号の文字列から筆頭の記号を取り、(サブクラス, メイングループ, 記号) を返す。

    例: "B01J20/22;C07F5/06" → ("B01J", "B01J20", "B01J20/22")。取れなければ空文字。
    """
    if not isinstance(codes, str):
        return "", "", ""
    m = _CLASS.search(unicodedata.normalize("NFKC", codes))
    if not m:
        return "", "", ""
    sub, grp, sg = m.group(1), m.group(2), m.group(3)
    return sub, f"{sub}{int(grp)}", f"{sub}{int(grp)}/{sg}"


def load_patent_csv(path: str | Path, cfg: dict | None = None) -> tuple[list[PatentRecord], dict]:
    """CSV を読み、PatentRecord の一覧と読み込みの記録（重複・空文書の件数など）を返す。

    cfg（任意）: {"sections": {キー: {column, heading}}, "normalize": "NFKC"}
    """
    cfg = cfg or {}
    sections = cfg.get("sections") or DEFAULT_SECTIONS
    normalize = cfg.get("normalize", "NFKC")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "doc_id" not in df:
        raise ValueError(f"{path} に doc_id 列がありません")
    missing_cols = [v["column"] for v in sections.values() if v["column"] not in df]
    info: dict = {"source": str(path), "n_rows": int(len(df)), "missing_columns": missing_cols}
    n_dup = int(df.doc_id.duplicated().sum())
    df = df[~df.doc_id.duplicated()]
    info["duplicate_doc_id_dropped"] = n_dup
    recs, empty = [], []
    for r in df.to_dict("records"):
        secs = {k: clean_text(r.get(v["column"], ""), normalize) for k, v in sections.items()}
        meta = {c: r.get(c, "") for c in META_COLUMNS if c in r}
        sub, mg, code = first_class(r.get("ipc", ""))
        meta.update({"ipc_subclass": sub, "ipc_main_group": mg, "ipc_first": code})
        fsub, fmg, fcode = first_class(r.get("fi", ""))
        meta.update({"fi_main_group": fmg, "fi_first": fcode})
        rec = PatentRecord(r["doc_id"], secs, {k: v["heading"] for k, v in sections.items()}, meta)
        rec.build_text()
        if not rec.text:
            empty.append(rec.doc_id)
            continue
        recs.append(rec)
    info["empty_dropped"] = empty
    info["n_docs"] = len(recs)
    info["empty_sections"] = {k: sum(1 for x in recs if not x.sections[k]) for k in sections}
    info["duplicate_text"] = len(recs) - len({x.text for x in recs})
    return recs, info


def to_documents(recs: list[PatentRecord]) -> list[Document]:
    """schema.Document に変換する。tech_domain は筆頭 IPC のサブクラス（ビューアの記号分け用）。"""
    return [Document(r.doc_id, "real", r.meta.get("ipc_subclass", "") or "不明", r.text, len(r.text), "") for r in recs]


def sections_frame(recs: list[PatentRecord]) -> pd.DataFrame:
    """data/sections.csv の中身（段落・書誌・筆頭分類）。"""
    rows = []
    for r in recs:
        row = {"doc_id": r.doc_id}
        row.update({k: r.meta.get(k, "") for k in ("pub_number", "title", "applicants", "ipc", "fi", "ipc_first", "ipc_main_group", "fi_first", "fi_main_group", "problem_source", "solution_source", "use_source")})
        row.update(r.sections)
        rows.append(row)
    return pd.DataFrame(rows)
