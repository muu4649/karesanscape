"""技術オントロジーの部品（モジュール）を作る・検査する。

使い方（ontomap/ で実行）:
    .venv/bin/python scripts/build_module.py split                 # 3 分野の開発用・確認用の分け方を表示
    .venv/bin/python scripts/build_module.py split_inorganic       # inorg-1.0 を上層と部品 inorganic に分ける
    .venv/bin/python scripts/build_module.py candidates battery    # 開発用から、今の辞書で拾えない語を数える
    .venv/bin/python scripts/build_module.py build battery         # config/modules/battery.tree → config/modules/battery.yaml
    .venv/bin/python scripts/build_module.py validate              # 全部品を組み、分野別・切り替えあり/なしで測る

決まり:
- 部品の木は config/modules/<部品>.tree に書く（scripts/build_ontology_inorganic.py の TREE と同じ書き方）。
  1 行 = 1 概念「ID: 表示名 | 別名, 別名」。字下げ 2 文字で 1 段下。最上段は親の ID（上層の概念か上位型 P/M/U）。
- 部品の概念 ID は部品の頭文字で始める（battery = BAT_、semicon = SEM_、inorganic = 既存の P_/M_/U_ のまま）。
- 開発用・確認用は doc_id の番号の奇数・偶数（無機材料と同じ）。候補語と取りこぼしの一覧は開発用だけで作る。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ontomap import modular  # noqa: E402
from ontomap.app import layers  # noqa: E402

MOD_DIR = ROOT / "config/modules"
CORPORA = {  # 部品 → コーパス
    "inorganic": ROOT / "datasets/inorganic1200/documents_sections.csv",
    "battery": ROOT / "datasets/battery600/documents_sections.csv",
    "semicon": ROOT / "datasets/semicon600/documents_sections.csv",
}
ROUTES = yaml.safe_load((ROOT / "config/modules/routes.yaml").read_text(encoding="utf-8"))["routes"]  # 部品 → 使う文書の分類（IPC・FI の前方一致）
PREFIX = {"battery": "BAT_", "semicon": "SEM_"}
# inorg-1.0 の中間概念のうち、上層（分野をまたいで使う層）に移すもの。子もいっしょに移す。ID は変えない
# （無機材料の文書から見た「上層＋無機材料の部品」は inorg-1.0 と同じ）。2026-10-20、電池の開発用での出現率を見て決めた
UPPER_MIDS = ["P_electrochem", "P_stability", "P_mechanical", "P_structure_quality", "P_process", "P_functional_property", "P_env_safety", "P_other",
              "M_metal_component", "M_oxide_material", "M_carbon_material", "M_battery_material", "M_organic_component", "M_structure_spec",
              "M_synthesis", "M_thermal_treatment", "M_post_treatment", "M_loading", "M_forming",
              "U_energy_device", "U_application_platform", "U_products"]


def load_corpus(name: str) -> pd.DataFrame:
    d = pd.read_csv(CORPORA[name], dtype=str, keep_default_na=False)
    num = d["doc_id"].str.extract(r"(\d+)")[0].astype(int)
    d["split"] = ["dev" if n % 2 else "check" for n in num]
    d["corpus"] = name
    return d


def module_paths(exclude: str | None = None) -> dict[str, Path]:
    return {p.stem: p for p in sorted(MOD_DIR.glob("*.yaml")) if p.stem != "upper" and p.stem != exclude}


def compose_all(exclude: str | None = None, fallback: str = "all") -> modular.ModularOntology:
    return modular.load(MOD_DIR / "upper.yaml", module_paths(exclude), {k: v for k, v in ROUTES.items() if k != exclude}, fallback,
                        MOD_DIR / "alias_scopes.yaml")


def cmd_split_inorganic() -> None:
    """config/ontology_inorganic_v1.yaml を、上層（upper.yaml）と部品 inorganic（inorganic.yaml）に分ける。"""
    b = yaml.safe_load((ROOT / "config/ontology_inorganic_v1.yaml").read_text(encoding="utf-8"))
    cs = b["concepts"]
    kids: dict[str, list[str]] = collections.defaultdict(list)
    for c in cs:
        for p in c["parents"]:
            kids[p].append(c["id"])
    move: set[str] = set()
    stack = list(UPPER_MIDS)
    while stack:
        x = stack.pop()
        if x not in move:
            move.add(x)
            stack.extend(kids.get(x, []))
    up = {"version": "tech-pilot-0.1", "top_types": b["top_types"], "predicates": b["predicates"], "concepts": [c for c in cs if c["id"] in move]}
    inorg = {"module": "inorganic", "concepts": [c for c in cs if c["id"] not in move]}
    (MOD_DIR / "upper.yaml").write_text("# 技術オントロジー試作の上層（上位型・述語と、分野をまたいで使う概念）。inorg-1.0 から移した。scripts/build_module.py split_inorganic\n"
                                        + yaml.safe_dump(up, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (MOD_DIR / "inorganic.yaml").write_text("# 部品「inorganic」= inorg-1.0 のうち上層に移さなかった概念。scripts/build_module.py split_inorganic\n"
                                            + yaml.safe_dump(inorg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"上層 {len(up['concepts'])} 概念、inorganic {len(inorg['concepts'])} 概念（合計 {len(cs)}）")


def cmd_split() -> None:
    for name, p in CORPORA.items():
        if p.exists():
            d = load_corpus(name)
            print(name, len(d), d["split"].value_counts().to_dict())
        else:
            print(name, "（まだ無い）")


def cmd_candidates(name: str) -> None:
    """開発用の文書から、今の辞書（上層＋全部品）で拾えない、よく出る語を段落ごとに数える。"""
    d = load_corpus(name)
    dev = d[d["split"] == "dev"].reset_index(drop=True)
    mo = compose_all()
    cands = layers.candidates(dev, mo.onto, set(), top_n=250, min_df=3)
    out = ROOT / f"datasets/{CORPORA[name].parent.name}/_work/ontology_build"
    out.mkdir(parents=True, exist_ok=True)
    rows = [{"section": c["section_label"], "term": c["term"], "df": c["df"], "share": c["share"], "score": c["score"],
             "example": (c["examples"][0]["text"] if c["examples"] else "")} for c in cands]
    pd.DataFrame(rows).to_csv(out / "candidates_dev.csv", index=False)
    # いまの辞書で拾える概念の出現（どの部品の概念か）も残す。分野をまたぐ概念を上層に移すかの判断に使う
    m = layers.Matcher(mo.onto)
    cnt: collections.Counter = collections.Counter()
    for _, r in dev.iterrows():
        seen = set()
        for col in ("problem", "solution", "use_text"):
            seen |= {c for _, _, _, c in m.find(layers._norm(r[col]))}
        cnt.update(seen)
    known = [{"concept": c, "label": mo.onto.label(c), "module": mo.module_of.get(c), "top": mo.onto.top_type(c), "df": n, "rate": round(n / len(dev), 3)}
             for c, n in cnt.most_common()]
    pd.DataFrame(known).to_csv(out / "known_concepts_dev.csv", index=False)
    print(f"{out}/candidates_dev.csv: {len(rows)} 語、known_concepts_dev.csv: {len(known)} 概念（開発用 {len(dev)} 件）")


def parse_tree(text: str) -> list[dict]:
    """木の文字列を概念の一覧にする。最上段の行は親の ID だけ（上層の概念か上位型）。"""
    out: list[dict] = []
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        depth = (len(raw) - len(raw.lstrip(" "))) // 2
        line = raw.strip()
        head, _, alias_part = line.partition("|")
        cid, _, label = head.partition(":")
        cid, label = cid.strip(), label.strip()
        aliases = [a.strip() for a in alias_part.split(",") if a.strip()] if alias_part else []
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if depth == 0:
            stack.append((0, cid))  # 親（上層の概念または上位型）
            continue
        out.append({"id": cid, "label": label, "aliases": aliases, "parents": [stack[-1][1]]})
        stack.append((depth, cid))
    return out


def cmd_build(name: str) -> None:
    from build_ontology_inorganic import expand_aliases

    tree = (MOD_DIR / f"{name}.tree").read_text(encoding="utf-8")
    cs = parse_tree(tree)
    pre = PREFIX.get(name)
    up = yaml.safe_load((MOD_DIR / "upper.yaml").read_text(encoding="utf-8"))
    up_ids = {c["id"] for c in up["concepts"]} | {t["id"] for t in up["top_types"]}
    ids = [c["id"] for c in cs]
    dup = [x for x, n in collections.Counter(ids).items() if n > 1]
    if dup:
        raise SystemExit(f"ID の重複: {dup}")
    for c in cs:
        if pre and not c["id"].startswith(pre):
            raise SystemExit(f"ID は {pre} で始める: {c['id']}")
        if c["parents"][0] not in up_ids and c["parents"][0] not in ids:
            raise SystemExit(f"親が見つからない: {c['id']} → {c['parents'][0]}")
        c["aliases"] = expand_aliases(c["label"], c["aliases"])
    # 同じ表記が 2 つの概念に入っていないか。部品の中では不可。上層とは、上層の概念の子孫（細分化）なら可
    parent = {c["id"]: c["parents"][0] for c in up["concepts"]} | {c["id"]: c["parents"][0] for c in cs}

    def ancestors(x: str) -> set[str]:
        out = set()
        while x in parent:
            x = parent[x]
            out.add(x)
        return out

    sc_path = MOD_DIR / "alias_scopes.yaml"
    scoped = {(unicodedata.normalize("NFKC", r["alias"]).lower(), r["concept"]): set(r["modules"])
              for r in (yaml.safe_load(sc_path.read_text(encoding="utf-8"))["alias_scopes"] if sc_path.exists() else [])}
    up_owner: dict[str, str] = {}
    for c in up["concepts"]:
        for s in [c["label"], *c.get("aliases", [])]:
            up_owner[unicodedata.normalize("NFKC", s).lower()] = c["id"]
    owner: dict[str, str] = {}
    clashes, refined = [], []
    mids = {p for c in cs for p in c["parents"]}
    for c in cs:
        forms = ([c["label"]] if c["id"] not in mids or c["aliases"] else []) + c["aliases"]
        for s in forms:
            n = unicodedata.normalize("NFKC", s).lower()
            if n in owner and owner[n] != c["id"]:
                clashes.append(f"{s}: {owner[n]} / {c['id']}（部品の中）")
            owner[n] = c["id"]
            if n in up_owner and name not in scoped.get((n, up_owner[n]), {name}):
                pass  # 上層側の別名はこの分野の文書では使わない（alias_scopes.yaml）ので重ならない
            elif n in up_owner:
                if up_owner[n] in ancestors(c["id"]):
                    refined.append(f"{s}（{up_owner[n]} → {c['id']}）")
                else:
                    clashes.append(f"{s}: 上層 {up_owner[n]} / {c['id']}（親子でない）")
    if clashes:
        raise SystemExit("同じ表記が 2 つの概念にある: " + "; ".join(clashes))
    if refined:
        print(f"細分化 {len(refined)} 表記: " + "、".join(refined[:12]) + (" ほか" if len(refined) > 12 else ""))
    n_leaf = sum(1 for c in cs if c["id"] not in mids)
    head = (f"# 技術オントロジー試作の部品「{name}」。木は config/modules/{name}.tree、作り方は scripts/build_module.py。\n"
            f"# 概念 {len(cs)}（中間 {len(cs) - n_leaf}・末端 {n_leaf}）、別名 {sum(len(c['aliases']) for c in cs)}。\n")
    (MOD_DIR / f"{name}.yaml").write_text(head + yaml.safe_dump({"module": name, "concepts": cs}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"config/modules/{name}.yaml: 概念 {len(cs)}（末端 {n_leaf}）")


def section_hits(df: pd.DataFrame, mo: modular.ModularOntology, scoped: bool) -> list[dict]:
    """文書ごと・段落ごとの拾えた概念（段落の型の概念だけ）。scoped なら文書の分類で部品を切り替える。"""
    cache: dict[frozenset, layers.Matcher] = {}
    out = []
    for _, r in df.iterrows():
        act = mo.modules_for(";".join([r.get("ipc", ""), r.get("fi", "")])) if scoped else set(mo.routes)
        key = frozenset(act)
        if key not in cache:
            m = layers.Matcher(mo.onto)
            if scoped:
                m.index = {k: [(s, c) for s, c in v if mo.allowed(s, c, set(act))] for k, v in m.index.items()}
            cache[key] = m
        row = {"doc_id": r["doc_id"], "corpus": r["corpus"], "split": r["split"], "modules": "+".join(sorted(act))}
        for col, top in layers.SECTION_TOP.items():
            t = layers._norm(r.get(col, ""))
            row[col] = None if not t.strip() else [(s, c) for _, _, s, c in cache[key].find(t) if mo.onto.top_type(c) == top]
        out.append(row)
    return out


def cmd_validate() -> None:
    """3 つの照合のしかたを、分野ごと・開発用/確認用ごとに比べる。
    A = inorg-1.0 だけ（試作の前）、B = 全部品を合わせた一枚の辞書、C = 分野で部品を切り替え、別名も分野で限る。
    指標: 段落の型の概念が 1 つも拾えない段落の割合（取りこぼし）、拾った概念のうち別の分野の部品の概念の割合、
    文書あたりの拾った概念の種類数。"""
    from ontomap.ontology import Ontology

    mo = compose_all()
    inorg = Ontology.load(ROOT / "config/ontology_inorganic_v1.yaml")
    docs = pd.concat([load_corpus(n) for n, p in CORPORA.items() if p.exists()], ignore_index=True)
    own = {"inorganic": "inorganic", "battery": "battery", "semicon": "semicon"}
    plain_a = layers.Matcher(inorg)
    rows, foreign_rows = [], []
    for cfg in ("A", "B", "C"):
        if cfg == "A":
            hits = []
            for _, r in docs.iterrows():
                h = {"doc_id": r["doc_id"], "corpus": r["corpus"], "split": r["split"]}
                for col, top in layers.SECTION_TOP.items():
                    t = layers._norm(r.get(col, ""))
                    h[col] = None if not t.strip() else [(s_, c) for _, _, s_, c in plain_a.find(t) if inorg.top_type(c) == top]
                hits.append(h)
        else:
            hits = section_hits(docs, mo, scoped=(cfg == "C"))
        for corpus in docs["corpus"].unique():
            for split in ("dev", "check"):
                hs = [h for h in hits if h["corpus"] == corpus and h["split"] == split]
                row = {"照合": cfg, "コーパス": corpus, "分け方": split, "文書": len(hs)}
                kinds = []
                for col in layers.SECTION_TOP:
                    secs = [h[col] for h in hs if h[col] is not None]
                    row[f"{col}_取りこぼし"] = round(sum(1 for x in secs if not x) / max(1, len(secs)), 3)
                    if cfg != "A":
                        allm = [(s_, c) for sec in secs for s_, c in sec]
                        foreign = [(s_, c) for s_, c in allm if mo.module_of.get(c) not in (modular.UPPER, own[corpus])]
                        row[f"{col}_他分野の概念"] = round(len(foreign) / max(1, len(allm)), 3)
                        if split == "check":
                            for (s_, c), n in collections.Counter(foreign).most_common(10):
                                foreign_rows.append({"照合": cfg, "コーパス": corpus, "段落": col, "表記": s_, "概念": mo.onto.label(c), "部品": mo.module_of.get(c), "回数": n})
                for h in hs:
                    kinds.append(len({c for col in layers.SECTION_TOP for (_, c) in (h[col] or [])}))
                row["文書あたり概念数"] = round(sum(kinds) / max(1, len(kinds)), 1)
                rows.append(row)
    out = ROOT / "runs/tech_pilot"
    out.mkdir(parents=True, exist_ok=True)
    res = pd.DataFrame(rows)
    res.to_csv(out / "validate.csv", index=False)
    pd.DataFrame(foreign_rows).to_csv(out / "foreign_examples.csv", index=False)
    pd.set_option("display.width", 250)
    print(res[res["分け方"] == "check"].to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["split", "split_inorganic", "candidates", "build", "validate"])
    ap.add_argument("name", nargs="?")
    a = ap.parse_args()
    {"split": lambda: cmd_split(), "split_inorganic": lambda: cmd_split_inorganic(), "candidates": lambda: cmd_candidates(a.name), "build": lambda: cmd_build(a.name),
     "validate": lambda: cmd_validate()}[a.cmd]()


if __name__ == "__main__":
    main()
