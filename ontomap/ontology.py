"""オントロジー v0.x の読み込みと、概念・述語の類似度。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml


@dataclass
class Concept:
    """オントロジーの概念ノード。"""

    concept_id: str
    preferred_label: str
    aliases: list[str] = field(default_factory=list)
    parent_ids: list[str] = field(default_factory=list)
    top_type: str = ""


@dataclass
class Predicate:
    """関係の型（述語）。"""

    predicate_id: str
    label: str
    group: str
    polarity: int


class Ontology:
    """概念の階層・別名・述語を持ち、類似度を返す。"""

    def __init__(self, data: dict) -> None:
        self.version: str = str(data.get("version", "0.1"))
        self.top_types: dict[str, str] = {t["id"]: t["label"] for t in data["top_types"]}
        self.predicates: dict[str, Predicate] = {
            p["id"]: Predicate(p["id"], p["label"], p["group"], int(p["polarity"]))
            for p in data["predicates"]
        }
        self.concepts: dict[str, Concept] = {}
        for tid, label in self.top_types.items():
            self.concepts[tid] = Concept(tid, label, [], [], tid)
        for c in data["concepts"]:
            self.concepts[c["id"]] = Concept(
                c["id"], c["label"], list(c.get("aliases", [])), list(c.get("parents", []))
            )
        for cid in self.concepts:
            self.concepts[cid].top_type = self._resolve_top(cid)
        self._ancestors: dict[str, dict[str, int]] = {cid: self._ancestor_depths(cid) for cid in self.concepts}
        self._depth: dict[str, int] = {cid: self._min_depth(cid) for cid in self.concepts}
        self.concept_ids: list[str] = sorted(self.concepts)
        self.index: dict[str, int] = {cid: i for i, cid in enumerate(self.concept_ids)}
        self.predicate_ids: list[str] = sorted(self.predicates)
        self.predicate_index: dict[str, int] = {pid: i for i, pid in enumerate(self.predicate_ids)}
        self._sim_matrix: np.ndarray | None = None
        self._pred_matrix: np.ndarray | None = None

    # ---------- 読み書き ----------
    @classmethod
    def load(cls, path: str | Path) -> "Ontology":
        """YAML または JSON から読む。"""
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        if "concepts" in data and data["concepts"] and "concept_id" in data["concepts"][0]:
            data = cls._from_export(data)
        return cls(data)

    @staticmethod
    def _from_export(data: dict) -> dict:
        concepts = [
            {"id": c["concept_id"], "label": c["preferred_label"], "aliases": c["aliases"], "parents": c["parent_ids"]}
            for c in data["concepts"]
            if c["concept_id"] not in {t["id"] for t in data["top_types"]}
        ]
        return {"version": data["version"], "top_types": data["top_types"], "predicates": data["relation_types"], "concepts": concepts}

    def export_json(self, path: str | Path) -> None:
        """計画書の ontology.json 形式で書き出す。"""
        out = {
            "version": self.version,
            "top_types": [{"id": k, "label": v} for k, v in self.top_types.items()],
            "relation_types": [
                {"id": p.predicate_id, "label": p.label, "group": p.group, "polarity": p.polarity}
                for p in self.predicates.values()
            ],
            "concepts": [
                {
                    "concept_id": c.concept_id,
                    "preferred_label": c.preferred_label,
                    "aliases": c.aliases,
                    "parent_ids": c.parent_ids,
                    "top_type": c.top_type,
                    "relation_types": sorted(self.predicates),
                }
                for c in self.concepts.values()
            ],
        }
        Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 階層 ----------
    def _resolve_top(self, cid: str) -> str:
        seen: set[str] = set()
        stack = [cid]
        while stack:
            cur = stack.pop()
            if cur in self.top_types:
                return cur
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.concepts[cur].parent_ids)
        raise ValueError(f"上位型に届かない概念: {cid}")

    def _ancestor_depths(self, cid: str) -> dict[str, int]:
        """自分を含む祖先 → 上からの最小深さ（上位型=1）。"""
        out: dict[str, int] = {}
        stack = [cid]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out[cur] = 0
            stack.extend(self.concepts[cur].parent_ids)
        return out

    def _min_depth(self, cid: str) -> int:
        if cid in self.top_types:
            return 1
        return 1 + min(self._min_depth(p) for p in self.concepts[cid].parent_ids)

    def top_type(self, cid: str) -> str:
        """概念の上位型（T/P/M/A/E）。"""
        return self.concepts[cid].top_type

    def label(self, cid: str) -> str:
        """表示名。"""
        c = self.concepts.get(cid)
        return c.preferred_label if c else cid

    def is_known(self, cid: str) -> bool:
        """オントロジーに載っている概念か。"""
        return cid in self.concepts

    # ---------- 類似度 ----------
    def concept_sim(self, a: str, b: str) -> float:
        """同義（同一 ID）=1。上位型以外の共通祖先があれば Wu-Palmer 型の値。上位型だけの共有は 0。"""
        if a == b:
            return 1.0
        if a not in self.concepts or b not in self.concepts:
            return 0.0
        anc_a = self._ancestors[a]
        anc_b = self._ancestors[b]
        best = 0.0
        for c in set(anc_a) & set(anc_b):
            if c in self.top_types:
                continue
            best = max(best, 2.0 * self._depth[c] / (self._depth[a] + self._depth[b]))
        return best

    def predicate_sim(self, r1: str, r2: str) -> float:
        """同じ述語=1、同じ群=0.7、極性が逆=0、その他=0.2。"""
        if r1 == r2:
            return 1.0
        p1, p2 = self.predicates.get(r1), self.predicates.get(r2)
        if p1 is None or p2 is None:
            return 0.0
        if p1.polarity * p2.polarity < 0:
            return 0.0
        if p1.group == p2.group:
            return 0.7
        return 0.2

    def concept_sim_matrix(self) -> np.ndarray:
        """全概念 × 全概念の類似度行列（concept_ids の順）。"""
        if self._sim_matrix is None:
            n = len(self.concept_ids)
            m = np.zeros((n, n), dtype=np.float32)
            for i, a in enumerate(self.concept_ids):
                for j in range(i, n):
                    v = self.concept_sim(a, self.concept_ids[j])
                    m[i, j] = m[j, i] = v
            self._sim_matrix = m
        return self._sim_matrix

    def predicate_sim_matrix(self) -> np.ndarray:
        """述語 × 述語の類似度行列（predicate_ids の順）。"""
        if self._pred_matrix is None:
            n = len(self.predicate_ids)
            m = np.zeros((n, n), dtype=np.float32)
            for i, a in enumerate(self.predicate_ids):
                for j, b in enumerate(self.predicate_ids):
                    m[i, j] = self.predicate_sim(a, b)
            self._pred_matrix = m
        return self._pred_matrix

    def surface_forms(self) -> list[tuple[str, str]]:
        """(表記, 概念ID) を長い順に。中間概念・上位型は本文に出ない前提で除く。"""
        forms: list[tuple[str, str]] = []
        for c in self.concepts.values():
            if not c.aliases and c.concept_id not in self._leaf_ids():
                continue
            for s in [c.preferred_label, *c.aliases]:
                forms.append((s, c.concept_id))
        forms.sort(key=lambda x: (-len(x[0]), x[0]))
        return forms

    @lru_cache(maxsize=1)
    def _leaf_ids(self) -> frozenset[str]:
        parents = {p for c in self.concepts.values() for p in c.parent_ids}
        return frozenset(cid for cid in self.concepts if cid not in parents and cid not in self.top_types)
