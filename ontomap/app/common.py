"""使用者が育てる共通層（辞書）。

保存先: ONTOMAP_HOME/common/<cid>/
- meta.json      名前・元にした共通層・作成日・発行した版の一覧
- draft/         下書き（upper.yaml、部品の <名前>.yaml、routes.yaml、alias_scopes.yaml）
- v<n>/          発行した版の控え（変えない）。マップは版に固定する（ontology ID = "common:<cid>@v<n>"）
- changelog.jsonl  変更の記録

形式は config/modules/ と同じ（ontomap/modular.py の load_dir で読める）。部品の無い共通層は upper.yaml だけを持つ。
同梱の共通層（inorganic-1.0・mof-0.1・tech-0.1）は読み取り専用で、複製してから直す。

育て方:
1. 格上げ: この共通層を使うマップの拡張層で採用された語を集め、共通層（上層か部品）へ入れる。
2. 編集: 概念の追加・変更・削除、別名の分野制限、部品の振り分け規則。
3. 新しい分野: 部品を足し、マップの文書から共通層で拾えない語を候補にして入れる。
発行の前に検査（check）を通す。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from .. import modular
from ..ontology import Ontology

TOP_LABEL = {"P": "課題", "M": "解決手段", "U": "用途"}
SPECIAL = ("upper", "routes", "alias_scopes")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def _key(s: str) -> str:
    return _norm(s).lower()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def root() -> Path:
    from .engine import home

    p = home() / "common"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cdir(cid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{10}", cid or ""):
        raise ValueError("共通辞書の ID が不正です")
    return root() / cid


def _read_yaml(p: Path, default=None):
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else default


def _write_yaml(p: Path, obj, head: str = "") -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(head + yaml.safe_dump(obj, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(p)


def _write_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _log(cid: str, op: str, detail: dict) -> None:
    with (cdir(cid) / "changelog.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), "op": op, **detail}, ensure_ascii=False) + "\n")


# ---------- 下書きの読み書き ----------
def load_draft(cid: str) -> dict:
    """下書きを dict で読む: {"upper": {...}, "modules": {名前: {...}}, "routes": {...}, "alias_scopes": [...]}。"""
    d = cdir(cid) / "draft"
    up = _read_yaml(d / "upper.yaml")
    mods = {p.stem: _read_yaml(p) for p in sorted(d.glob("*.yaml")) if p.stem not in SPECIAL}
    rt = _read_yaml(d / "routes.yaml", {"fallback": "all", "routes": {}, "labels": {}})
    rt.setdefault("labels", {})
    sc = (_read_yaml(d / "alias_scopes.yaml", {}) or {}).get("alias_scopes", []) or []
    return {"upper": up, "modules": mods, "routes": rt, "alias_scopes": sc}


def save_draft(cid: str, dr: dict) -> None:
    d = cdir(cid) / "draft"
    d.mkdir(parents=True, exist_ok=True)
    _write_yaml(d / "upper.yaml", dr["upper"], "# 共通層の上層（下書き）。アプリの「共通層を育てる」で編集。\n")
    for name, mod in dr["modules"].items():
        _write_yaml(d / f"{name}.yaml", mod, f"# 共通層の部品「{name}」（下書き）。\n")
    for p in d.glob("*.yaml"):
        if p.stem not in SPECIAL and p.stem not in dr["modules"]:
            p.unlink()
    _write_yaml(d / "routes.yaml", dr["routes"], "# 部品の振り分け規則（IPC・FI の前方一致）と表示名。\n")
    _write_yaml(d / "alias_scopes.yaml", {"alias_scopes": dr["alias_scopes"]}, "# 使う分野（部品）を限る別名。\n")


def compose_draft(dr: dict) -> modular.ModularOntology:
    return modular.compose(dr["upper"], dr["modules"], dr["routes"].get("routes", {}), dr["routes"].get("fallback", "all"), dr["alias_scopes"])


# ---------- 一覧・複製 ----------
def list_common() -> list[dict]:
    out = []
    for d in sorted(root().iterdir()):
        if (d / "meta.json").exists():
            out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
    return out


def meta(cid: str) -> dict:
    return json.loads((cdir(cid) / "meta.json").read_text(encoding="utf-8"))


def create(source: str, name: str) -> dict:
    """既存の共通層（同梱・自作の版・追加した YAML）を複製して、新しい共通層の下書きを作る。"""
    from .engine import BUNDLED_ONTOLOGIES, is_modular, ontology_path

    cid = uuid.uuid4().hex[:10]
    d = cdir(cid)
    (d / "draft").mkdir(parents=True)
    src_path = ontology_path(source)
    if is_modular(source):
        for p in src_path.parent.glob("*.yaml"):
            shutil.copy(p, d / "draft" / p.name)
        rt = _read_yaml(d / "draft" / "routes.yaml", {"fallback": "all", "routes": {}})
        if not (d / "draft" / "alias_scopes.yaml").exists():
            _write_yaml(d / "draft" / "alias_scopes.yaml", {"alias_scopes": []})
    else:
        data = Ontology.load(src_path)
        raw = yaml.safe_load(src_path.read_text(encoding="utf-8"))
        up = {"version": str(raw.get("version", data.version)), "top_types": raw["top_types"], "predicates": raw["predicates"], "concepts": raw["concepts"]}
        _write_yaml(d / "draft" / "upper.yaml", up)
        _write_yaml(d / "draft" / "alias_scopes.yaml", {"alias_scopes": []})
        rt = {"fallback": "all", "routes": {}}
    from .engine import MODULE_LABEL

    rt.setdefault("labels", {k: MODULE_LABEL.get(k, k) for k in rt.get("routes", {})})
    _write_yaml(d / "draft" / "routes.yaml", rt)
    src_label = BUNDLED_ONTOLOGIES.get(source, {}).get("label") or source
    m = {"id": cid, "name": (name or "").strip() or f"{src_label} の複製", "created": _now(), "based_on": source, "based_on_label": src_label, "versions": []}
    _write_json(d / "meta.json", m)
    _log(cid, "create", {"source": source})
    return m


def rename(cid: str, name: str) -> dict:
    m = meta(cid)
    if _norm(name):
        m["name"] = _norm(name)
        _write_json(cdir(cid) / "meta.json", m)
    return m


def delete(cid: str) -> None:
    shutil.rmtree(cdir(cid), ignore_errors=True)


# ---------- 検査 ----------
_SHORT = re.compile(r"[A-Za-z0-9]{1,2}")


def check(dr: dict) -> dict:
    """発行してよいかの検査。errors が空なら発行できる。"""
    errors, warnings = [], []
    up = dr["upper"]
    up_ids = {c["id"] for c in up["concepts"]} | {t["id"] for t in up["top_types"]}
    parent = {c["id"]: (c.get("parents") or [None])[0] for c in up["concepts"]}
    all_ids = Counter(c["id"] for c in up["concepts"])
    for name, mod in dr["modules"].items():
        for c in mod.get("concepts", []):
            all_ids[c["id"]] += 1
            parent[c["id"]] = (c.get("parents") or [None])[0]
    for i, n in all_ids.items():
        if n > 1:
            errors.append(f"概念 ID が重なっています: {i}")
    known = set(all_ids) | {t["id"] for t in up["top_types"]}
    for i, p in parent.items():
        if p is None or p not in known:
            errors.append(f"親が見つかりません: {i} → {p}")

    def ancestors(x: str) -> set[str]:
        out, seen = set(), set()
        while x in parent and x not in seen:
            seen.add(x)
            x = parent[x]
            if x:
                out.add(x)
        return out

    scoped = {(_key(r["alias"]), r["concept"]): set(r["modules"]) for r in dr["alias_scopes"]}

    def forms(cs: list[dict]) -> list[tuple[str, str]]:
        mids = {p for c in cs for p in (c.get("parents") or [])}
        out = []
        for c in cs:
            fs = ([c["label"]] if c["id"] not in mids or c.get("aliases") else []) + list(c.get("aliases") or [])
            out.extend((f, c["id"]) for f in fs)
        return out

    up_forms = forms(up["concepts"])
    up_owner: dict[str, str] = {}
    for f, c in up_forms:
        k = _key(f)
        if k in up_owner and up_owner[k] != c:
            errors.append(f"全分野共通の中で同じ表記が 2 つの概念にあります: 「{f}」（{up_owner[k]} / {c}）")
        up_owner[k] = c
        if _SHORT.fullmatch(_norm(f)):
            errors.append(f"1〜2 文字の英数字の表記は使えません: 「{f}」（{c}）")
    cross = Counter()
    mod_owner: dict[str, dict[str, str]] = {}
    for name, mod in dr["modules"].items():
        own: dict[str, str] = {}
        for f, c in forms(mod.get("concepts", [])):
            k = _key(f)
            if k in own and own[k] != c:
                errors.append(f"分野別辞書「{name}」の中で同じ表記が 2 つの概念にあります: 「{f}」（{own[k]} / {c}）")
            own[k] = c
            if _SHORT.fullmatch(_norm(f)):
                errors.append(f"1〜2 文字の英数字の表記は使えません: 「{f}」（{c}）")
            if k in up_owner:
                u = up_owner[k]
                if name not in scoped.get((k, u), {name}):
                    pass
                elif u not in ancestors(c):
                    errors.append(f"分野別辞書「{name}」の「{f}」が全分野共通の {u} と重なっています。全分野共通の概念の子にするか、全分野共通の別名の分野を限ってください")
        mod_owner[name] = own
    names = list(mod_owner)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for k in set(mod_owner[a]) & set(mod_owner[b]):
                cross[(a, b)] += 1
    for (a, b), n in cross.items():
        warnings.append(f"分野別辞書「{a}」と「{b}」で同じ表記が {n} 個あります（分野で切り替えるので問題ありませんが、一枚の辞書として使うと曖昧になります）")
    if not errors:
        try:
            compose_draft(dr)
        except Exception as e:  # noqa: BLE001
            errors.append(f"組み立てられません: {e}")
    return {"errors": errors, "warnings": warnings}


# ---------- 状態 ----------
def state(cid: str) -> dict:
    dr = load_draft(cid)
    m = meta(cid)
    mo = compose_draft(dr)
    labels = dr["routes"].get("labels", {})
    mods = [{"id": "upper", "label": "上層", "routes": [], "concepts": sum(1 for c in dr["upper"]["concepts"])}]
    for name, mod in dr["modules"].items():
        mods.append({"id": name, "label": labels.get(name, name), "routes": dr["routes"].get("routes", {}).get(name, []), "concepts": len(mod.get("concepts", []))})
    log = []
    p = cdir(cid) / "changelog.jsonl"
    if p.exists():
        log = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[-40:]]
    last = m["versions"][-1]["version"] if m["versions"] else 0
    return {"meta": m, "modules": mods, "alias_scopes": dr["alias_scopes"], "fallback": dr["routes"].get("fallback", "all"),
            "check": check(dr), "log": list(reversed(log)), "dirty": _dirty(cid, last), "n_concepts": sum(1 for c in mo.onto.concepts.values() if c.concept_id not in mo.onto.top_types)}


def _dirty(cid: str, last: int) -> bool:
    if not last:
        return True
    a, b = cdir(cid) / "draft", cdir(cid) / f"v{last}"

    def load(d: Path) -> dict:
        out = {p.name: _read_yaml(p) for p in d.glob("*.yaml")}
        if out.get("upper.yaml"):
            out["upper.yaml"] = {k: v for k, v in out["upper.yaml"].items() if k != "version"}  # 版の名前は発行時に書き込む
        return out

    return load(a) != load(b)


def concepts(cid: str, module: str = "", q: str = "") -> list[dict]:
    """概念の一覧（部品で絞り、表示名・別名で検索）。"""
    dr = load_draft(cid)
    mo = compose_draft(dr)
    kids = Counter(p for c in mo.onto.concepts.values() for p in c.parent_ids)
    qq = _key(q)
    out = []
    for c in mo.onto.concepts.values():
        if c.concept_id in mo.onto.top_types:
            continue
        mod = mo.module_of.get(c.concept_id, "upper")
        if module and mod != module:
            continue
        if qq and qq not in _key(c.preferred_label) and not any(qq in _key(a) for a in c.aliases):
            continue
        p = c.parent_ids[0] if c.parent_ids else ""
        out.append({"id": c.concept_id, "label": c.preferred_label, "aliases": c.aliases, "parent": p, "parent_label": mo.onto.label(p) if p else "",
                    "top": c.top_type, "module": mod, "children": kids.get(c.concept_id, 0),
                    "scoped": [r for r in dr["alias_scopes"] if r["concept"] == c.concept_id]})
    out.sort(key=lambda x: (x["module"] != "upper", x["module"], x["top"], x["parent_label"], x["label"]))
    return out[:600]


# ---------- 編集 ----------
def _find(dr: dict, cid_: str) -> tuple[str, dict] | tuple[None, None]:
    for c in dr["upper"]["concepts"]:
        if c["id"] == cid_:
            return "upper", c
    for name, mod in dr["modules"].items():
        for c in mod.get("concepts", []):
            if c["id"] == cid_:
                return name, c
    return None, None


def _clean_aliases(xs) -> list[str]:
    out = []
    for a in xs or []:
        a = _norm(a)
        if a and a not in out:
            out.append(a)
    return out


def new_concept_id(module: str, label: str, top: str) -> str:
    pre = "U" if module == "upper" else re.sub(r"[^A-Za-z0-9]", "", module)[:6].upper() or "X"
    return f"{pre}_{top}_" + hashlib.sha1(_norm(label).encode("utf-8")).hexdigest()[:8]


def edit_concept(cid: str, req: dict) -> dict:
    """op = add（module, label, aliases, parent）/ edit（id, label, aliases, parent）/ delete（id）。"""
    dr = load_draft(cid)
    mo = compose_draft(dr)
    op = req.get("op")
    if op == "add":
        module = req.get("module") or "upper"
        if module != "upper" and module not in dr["modules"]:
            raise ValueError("分野別辞書が見つかりません")
        label = _norm(req.get("label", ""))
        parent = req.get("parent", "")
        if not label:
            raise ValueError("表示名が空です")
        if parent not in mo.onto.concepts:
            raise ValueError("親の概念が見つかりません")
        top = mo.onto.top_type(parent)
        new = {"id": new_concept_id(module, label, top), "label": label, "aliases": _clean_aliases(req.get("aliases")), "parents": [parent]}
        if _find(dr, new["id"])[1] is not None:
            raise ValueError("同じ表示名の概念がすでにあります")
        (dr["upper"]["concepts"] if module == "upper" else dr["modules"][module].setdefault("concepts", [])).append(new)
        _log(cid, "add", {"module": module, "id": new["id"], "label": label, "parent": parent})
    elif op == "edit":
        where, c = _find(dr, req.get("id", ""))
        if c is None:
            raise ValueError("概念が見つかりません")
        before = copy.deepcopy(c)
        if req.get("label"):
            c["label"] = _norm(req["label"])
        if "aliases" in req:
            c["aliases"] = _clean_aliases(req["aliases"])
        if req.get("parent") and req["parent"] != (c.get("parents") or [None])[0]:
            if req["parent"] not in mo.onto.concepts or req["parent"] == c["id"]:
                raise ValueError("親の概念が見つかりません")
            if c["id"] in mo.onto._ancestors.get(req["parent"], {}):
                raise ValueError("自分の子孫を親にはできません")
            c["parents"] = [req["parent"]]
        _log(cid, "edit", {"module": where, "id": c["id"], "before": before, "after": c})
    elif op == "delete":
        where, c = _find(dr, req.get("id", ""))
        if c is None:
            raise ValueError("概念が見つかりません")
        if any(c["id"] in (x.get("parents") or []) for x in dr["upper"]["concepts"] + [y for m in dr["modules"].values() for y in m.get("concepts", [])]):
            raise ValueError("子を持つ概念は消せません。先に子を移すか消してください")
        if where == "upper":
            dr["upper"]["concepts"] = [x for x in dr["upper"]["concepts"] if x["id"] != c["id"]]
        else:
            dr["modules"][where]["concepts"] = [x for x in dr["modules"][where]["concepts"] if x["id"] != c["id"]]
        dr["alias_scopes"] = [r for r in dr["alias_scopes"] if r["concept"] != c["id"]]
        _log(cid, "delete", {"module": where, "id": c["id"], "label": c["label"]})
    else:
        raise ValueError("指定が不正です")
    save_draft(cid, dr)
    return check(dr)


def set_module(cid: str, req: dict) -> dict:
    """部品を足す・振り分け規則と表示名を変える・消す（op = add / edit / delete）。"""
    dr = load_draft(cid)
    op = req.get("op")
    name = re.sub(r"[^a-z0-9_]", "", (req.get("id") or "").lower())
    if not name or name in SPECIAL:
        raise ValueError("分野別辞書の ID は英小文字・数字・_ で付けてください（upper・routes・alias_scopes は使えません）")
    prefixes = [p.strip().replace(" ", "") for p in re.split(r"[,、;\s]+", req.get("routes", "")) if p.strip()] if isinstance(req.get("routes"), str) else list(req.get("routes") or [])
    rt = dr["routes"]
    if op == "add":
        if name in dr["modules"]:
            raise ValueError("同じ ID の分野別辞書があります")
        dr["modules"][name] = {"module": name, "concepts": []}
        rt.setdefault("routes", {})[name] = prefixes
        rt.setdefault("labels", {})[name] = _norm(req.get("label", "")) or name
    elif op == "edit":
        if name not in dr["modules"]:
            raise ValueError("分野別辞書が見つかりません")
        rt.setdefault("routes", {})[name] = prefixes
        if req.get("label"):
            rt.setdefault("labels", {})[name] = _norm(req["label"])
    elif op == "delete":
        if dr["modules"].get(name, {}).get("concepts"):
            raise ValueError("概念を持つ分野別辞書は消せません")
        dr["modules"].pop(name, None)
        rt.get("routes", {}).pop(name, None)
        rt.get("labels", {}).pop(name, None)
    else:
        raise ValueError("指定が不正です")
    _log(cid, "module_" + op, {"module": name, "routes": prefixes})
    save_draft(cid, dr)
    return check(dr)


def set_scope(cid: str, req: dict) -> dict:
    """別名の分野制限（op = add / remove）。add は concept・alias・modules（使ってよい部品）。"""
    dr = load_draft(cid)
    concept, alias = req.get("concept", ""), _norm(req.get("alias", ""))
    where, c = _find(dr, concept)
    if c is None:
        raise ValueError("概念が見つかりません")
    if req.get("op") == "add":
        if alias not in c.get("aliases", []):
            raise ValueError("その概念の別名ではありません（表示名は限れません）")
        mods = [m for m in req.get("modules", []) if m in dr["modules"]]
        dr["alias_scopes"] = [r for r in dr["alias_scopes"] if not (r["concept"] == concept and _key(r["alias"]) == _key(alias))]
        dr["alias_scopes"].append({"concept": concept, "label": c["label"], "alias": alias, "modules": mods, "reason": _norm(req.get("reason", "")), "added": _now()})
    elif req.get("op") == "remove":
        dr["alias_scopes"] = [r for r in dr["alias_scopes"] if not (r["concept"] == concept and _key(r["alias"]) == _key(alias))]
    else:
        raise ValueError("指定が不正です")
    _log(cid, "scope_" + req.get("op", ""), {"concept": concept, "alias": alias, "modules": req.get("modules", [])})
    save_draft(cid, dr)
    return check(dr)


# ---------- 格上げ ----------
def promotions(cid: str) -> list[dict]:
    """この共通層（どの版でも）を使うマップの拡張層で採用された語を集める。共通層に既にある表記は除く。"""
    from .engine import home, read_json

    dr = load_draft(cid)
    mo = compose_draft(dr)
    known = {_key(s) for s, _ in mo.onto.surface_forms()}
    ignored = set(meta(cid).get("promotion_ignored", []))
    rows: dict[str, dict] = {}
    for pdir in sorted((home() / "projects").iterdir()):
        m = read_json(pdir / "meta.json", None)
        if not m or not str(m.get("ontology", "")).startswith(f"common:{cid}@"):
            continue
        ext = read_json(pdir / "extension.json", None) or {}
        for c in ext.get("concepts", []):
            for term in [c["label"], *c.get("aliases", [])]:
                k = _key(term)
                if k in known or k in ignored:
                    continue
                r = rows.setdefault(k, {"term": _norm(term), "kind": "concept", "maps": [], "parents": Counter(), "top": c.get("top")})
                r["maps"].append(m.get("name", pdir.name))
                if c.get("parent") in mo.onto.concepts:
                    r["parents"][c["parent"]] += 1
        for a in ext.get("aliases", []):
            k = _key(a["alias"])
            if k in known or k in ignored:
                continue
            r = rows.setdefault(k, {"term": _norm(a["alias"]), "kind": "alias", "maps": [], "parents": Counter(), "top": None})
            r["maps"].append(m.get("name", pdir.name))
            if a.get("concept_id") in mo.onto.concepts:
                r["parents"][a["concept_id"]] += 1
                r["kind"] = "alias"
    out = []
    for r in rows.values():
        target = r["parents"].most_common(1)[0][0] if r["parents"] else ""
        out.append({"term": r["term"], "kind": r["kind"], "n_maps": len(set(r["maps"])), "maps": sorted(set(r["maps"]))[:5],
                    "target": target, "target_label": mo.onto.label(target) if target else "", "target_module": mo.module_of.get(target, "upper") if target else "upper",
                    "top": r["top"] or (mo.onto.top_type(target) if target else "")})
    out.sort(key=lambda x: (-x["n_maps"], x["term"]))
    return out


def promote(cid: str, req: dict) -> dict:
    """格上げ: mode = concept（module・parent に新しい概念）/ alias（concept_id の別名に足す）/ ignore（候補に出さない）。"""
    term = _norm(req.get("term", ""))
    if not term:
        raise ValueError("語が空です")
    if req.get("mode") == "ignore":
        m = meta(cid)
        m.setdefault("promotion_ignored", []).append(_key(term))
        _write_json(cdir(cid) / "meta.json", m)
        _log(cid, "promote_ignore", {"term": term})
        return check(load_draft(cid))
    if req.get("mode") == "concept":
        res = edit_concept(cid, {"op": "add", "module": req.get("module") or "upper", "label": term, "aliases": req.get("aliases", []), "parent": req.get("parent")})
    elif req.get("mode") == "alias":
        dr = load_draft(cid)
        _, c = _find(dr, req.get("concept_id", ""))
        if c is None:
            raise ValueError("別名を足す先の概念が見つかりません")
        res = edit_concept(cid, {"op": "edit", "id": c["id"], "aliases": [*c.get("aliases", []), term]})
    else:
        raise ValueError("指定が不正です")
    _log(cid, "promote", {"term": term, "mode": req.get("mode")})
    return res


# ---------- 新しい分野: マップの文書から候補を探す ----------
def corpus_candidates(cid: str, pid: str, module: str = "") -> dict:
    """マップの文書のうち、部品 module に振り分けられる文書（module が空なら全文書）から、下書きの共通層で拾えない語を数える。"""
    import pandas as pd

    from . import layers
    from .engine import pdir

    dr = load_draft(cid)
    mo = compose_draft(dr)
    sec = pd.read_csv(pdir(pid) / "source.csv", dtype=str, keep_default_na=False)
    if module and module != "upper":
        prefixes = dr["routes"].get("routes", {}).get(module, [])
        if prefixes:
            sec = sec[[any(c.strip().replace(" ", "").startswith(p) for c in re.split(r"[;,]", f"{r.get('ipc', '')};{r.get('fi', '')}") for p in prefixes) for _, r in sec.iterrows()]]
    cands = layers.candidates(sec.reset_index(drop=True), mo.onto, set(), top_n=80, min_df=max(2, len(sec) // 200))
    return {"n_docs": int(len(sec)), "candidates": cands}


# ---------- 発行 ----------
def publish(cid: str, note: str = "") -> dict:
    """検査を通った下書きを版として固定する（v<n>）。"""
    dr = load_draft(cid)
    res = check(dr)
    if res["errors"]:
        raise ValueError("検査で直すところがあります: " + "；".join(res["errors"][:5]))
    m = meta(cid)
    v = (m["versions"][-1]["version"] if m["versions"] else 0) + 1
    dst = cdir(cid) / f"v{v}"
    shutil.copytree(cdir(cid) / "draft", dst)
    up = _read_yaml(dst / "upper.yaml")
    up["version"] = f"{m['name']} v{v}"
    _write_yaml(dst / "upper.yaml", up, f"# 共通層「{m['name']}」 v{v}（{_now()} 発行。変えない）\n")
    mo = compose_draft(load_draft(cid))
    m["versions"].append({"version": v, "published": _now(), "note": _norm(note), "concepts": sum(1 for c in mo.onto.concepts.values() if c.concept_id not in mo.onto.top_types)})
    _write_json(cdir(cid) / "meta.json", m)
    _log(cid, "publish", {"version": v, "note": note})
    return m


def version_dir(cid: str, v: int) -> Path:
    d = cdir(cid) / f"v{v}"
    if not d.exists():
        raise ValueError(f"共通辞書の版が見つかりません: {cid} v{v}")
    return d


def surfaces_of(oid_dir: Path) -> set[str]:
    mo = modular.load_dir(oid_dir)
    return {_key(s) for s, _ in mo.onto.surface_forms()}


# ---------- CSV で作る（チャットの LLM に下書きさせる） ----------
REPO = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO / "config" / "prompts" / "common_layer_prompt.md"
CSV_COLUMNS = ["型", "分野", "中間概念", "概念", "別名"]
TYPE_OF = {"課題": "P", "p": "P", "problem": "P", "解決手段": "M", "手段": "M", "m": "M", "solution": "M", "用途": "U", "u": "U", "use": "U"}
GENERIC = {"構造", "方法", "材料", "装置", "表面", "形状", "性能", "特性", "製造", "処理", "工程", "部材", "部品", "システム", "技術"}


def build_prompt(field: str, modules: list[dict] | None = None, n_concepts: int = 200, sample: str = "") -> str:
    """チャットの LLM に貼るプロンプト。modules = [{"label": 分野名, "hint": 範囲の説明}]（無ければ分野に分けない）。"""
    t = PROMPT_PATH.read_text(encoding="utf-8")
    mods = [m for m in (modules or []) if _norm(m.get("label", ""))]
    if mods:
        ms = "## 分野（部品）\n分野ごとに意味が変わる語は、分野を分けて持ちます。どの分野にも使う語は「分野」を空にしてください（共通の層に入ります）。\n" + \
             "\n".join(f"- {_norm(m['label'])}: {_norm(m.get('hint', ''))}" for m in mods)
        rule = "上の分野名のどれか。どの分野にも使う語は空にする"
        route = ("あわせて、分野ごとに特許分類（IPC・FI）の前方一致を、次の形の行で CSV の最後に足してください（型を「振り分け」にし、別名に分類を「|」で区切って書く）。\n"
                 f"例: 振り分け,{_norm(mods[0]['label'])},,,H01M|H02J7")
        ex = _norm(mods[0]["label"])
    else:
        ms, rule, route, ex = "", "空にする（分野に分けない）", "", ""
    samp = ("\n## 見本の特許文（この分野でよく出る書き方を参考にしてください）\n" + sample.strip()[:12000] + "\n") if sample.strip() else ""
    return (t.replace("{{FIELD}}", _norm(field) or "（分野を書いてください）").replace("{{MODULES}}", ms).replace("{{N_CONCEPTS}}", str(int(n_concepts)))
            .replace("{{SAMPLE}}", samp).replace("{{MODULE_RULE}}", rule).replace("{{ROUTE_RULE}}", route).replace("{{EXAMPLE_MODULE}}", ex))


def _read_csv_rows(data: bytes) -> list[dict]:
    import csv
    import io

    text = None
    for enc in ("utf-8-sig", "cp932", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("文字コードが読めません（UTF-8 か Shift_JIS で保存してください）")
    # チャットの出力をそのまま貼ったとき用に、``` の行を除く
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    rd = csv.DictReader(io.StringIO("\n".join(lines)))
    cols = [_norm(c) for c in (rd.fieldnames or [])]
    if "概念" not in cols or "型" not in cols:
        raise ValueError("見出しに「型」と「概念」が要ります（型,分野,中間概念,概念,別名）")
    out = []
    for i, r in enumerate(rd, start=2):
        out.append({"_row": i, **{_norm(k): _norm(v) for k, v in r.items() if k is not None}})
    return out


def import_csv(data: bytes, name: str = "", filename: str = "", cid: str | None = None) -> dict:
    """CSV から共通層の下書きを作る（cid を渡すとその下書きを置き換える）。
    決まりに合わない行・別名は止めずに外し、外した理由を返す。"""
    rows = _read_csv_rows(data)
    base = _read_yaml(REPO / "config" / "modules" / "upper.yaml")
    notes: list[dict] = []
    mod_label: dict[str, str] = {}
    routes: dict[str, list[str]] = {}

    def mod_id(label: str) -> str:
        if not label:
            return "upper"
        for k, v in mod_label.items():
            if v == label:
                return k
        k = f"f{len(mod_label) + 1}"
        mod_label[k] = label
        return k

    upper = {"version": "csv", "top_types": base["top_types"], "predicates": base["predicates"], "concepts": []}
    mods: dict[str, dict] = {}
    by_key: dict[tuple, dict] = {}
    mids: dict[tuple, str] = {}
    for r in rows:
        typ = (r.get("型") or "").strip()
        if typ in ("振り分け", "分類", "route"):
            m = mod_id(r.get("分野", ""))
            if m == "upper":
                notes.append({"row": r["_row"], "msg": "振り分けの行に分野がありません"})
                continue
            routes.setdefault(m, [])
            routes[m] += [p.strip().replace(" ", "") for p in re.split(r"[|｜,、;／/\s]+", r.get("別名", "")) if p.strip()]
            continue
        top = TYPE_OF.get(typ.lower() if typ.isascii() else typ)
        label = r.get("概念", "")
        if not top or not label:
            notes.append({"row": r["_row"], "msg": f"型「{typ}」か概念が空のため外した"})
            continue
        if label in GENERIC or _SHORT.fullmatch(label):
            notes.append({"row": r["_row"], "msg": f"「{label}」は意味の薄い語か 1〜2 文字の英数字なので外した"})
            continue
        m = mod_id(r.get("分野", ""))
        target = upper["concepts"] if m == "upper" else mods.setdefault(m, {"module": m, "concepts": []})["concepts"]
        mid_label = r.get("中間概念", "")
        parent = top
        if mid_label:
            mk = (m, top, mid_label)
            if mk not in mids:
                mids[mk] = new_concept_id(m, "MID:" + mid_label, top)
                target.append({"id": mids[mk], "label": mid_label, "aliases": [], "parents": [top]})
            parent = mids[mk]
        aliases = []
        for a in re.split(r"[|｜]", r.get("別名", "")):
            a = _norm(a)
            if not a or a == label:
                continue
            if _SHORT.fullmatch(a) or a in GENERIC:
                notes.append({"row": r["_row"], "msg": f"別名「{a}」は 1〜2 文字の英数字か意味の薄い語なので外した"})
                continue
            aliases.append(a)
        key = (m, top, label)
        if key in by_key:
            by_key[key]["aliases"] = list(dict.fromkeys(by_key[key]["aliases"] + aliases))
            notes.append({"row": r["_row"], "msg": f"「{label}」が重なっていたので別名をまとめた"})
            continue
        c = {"id": new_concept_id(m, label, top), "label": label, "aliases": list(dict.fromkeys(aliases)), "parents": [parent]}
        by_key[key] = c
        target.append(c)
    # 同じ層（上層・各部品）の中で同じ表記が 2 つの概念にあれば、後の概念から外す
    for layer_name, cs in [("upper", upper["concepts"]), *[(k, v["concepts"]) for k, v in mods.items()]]:
        owner: dict[str, str] = {}
        midset = {p for c in cs for p in c["parents"]}
        for c in cs:
            if c["id"] in midset and not c["aliases"]:
                continue
            keep = []
            for f in [c["label"], *c["aliases"]]:
                k = _key(f)
                if k in owner and owner[k] != c["id"]:
                    notes.append({"row": None, "msg": f"「{f}」が {layer_name} の 2 つの概念にあったので「{c['label']}」から外した"})
                    continue
                owner[k] = c["id"]
                keep.append(f)
            if keep and keep[0] == c["label"]:
                c["aliases"] = keep[1:]
    # 部品の表記が上層と（親子でなく）重なれば、部品側から外す
    up_keys = {_key(f) for c in upper["concepts"] for f in [c["label"], *c["aliases"]]}
    for k, mod in mods.items():
        for c in mod["concepts"]:
            dup = [a for a in c["aliases"] if _key(a) in up_keys]
            if dup:
                c["aliases"] = [a for a in c["aliases"] if _key(a) not in up_keys]
                notes.append({"row": None, "msg": f"分野「{mod_label[k]}」の「{c['label']}」の別名 {'・'.join(dup)} は全分野共通と重なるので外した"})
    dr = {"upper": upper, "modules": mods, "routes": {"fallback": "all", "routes": {k: list(dict.fromkeys(v)) for k, v in routes.items()} | {k: routes.get(k, []) for k in mods if k not in routes},
                                                       "labels": mod_label}, "alias_scopes": []}
    if cid is None:
        cid = uuid.uuid4().hex[:10]
        cdir(cid).mkdir(parents=True)
        m = {"id": cid, "name": _norm(name) or f"CSV から（{filename or '無題'}）", "created": _now(), "based_on": "csv", "based_on_label": f"CSV（{filename or '無題'}）", "versions": []}
        _write_json(cdir(cid) / "meta.json", m)
    save_draft(cid, dr)
    n = Counter(("upper" if mm == "upper" else mod_label[mm], TOP_LABEL[t]) for (mm, t, _l) in by_key)
    _log(cid, "import_csv", {"label": filename, "rows": len(rows), "notes": len(notes)})
    return {"id": cid, "rows": len(rows), "concepts": len(by_key), "mids": len(mids), "counts": [{"module": k[0], "type": k[1], "n": v} for k, v in sorted(n.items())],
            "modules": [{"id": k, "label": v, "routes": dr["routes"]["routes"].get(k, [])} for k, v in mod_label.items()], "notes": notes[:200], "check": check(dr)}


def export_csv(cid: str) -> str:
    """下書きを CSV（型,分野,中間概念,概念,別名）で書き出す。Excel で直して import_csv で取り込み直せる。"""
    import csv
    import io

    dr = load_draft(cid)
    labels = dr["routes"].get("labels", {})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for mname, cs in [("upper", dr["upper"]["concepts"]), *[(k, v.get("concepts", [])) for k, v in dr["modules"].items()]]:
        allc = {c["id"]: c for c in dr["upper"]["concepts"] + [y for v in dr["modules"].values() for y in v.get("concepts", [])]}
        mids = {p for c in cs for p in c.get("parents", [])}
        for c in cs:
            if c["id"] in mids and not c.get("aliases"):
                continue
            # 型は上位型までたどる
            x, mid = c, ""
            while x["parents"][0] not in ("P", "M", "U"):
                x = allc.get(x["parents"][0])
                if x is None:
                    break
                mid = mid or x["label"]
            top = x["parents"][0] if x else "M"
            w.writerow([TOP_LABEL.get(top, top), "" if mname == "upper" else labels.get(mname, mname), mid, c["label"], "|".join(c.get("aliases", []))])
    for k, v in dr["routes"].get("routes", {}).items():
        if v:
            w.writerow(["振り分け", labels.get(k, k), "", "", "|".join(v)])
    return buf.getvalue()


def trial(cid: str, pid: str) -> dict:
    """試し当て: マップの文書に下書きの共通層を当てて、取りこぼし率を、マップが今使っている共通層と比べる。"""
    import pandas as pd

    from . import layers
    from .engine import _coverage, pdir, project_ontology, read_json

    dr = load_draft(cid)
    mo = compose_draft(dr)
    sec = pd.read_csv(pdir(pid) / "source.csv", dtype=str, keep_default_na=False)
    cache: dict[frozenset, layers.Matcher] = {}
    route_count: Counter = Counter()
    res = {}
    labels = dr["routes"].get("labels", {})
    for col, top in layers.SECTION_TOP.items():
        n = zero = 0
        for _, r in sec.iterrows():
            codes = ";".join([r.get("ipc", ""), r.get("fi", "")])
            act = frozenset(mo.modules_for(codes))
            if col == "problem":
                cs_ = [c.strip().replace(" ", "") for c in re.split(r"[;,]", codes) if c.strip()]
                direct = {m for m, ps in mo.routes.items() if any(c.startswith(p) for c in cs_ for p in ps)}
                key = "・".join(sorted(labels.get(a, a) for a in direct)) if direct else ("どれにも当たらない（すべての分野を使う）" if act else "（共通の層だけ）")
                route_count[key] += 1
            t = layers._norm(r.get(col, ""))
            if not t.strip():
                continue
            if act not in cache:
                m = layers.Matcher(mo.onto)
                m.index = {k: [(s_, c) for s_, c in v if mo.allowed(s_, c, set(act))] for k, v in m.index.items()}
                cache[act] = m
            n += 1
            zero += not any(mo.onto.top_type(c) == top for _, _, _, c in cache[act].find(t))
        res[col] = {"label": layers.SECTION_LABEL[col], "n": n, "rate": round(zero / n, 3) if n else None}
    cur = _coverage(pid, sec, project_ontology(pid, False), False)
    meta_p = read_json(pdir(pid) / "meta.json")
    return {"draft": res, "current": cur, "current_ontology": meta_p.get("ontology"), "routes": dict(route_count), "n_docs": int(len(sec))}
