"""分野ごとの部品（モジュール）を組み合わせたオントロジーと、文書の分類で部品を切り替える抽出。

考え方:
- 上層（upper）: 上位型・述語と、分野をまたいで使う概念（コスト、安全性、耐久性など）。
- 部品（module）: 分野ごとの概念と別名。親は上層の概念か、同じ部品の概念。
- 照合: 文書の IPC・FI を見て使う部品を決め、上層と使う部品の表記だけで最長一致する。
- 別名の分野制限（alias_scopes）: 上層の別名のうち、分野で意味が変わるものは使う部品を限る
  （例: 「容量」を放電容量として使うのは無機材料・電池の文書だけ。半導体では静電容量の意味で当たるため）。
  一枚の辞書で全分野の表記を照合すると、分野で意味が変わる語や、長い語の一部への当たりが増えるため。
- 類似度は全部品を合わせたオントロジーで測る（概念 ID は部品をまたいで重ならない）。
- 細分化: 部品の概念は、上層の概念の子（子孫）として置けば、上層と同じ表記を持ってよい。照合では部品の概念が先に当たる。
  例: 電池の部品の「内部抵抗」（親は上層の「出力特性」）。電池の文書では細かい概念で拾い、親を通じて他分野の文書とも似る。

部品の YAML は通常のオントロジーと同じ形（concepts: [{id, label, aliases, parents}]）。
top_types と predicates は上層だけが持つ。
"""
from __future__ import annotations

import copy
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .ontology import Ontology

UPPER = "upper"


@dataclass
class ModularOntology:
    """上層と部品を合わせたオントロジーと、概念 → 部品の対応、部品の振り分け規則。"""

    onto: Ontology
    module_of: dict[str, str]
    routes: dict[str, list[str]] = field(default_factory=dict)  # 部品 → IPC・FI の前方一致の一覧
    fallback: str = "all"  # どの規則にも当たらない文書: all = 全部品 / upper = 上層だけ
    alias_scopes: dict[tuple[str, str], set[str]] = field(default_factory=dict)  # (正規化した表記, 概念) → 使ってよい部品
    data: dict = field(default_factory=dict)  # 組み立てたオントロジーの dict（拡張層を重ねるときに使う）

    def allowed(self, surface: str, cid: str, active: set[str]) -> bool:
        """この文書（使う部品 active）で、表記 surface を概念 cid として照合してよいか。"""
        if self.module_of.get(cid, UPPER) not in active | {UPPER}:
            return False
        sc = self.alias_scopes.get((_key(surface), cid))
        return sc is None or bool(sc & active)

    def modules_for(self, codes: str) -> set[str]:
        """文書の分類（IPC・FI を ; で並べた文字列）から使う部品を決める。"""
        cs = [c.strip().replace(" ", "") for c in (codes or "").replace(",", ";").split(";") if c.strip()]
        hit = {m for m, prefixes in self.routes.items() if any(c.startswith(p) for c in cs for p in prefixes)}
        if hit:
            return hit
        return set(self.routes) if self.fallback == "all" else set()


