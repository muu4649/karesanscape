"""計画書 12 章のデータスキーマ（documents.csv / triples.jsonl ほか）の読み書き。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

ROLES = ("対象", "課題", "手段", "作用", "効果", "用途")
# 上位型 ID → 文書内の役割名。U（用途）は特許の「用途記載」を扱うときに使う。
TOP_TO_ROLE = {"T": "対象", "P": "課題", "M": "手段", "A": "作用", "E": "効果", "U": "用途"}


@dataclass
class Document:
    """1 文書。source_type は real / paraphrase / inverted。"""

    doc_id: str
    source_type: str
    tech_domain: str
    text: str
    char_count: int
    original_id: str = ""


@dataclass
class Triple:
    """型付き三つ組。role は「始点役割>終点役割」。"""

    doc_id: str
    subject_id: str
    predicate: str
    object_id: str
    subject_role: str
    object_role: str
    confidence: float = 1.0
    negated: bool = False
    speculative: bool = False
    evidence_span: list[int] = field(default_factory=lambda: [0, 0])
    evidence_text: str = ""
    subject_text: str = ""
    object_text: str = ""

    @property
    def role(self) -> str:
        """計画書の role フィールド。"""
        return f"{self.subject_role}>{self.object_role}"

    def key(self) -> tuple[str, str, str]:
        """抽出評価で使う一致キー。"""
        return (self.subject_id, self.predicate, self.object_id)

    def to_json(self) -> dict:
        d = asdict(self)
        d["role"] = self.role
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Triple":
        d = dict(d)
        d.pop("role", None)
        return cls(**d)


@dataclass
class ConceptMention:
    """文書内の概念の出現。"""

    doc_id: str
    concept_id: str
    start: int
    end: int
    surface: str


@dataclass
class GraphLayer:
    """1 つのグラフ層（gold / dict / llm など）。"""

    name: str
    triples: dict[str, list[Triple]]
    concepts: dict[str, list[ConceptMention]]

    def save(self, root: Path) -> None:
        """graphs/<name>/triples.jsonl と concepts.jsonl に書く。"""
        d = root / self.name
        d.mkdir(parents=True, exist_ok=True)
        with (d / "triples.jsonl").open("w", encoding="utf-8") as f:
            for doc_id in sorted(self.triples):
                for t in self.triples[doc_id]:
                    f.write(json.dumps(t.to_json(), ensure_ascii=False) + "\n")
        with (d / "concepts.jsonl").open("w", encoding="utf-8") as f:
            for doc_id in sorted(self.concepts):
                for m in self.concepts[doc_id]:
                    f.write(json.dumps(asdict(m), ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, root: Path, name: str, doc_ids: list[str]) -> "GraphLayer":
        """保存済みの層を読む。三つ組が無い文書も空リストで持つ。"""
        d = root / name
        triples: dict[str, list[Triple]] = {i: [] for i in doc_ids}
        concepts: dict[str, list[ConceptMention]] = {i: [] for i in doc_ids}
        for line in (d / "triples.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = Triple.from_json(json.loads(line))
                triples.setdefault(t.doc_id, []).append(t)
        cpath = d / "concepts.jsonl"
        if cpath.exists():
            for line in cpath.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    m = ConceptMention(**json.loads(line))
                    concepts.setdefault(m.doc_id, []).append(m)
        return cls(name, triples, concepts)


def save_documents(docs: list[Document], path: Path) -> None:
    """documents.csv を書く。"""
    pd.DataFrame([asdict(d) for d in docs]).to_csv(path, index=False)


def load_documents(path: Path) -> list[Document]:
    """documents.csv を読む。必須列: doc_id, text。"""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "doc_id" not in df or "text" not in df:
        raise ValueError("documents.csv には doc_id と text の列が必要です")
    out = []
    for r in df.to_dict("records"):
        out.append(
            Document(
                doc_id=r["doc_id"],
                source_type=r.get("source_type", "real") or "real",
                tech_domain=r.get("tech_domain", "") or "",
                text=r["text"],
                char_count=len(r["text"]),
                original_id=r.get("original_id", "") or "",
            )
        )
    return out