def _key(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower()


def compose(upper: dict, modules: dict[str, dict], routes: dict[str, list[str]] | None = None, fallback: str = "all",
            alias_scopes: list[dict] | None = None) -> ModularOntology:
    """上層と部品の YAML（dict）を 1 つのオントロジーにまとめる。ID が重なれば止める。"""
    data = copy.deepcopy(upper)
    module_of = {c["id"]: UPPER for c in data["concepts"]}
    module_of.update({t["id"]: UPPER for t in data["top_types"]})
    mod_concepts = []
    for name, mod in modules.items():
        for c in mod.get("concepts", []):
            if c["id"] in module_of:
                raise ValueError(f"概念 ID が重なっています: {c['id']}（{module_of[c['id']]} と {name}）")
            module_of[c["id"]] = name
            mod_concepts.append(copy.deepcopy(c))
    # 部品の概念を先に並べる。同じ表記が上層と部品の両方にあるとき（部品が上層の概念を細分化したとき）、
    # 照合の表記の並び（長さが同じなら並び順）で部品の概念が先に当たる
    data["concepts"] = mod_concepts + data["concepts"]
    scopes = {(_key(r["alias"]), r["concept"]): set(r["modules"]) for r in (alias_scopes or [])}
    return ModularOntology(Ontology(data), module_of, dict(routes or {}), fallback, scopes, data)


def load_dir(d: Path) -> ModularOntology:
    """部品の設定フォルダ（upper.yaml・部品の *.yaml・routes.yaml・alias_scopes.yaml）から組み立てる。"""
    d = Path(d)
    rt = yaml.safe_load((d / "routes.yaml").read_text(encoding="utf-8"))
    mods = {p.stem: p for p in sorted(d.glob("*.yaml")) if p.stem not in ("upper", "routes", "alias_scopes")}
    return load(d / "upper.yaml", mods, rt["routes"], rt.get("fallback", "all"), d / "alias_scopes.yaml")


def with_data(mo: ModularOntology, data: dict) -> ModularOntology:
    """同じ部品の設定のまま、オントロジーの中身だけ差し替える（拡張層を重ねたとき）。
    拡張層で足した概念は上層の扱い（どの分野の文書でも使う）。"""
    module_of = dict(mo.module_of)
    for c in data["concepts"]:
        module_of.setdefault(c["id"], UPPER)
    return ModularOntology(Ontology(data), module_of, mo.routes, mo.fallback, mo.alias_scopes, data)


def load(upper_path: Path, module_paths: dict[str, Path], routes: dict[str, list[str]] | None = None, fallback: str = "all",
         alias_scope_path: Path | None = None) -> ModularOntology:
    up = yaml.safe_load(Path(upper_path).read_text(encoding="utf-8"))
    mods = {k: yaml.safe_load(Path(p).read_text(encoding="utf-8")) for k, p in module_paths.items()}
    scopes = yaml.safe_load(Path(alias_scope_path).read_text(encoding="utf-8"))["alias_scopes"] if alias_scope_path and Path(alias_scope_path).exists() else None
    return compose(up, mods, routes, fallback, scopes)


def build_section_layer_scoped(recs, mo: ModularOntology, cfg: dict | None = None, name: str = "dict", scoped: bool = True):
    """部品を切り替えて抽出する（scoped=False なら全部品で照合＝一枚の辞書と同じ）。

    返り値は build_section_layer と同じ (GraphLayer, stats)。stats に部品の使われ方を足す。
    """
    from .patents.extract import SectionExtractor
    from .schema import GraphLayer

    base = SectionExtractor(mo.onto, cfg)
    all_forms = base.forms
    groups: dict[frozenset, list] = defaultdict(list)
    for r in recs:
        act = mo.modules_for(";".join([r.meta.get("ipc", ""), r.meta.get("fi", "")])) if scoped else set(mo.routes) or {m for m in mo.module_of.values()}
        groups[frozenset(act)].append(r)
    triples, concepts = {}, {}
    route_count: Counter = Counter()
    roles: Counter = Counter()
    trip_stats: Counter = Counter()
    capped = 0
    for act, rs in groups.items():
        ex = SectionExtractor(mo.onto, cfg)
        ex.forms = [(s, c) for s, c in all_forms if mo.allowed(s, c, set(act))] if scoped else list(all_forms)
        for r in rs:
            t, c = ex.extract(r)
            triples[r.doc_id] = t
            concepts[r.doc_id] = c
        route_count["+".join(sorted(act)) or "(上層だけ)"] += len(rs)
        roles.update(ex.stats_roles)
        trip_stats.update(ex.stats_triples)
        capped += ex.n_capped
    stats = {
        "scoped": scoped,
        "routes": dict(route_count),
        "roles": [{"section": s, "top_type": tp, "decision": k, "count": v} for (s, tp, k), v in sorted(roles.items())],
        "triples": [{"predicate": p, "proximity": x, "negated": n, "count": v} for (p, x, n), v in sorted(trip_stats.items())],
        "docs_capped": capped,
        "no_concepts": [d for d in concepts if not concepts[d]],
        "no_triples": [d for d in triples if not triples[d]],
    }
    return GraphLayer(name, triples, concepts), stats
