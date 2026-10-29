"""特許俯瞰マップ アプリの計算部分。

1 つの「マップ」= 取り込んだ特許一覧 1 つ。保存先 ONTOMAP_HOME（既定 ~/OntoMap）の projects/<id>/ に置く。
- source.csv   取り込んだ一覧を documents_sections の形にそろえたもの
- meta.json    名前・列の対応・オントロジー・作成日
- status.json  計算の進み具合（stage, progress, message, error）
- graphs/      関係方式の三つ組（GraphLayer）
- sims.npz     方式ごとの類似度行列
- map.json     画面に渡す地図データ（座標・等高線・見出し・近傍・用途をまたぐ組）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from ..ontology import Ontology
from ..schema import GraphLayer

REPO = Path(__file__).resolve().parents[2]
BUNDLED_ONTOLOGIES = {
    "inorganic-1.0": {"label": "無機材料 課題・解決手段・用途（inorg-1.0）", "path": REPO / "config" / "ontology_inorganic_v1.yaml"},
    "mof-0.1": {"label": "MOF 課題・解決手段・用途（mof-0.1）", "path": REPO / "config" / "ontology_mof_v0_1.yaml"},
    # 分野ごとの部品を文書の分類（IPC・FI）で切り替える共通層（ontomap/modular.py、config/modules/）
    "tech-0.1": {"label": "技術（試作）: 全分野共通＋無機材料・電池・半導体の分野別辞書（分類で切り替え）", "path": REPO / "config" / "modules" / "upper.yaml", "modular": True},
}
MODULE_LABEL = {"upper": "全分野共通", "inorganic": "無機材料", "battery": "電池", "semicon": "半導体"}
DEFAULT_ONTOLOGY = "inorganic-1.0"
FIELDS = [
    ("title", "名称", True),
    ("abstract", "要約", False),
    ("problem", "課題", False),
    ("solution", "解決手段", False),
    ("use_text", "用途", False),
    ("applicants", "出願人", False),
    ("filing_date", "出願日", False),
    ("ipc", "IPC", False),
    ("fi", "FI", False),  # 部品で切り替える共通層では、IPC と合わせて振り分けに使う
    ("pub_number", "公報番号", False),
    ("claim1", "請求項 1", False),
    ("description", "明細書（全文）", False),  # 選ぶと、用途の文をここから切り出す（ontomap/patents/use_extract.py）
]
GUESS = {
    "title": [r"発明の名称", r"^名称$", r"title", r"タイトル"],
    "abstract": [r"要約", r"abstract", r"抄録"],
    "problem": [r"課題", r"problem"],
    "solution": [r"解決手段", r"solution", r"手段"],
    "use_text": [r"用途", r"利用可能性", r"\buse"],
    "applicants": [r"出願人", r"権利者", r"applicant", r"assignee"],
    "filing_date": [r"出願日", r"filing", r"application date"],
    "ipc": [r"^ipc", r"国際特許分類", r"\bipc\b"],
    "fi": [r"^fi$", r"^fi[\s_（(]", r"fi記号", r"fi分類"],
    "pub_number": [r"公報番号", r"公開番号", r"文献番号", r"publication", r"pub_number", r"登録番号"],
    "claim1": [r"請求項", r"claim"],
    "description": [r"明細書", r"全文", r"本文", r"description"],
}
METHODS = {"emb": "意味の類似性", "rel": "技術関係の類似性", "hyb": "併用（意味＋技術関係）"}
_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}


# ---------- 保存先 ----------
def home() -> Path:
    raw = os.environ.get("ONTOMAP_HOME", "")
    h = Path(raw).expanduser() if raw else Path.home() / "OntoMap"
    (h / "projects").mkdir(parents=True, exist_ok=True)
    (h / "uploads").mkdir(parents=True, exist_ok=True)
    (h / "ontologies").mkdir(parents=True, exist_ok=True)
    return h


def pdir(pid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{12}", pid):
        raise ValueError("マップの ID が不正です")
    return home() / "projects" / pid


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=_default), encoding="utf-8")
    tmp.replace(path)


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---------- オントロジー ----------
def ontologies() -> list[dict]:
    from . import common

    out = [{"id": k, "label": v["label"], "bundled": True} for k, v in BUNDLED_ONTOLOGIES.items() if v["path"].exists()]
    for m in common.list_common():  # 使用者が育てた共通層（発行した最新の版）
        if m["versions"]:
            v = m["versions"][-1]["version"]
            out.append({"id": f"common:{m['id']}@v{v}", "label": f"{m['name']} v{v}（自作）", "bundled": False, "common": m["id"]})
    for p in sorted((home() / "ontologies").glob("*.yaml")):
        out.append({"id": "user:" + p.stem, "label": f"{p.stem}（追加したもの）", "bundled": False})
    return out


def ontology_path(oid: str) -> Path:
    if oid in BUNDLED_ONTOLOGIES:
        return BUNDLED_ONTOLOGIES[oid]["path"]
    m = re.fullmatch(r"common:([a-z0-9]{10})@v(\d+)", oid or "")
    if m:
        from . import common

        return common.version_dir(m.group(1), int(m.group(2))) / "upper.yaml"
    if oid.startswith("user:"):
        p = home() / "ontologies" / (oid[5:] + ".yaml")
        if p.exists():
            return p
    raise ValueError(f"オントロジーが見つかりません: {oid}")


def add_ontology(filename: str, data: bytes) -> dict:
    stem = re.sub(r"[^\w\-]+", "_", Path(filename).stem)[:60] or "ontology"
    dest = home() / "ontologies" / f"{stem}.yaml"
    dest.write_bytes(data)
    try:
        o = Ontology.load(dest)
    except Exception as e:  # noqa: BLE001 - 読めない YAML は登録しない
        dest.unlink(missing_ok=True)
        raise ValueError(f"オントロジーとして読めません: {e}") from e
    tops = set(o.top_types)
    if not {"P", "M", "U"} <= tops:
        dest.unlink(missing_ok=True)
        raise ValueError("上位型に P（課題）・M（解決手段）・U（用途）が必要です")
    return {"id": "user:" + stem, "label": f"{stem}（追加したもの）"}


# ---------- 2 層のオントロジー ----------
def project_ontology(pid: str, use_extension: bool = True, built: bool = False) -> Ontology:
    """共通層（選んだオントロジー）に、そのマップの拡張層を重ねる。built=True なら地図を描いた時点の拡張層を使う。"""
    from . import layers

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    mo = project_modular(pid, use_extension, built)
    if mo is not None:
        return mo.onto
    base = layers.base_dict(ontology_path(meta["ontology"]))
    if not use_extension:
        return layers.merged(base, None)
    ext = read_json(d / "extension_built.json", layers.empty_extension()) if built else layers.load_extension(d)
    return layers.merged(base, ext)


def is_modular(oid: str) -> bool:
    return bool(BUNDLED_ONTOLOGIES.get(oid, {}).get("modular")) or str(oid).startswith("common:")


def module_labels(oid: str) -> dict[str, str]:
    """部品の表示名（自作の共通層は routes.yaml の labels）。"""
    out = dict(MODULE_LABEL)
    if is_modular(oid):
        import yaml as _y

        rt = _y.safe_load((ontology_path(oid).parent / "routes.yaml").read_text(encoding="utf-8")) or {}
        out.update(rt.get("labels") or {})
    return out


def project_modular(pid: str, use_extension: bool = True, built: bool = False):
    """部品で切り替える共通層を選んだマップなら、拡張層を重ねた ModularOntology を返す（それ以外は None）。"""
    from . import layers
    from .. import modular

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    if not is_modular(meta["ontology"]):
        return None
    mo = modular.load_dir(ontology_path(meta["ontology"]).parent)
    if not use_extension:
        return mo
    ext = read_json(d / "extension_built.json", layers.empty_extension()) if built else layers.load_extension(d)
    return modular.with_data(mo, layers.merged_data(mo.data, ext))


# ---------- 取り込み ----------
def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp932", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("文字コードを判別できません（UTF-8 か Shift_JIS で保存してください）")
        from io import StringIO

        sep = "\t" if text.split("\n", 1)[0].count("\t") > text.split("\n", 1)[0].count(",") else ","
        df = pd.read_csv(StringIO(text), dtype=str, sep=sep)
    df = df.fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def guess_mapping(columns: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    norm = {c: unicodedata.normalize("NFKC", c).lower() for c in columns}
    for field, pats in GUESS.items():
        for pat in pats:
            hit = next((c for c in columns if c not in used and re.search(pat, norm[c])), None)
            if hit:
                out[field] = hit
                used.add(hit)
                break
    return out


def _upload_path(upload_id: str) -> Path:
    """取り込んだファイル本体（元のファイル名を記録した .name の控えは除く）。"""
    if not re.fullmatch(r"[a-f0-9]{12}", upload_id):
        raise ValueError("取り込みの ID が不正です")
    path = next((p for p in (home() / "uploads").glob(upload_id + ".*") if p.suffix != ".name"), None)
    if path is None:
        raise ValueError("取り込んだファイルが見つかりません")
    return path


def preview(upload_id: str) -> dict:
    path = _upload_path(upload_id)
    df = read_table(path)
    return {
        "upload_id": upload_id,
        "filename": (home() / "uploads" / f"{upload_id}.name").read_text(encoding="utf-8") if (home() / "uploads" / f"{upload_id}.name").exists() else path.name,
        "n_rows": int(len(df)),
        "columns": list(df.columns),
        "rows": df.head(5).to_dict("records"),
        "mapping": guess_mapping(list(df.columns)),
        "fields": [{"key": k, "label": lab, "required": req} for k, lab, req in FIELDS],
    }


def save_upload(filename: str, data: bytes) -> str:
    uid = uuid.uuid4().hex[:12]
    safe = re.sub(r"[^\w\-.]+", "_", filename)[-80:]
    (home() / "uploads" / f"{uid}.{safe.rsplit('.', 1)[-1].lower()}").write_bytes(data)
    (home() / "uploads" / f"{uid}.name").write_text(filename, encoding="utf-8")
    return uid


def create_project(name: str, upload_id: str, mapping: dict[str, str], ontology_id: str) -> str:
    path = _upload_path(upload_id)
    if not mapping.get("title"):
        raise ValueError("「名称」の列を選んでください")
    if not any(mapping.get(k) for k in ("abstract", "problem", "solution", "use_text", "description")):
        raise ValueError("要約・課題・解決手段・用途・明細書のどれか 1 つ以上の列を選んでください")
    ontology_path(ontology_id)
    df = read_table(path)
    out = pd.DataFrame({"doc_id": [f"D{i + 1:04d}" for i in range(len(df))]})
    for key, _, _ in FIELDS:
        col = mapping.get(key)
        out[key] = df[col].astype(str) if col and col in df else ""
    # 用途は 2 通り持つ（使い分け）: use_text = 地図の計算に使う段落、use_sentences = 近い理由の確認に使う用途の文
    from ..patents.use_extract import extract_use, extract_use_paragraphs

    out["use_source"] = ["用途の列" if t.strip() else "" for t in out["use_text"]]
    sents, sents_src = [], []
    for i in out.index:
        desc, title = out.at[i, "description"], out.at[i, "title"]
        if desc.strip():
            if not out.at[i, "use_text"].strip():
                out.at[i, "use_text"], out.at[i, "use_source"] = extract_use_paragraphs(desc)
                if not out.at[i, "use_text"]:
                    out.at[i, "use_source"] = ""
            t, src, _ = extract_use(desc, title)
        else:
            t, src, _ = extract_use(out.at[i, "use_text"], title)  # 全文が無ければ、用途の列から用途の文だけを選ぶ
        sents.append(t)
        sents_src.append(src if t else "")
    out["use_sentences"], out["use_sentences_source"] = sents, sents_src
    out = out.drop(columns=["description"])
    out = out[out[["title", "abstract", "problem", "solution", "use_text"]].apply(lambda r: any(x.strip() for x in r), axis=1)]
    if len(out) < 10:
        raise ValueError("文書が 10 件未満です。地図を作るには 10 件以上が必要です")
    pid = uuid.uuid4().hex[:12]
    d = pdir(pid)
    d.mkdir(parents=True)
    out.to_csv(d / "source.csv", index=False)
    _write(d / "meta.json", {"id": pid, "use_extension": True, "name": name.strip() or "名称未設定のマップ", "created": time.strftime("%Y-%m-%d %H:%M"), "source_file": (home() / "uploads" / f"{upload_id}.name").read_text(encoding="utf-8") if (home() / "uploads" / f"{upload_id}.name").exists() else path.name, "mapping": mapping, "ontology": ontology_id, "n_docs": int(len(out))})
    _status(pid, "queued", 0.0, "計算の順番を待っています")
    threading.Thread(target=run_project, args=(pid,), daemon=True).start()
    return pid


def list_projects() -> list[dict]:
    out = []
    for d in sorted((home() / "projects").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = read_json(d / "meta.json")
        if meta:
            meta["status"] = read_json(d / "status.json", {})
            out.append(meta)
    return out


def delete_project(pid: str) -> None:
    shutil.rmtree(pdir(pid), ignore_errors=True)
    _CACHE.pop(pid, None)


def _status(pid: str, stage: str, progress: float, message: str, error: str | None = None) -> None:
    _write(pdir(pid) / "status.json", {"stage": stage, "progress": round(progress, 3), "message": message, "error": error, "updated": time.strftime("%H:%M:%S")})


# ---------- 計算 ----------
EXTRACTION = {
    "sections": {"problem": "P", "solution": "M", "use": "U"},
    "role_types": {"P": "課題", "M": "手段", "U": "用途"},
    "mismatch_policy": "type",
    "cross_type_factor": 0.8,
    "relations": {"addresses": {"subject": "手段", "object": "課題"}, "used_for": {"subject": "手段", "object": "用途"}, "arises_in": {"subject": "課題", "object": "用途"}},
    "confidence": {"sentence": 0.9, "section": 0.7, "cross_section": 0.5},
    "max_triples_per_doc": 30,
    "case_insensitive": True,
    "negation": {"cues": ["できない", "できず", "得ない", "困難", "不可能", "至らない"], "exclude": ["ないよう", "ことなく"]},
}
RELATION = {"weights": {"subject": 1.0, "predicate": 1.0, "object": 1.0}, "epsilon": 0.01, "min_endpoint_sim": 0.3, "role_mismatch": 0.3, "negation_mismatch": 0.2,
            "speculation_mismatch": 0.8, "missing_penalty": 0.7, "aggregation": "assignment", "predicate_weights": {"addresses": 1.0, "used_for": 0.8, "arises_in": 0.6}}
EMBEDDING_MODEL = "cl-nagoya/ruri-v3-30m"
_ENCODER = None


def _encoder():
    global _ENCODER
    if _ENCODER is None:
        from ..patents.vectors import RuriEncoder

        _ENCODER = RuriEncoder(EMBEDDING_MODEL, "cpu", "", 8, None)
    return _ENCODER


def _short_applicant(name: str) -> str:
    from ..landscape import short_applicant

    return short_applicant(name)


def _year(s: str) -> int | None:
    m = re.search(r"(19|20)\d{2}", unicodedata.normalize("NFKC", s or ""))
    return int(m.group(0)) if m else None


def _layout(S: np.ndarray, seed: int) -> np.ndarray:
    from ..reduce import run_umap, to_distance

    D, _ = to_distance(S)
    return run_umap(D, seed, n_neighbors=min(15, max(5, len(S) // 10)), min_dist=0.1)


def _pack_islands(xy: np.ndarray, S: np.ndarray | None = None) -> tuple[np.ndarray, int]:
    """配置が離れた島に分かれるとき、島ごとに大きさをそろえて並べ直す。

    部品で切り替える共通層では、分野の違う文書が関係の上でほとんどつながらず、UMAP が島を遠くに置く。
    そのまま正規化すると島の中がつぶれて読めない（3 分野 300 件の関係方式で、島の中の幅が 0.1 程度）。
    島の判定: 単連結で、点の最近傍距離の中央値の 10 倍を超える隙間で分ける（近い k 件のグラフでは、分野をまたぐ
    弱いつながりで 1 つにつながってしまうため、配置の隙間で見る）。2% 未満の小さな島は、いちばん近い大きな島に入れる。
    並べ方: 島ごとに中心をそろえ、半径を文書数の平方根に比例させて格子に置く。島どうしの距離には意味がない。
    いちばん大きな島の半径が全体の半径の 40% 以上なら、つぶれていないとみて並べ直さない（単一分野の地図の小さな離れ島など）。
    返り値は (座標, 島の数)。S は使わない（呼び出し側との互換のため）。"""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial import cKDTree

    n = len(xy)
    if n < 20:
        return xy, 1
    med = float(np.median(cKDTree(xy).query(xy, k=2)[0][:, 1]))
    if med <= 0:
        return xy, 1
    lab = fcluster(linkage(xy, "single"), 10 * med, "distance")
    sizes = {c: int((lab == c).sum()) for c in np.unique(lab)}
    big = sorted([c for c, k in sizes.items() if k >= max(5, int(0.02 * n))], key=lambda c: -sizes[c])
    if len(big) < 2:
        return xy, 1
    # つぶれ方がひどいときだけ並べ直す: いちばん大きな島の半径が全体の半径の 40% 未満
    glob = float(np.percentile(np.linalg.norm(xy - np.median(xy, axis=0), axis=1), 97)) or 1.0
    r_max = max(float(np.percentile(np.linalg.norm(xy[lab == c] - np.median(xy[lab == c], axis=0), axis=1), 97)) for c in big)
    if r_max / glob >= 0.4:
        return xy, 1
    cent = {c: np.median(xy[lab == c], axis=0) for c in big}
    for c in sizes:
        if c not in big:
            near = min(big, key=lambda b: float(np.linalg.norm(np.median(xy[lab == c], axis=0) - cent[b])))
            lab[lab == c] = near
    out = np.zeros_like(xy, dtype=float)
    cols_n = int(np.ceil(np.sqrt(len(big))))
    radii = {c: float(np.sqrt((lab == c).sum() / n)) for c in big}
    cell = 2.3 * max(radii.values())
    for i, c in enumerate(big):
        idx = lab == c
        z = xy[idx] - np.median(xy[idx], axis=0)
        r = float(np.percentile(np.linalg.norm(z, axis=1), 97)) or 1.0
        out[idx] = z / r * radii[c] + np.array([(i % cols_n) * cell, -(i // cols_n) * cell])
    return out, len(big)


def _normalize(xy: np.ndarray) -> np.ndarray:
    c = xy - np.median(xy, axis=0)
    r = np.percentile(np.linalg.norm(c, axis=1), 97)
    return c / (r if r > 0 else 1.0)


def _contours(xy: np.ndarray, levels: int = 14, lim: float = 1.25, n: int = 160) -> list[dict]:
    """全文書の密度の等高線（折れ線の座標）。"""
    import contourpy

    from ..landscape import _kde_grid

    gx, gy, z = _kde_grid(xy, lim, n=n)
    gen = contourpy.contour_generator(gx, gy, z)
    out = []
    zmax = float(z.max())
    for k, lv in enumerate(np.linspace(zmax * 0.03, zmax * 0.95, levels)):
        paths = [np.round(seg[:: max(1, len(seg) // 120)], 3).tolist() for seg in gen.lines(lv) if len(seg) >= 4]
        if paths:
            out.append({"level": round(k / (levels - 1), 3), "paths": paths})
    return out


def _group_contours(xy: np.ndarray, lim: float = 1.25) -> dict:
    """ある群（出願人・用途）の文書が多い範囲（文書の半分・8 割を含む線）。"""
    import contourpy

    from ..landscape import _kde_grid, _mass_level

    if len(xy) < 3:
        return {"p50": [], "p80": []}
    gx, gy, z = _kde_grid(xy, lim, n=120, bw=0.35)
    gen = contourpy.contour_generator(gx, gy, z)
    res = {}
    for key, mass in (("p50", 0.5), ("p80", 0.8)):
        lv = _mass_level(z, mass)
        res[key] = [np.round(seg, 3).tolist() for seg in gen.lines(lv) if len(seg) >= 4]
    return res


def _topk(S: np.ndarray, k: int = 10) -> list[list[list]]:
    S = np.array(S, dtype=float)
    np.fill_diagonal(S, -np.inf)
    S[np.isnan(S)] = -np.inf
    out = []
    for i in range(len(S)):
        order = np.argsort(-S[i], kind="stable")[:k]
        out.append([[int(j), round(float(S[i, j]), 3)] for j in order if np.isfinite(S[i, j])])
    return out


def run_project(pid: str) -> None:
    """取り込んだ一覧から地図データを作る（別スレッドで動く）。"""
    with _LOCK:
        try:
            _run(pid)
        except Exception as e:  # noqa: BLE001 - 画面にエラーを出すため
            traceback.print_exc()
            _status(pid, "error", 1.0, "計算に失敗しました", f"{type(e).__name__}: {e}")


# 地図の「用途」（色分け・異用途の類似技術）に使う、IPC から用途への対応。
# 実験の評価（patents/usage_eval.IPC_USAGE）の MOF 向けの用途に、食品・飲料・口腔ケアの用途を足したもの（2026-10-25、苦味マスキングのマップで追加）。
# 医薬は化粧品（A61K8）を除く。1 件に複数の用途が付くことがある（例: 機能性飲料の A23L2 と A61K31）。
APP_IPC_USAGE: list[tuple[str, str]] = [
    (r"^B01D53", "ガス分離・ガス精製"),
    (r"^B01D(69|71)", "分離膜"),
    (r"^F17C", "ガス貯蔵"),
    (r"^C01B3/", "水素の製造・貯蔵"),
    (r"^B01J(2[1-9]|3[0-8])", "触媒"),
    (r"^H01M", "電池・燃料電池"),
    (r"^H01G", "キャパシタ"),
    (r"^G01N", "測定・センサ"),
    (r"^A61K(?!8/)|^A61P", "医薬"),
    (r"^A61K8/|^A61Q", "口腔ケア・化粧品"),
    (r"^A61L", "消臭・殺菌"),
    (r"^C02F", "水処理"),
    (r"^H01L|^H10", "電子デバイス"),
    (r"^C12[CGH]", "酒類・ビール"),
    (r"^A23F", "茶・コーヒー"),
    (r"^A23L2/|^A23C", "清涼飲料・乳飲料"),
    (r"^A23K", "飼料・ペットフード"),
    (r"^A23[BDGJPV]|^A23L(?!2/)|^A21D", "食品（飲料以外）"),
]


def _run(pid: str) -> None:
    from ..landscape import cluster_labels
    from ..patents.data import DEFAULT_SECTIONS, load_patent_csv
    from ..patents.extract import build_section_layer
    from ..reduce import align_to
    from ..similarity.graph_sim import RelationConfig, relation_similarity
    from ..similarity.hybrid import combine
    from ..snapshots import usage_labels

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    seed = 42
    _status(pid, "load", 0.03, "一覧を読み込んでいます")
    recs, info = load_patent_csv(d / "source.csv", {"sections": DEFAULT_SECTIONS, "normalize": "NFKC"})
    doc_ids = [r.doc_id for r in recs]
    n = len(recs)
    from . import layers

    use_ext = bool(meta.get("use_extension", True))
    ext = layers.load_extension(d) if use_ext else layers.empty_extension()
    (d / "extension_built.json").write_text(json.dumps(ext, ensure_ascii=False), encoding="utf-8")
    onto = project_ontology(pid, use_ext, built=True)

    _status(pid, "embed", 0.08, f"文埋め込みを計算しています（{n} 件。初回はモデルの読み込みに時間がかかります）")
    from ..patents.vectors import concat_similarity

    S_emb, _, _ = concat_similarity(_encoder(), [r.text for r in recs])

    _status(pid, "relation", 0.45, "課題・解決手段・用途の関係を取り出しています")
    mo = project_modular(pid, use_ext, built=True)
    if mo is not None:
        from ..modular import build_section_layer_scoped

        layer, gstats = build_section_layer_scoped(recs, mo, EXTRACTION, name="dict")
        doc_modules = {r.doc_id: sorted(mo.modules_for(";".join([r.meta.get("ipc", ""), r.meta.get("fi", "")]))) for r in recs}
    else:
        layer, gstats = build_section_layer(recs, onto, EXTRACTION, name="dict")
        doc_modules = {}
    layer.save(d / "graphs")
    _status(pid, "relation", 0.55, "関係どうしの近さを計算しています")
    S_rel, _ = relation_similarity(layer, doc_ids, onto, RelationConfig.from_dict(RELATION))
    S_hyb = combine([S_rel, S_emb], [0.5, 0.5], "rank")
    np.savez_compressed(d / "sims.npz", emb=S_emb.astype(np.float32), rel=S_rel.astype(np.float32), hyb=S_hyb.astype(np.float32))

    concepts = {r.doc_id: set() for r in recs}
    for doc, ms in layer.concepts.items():
        concepts.setdefault(doc, set()).update(m.concept_id for m in ms)
    doc_concepts = [concepts[x] for x in doc_ids]

    methods = {}
    ref = None
    for k, (key, S) in enumerate((("emb", S_emb), ("rel", S_rel), ("hyb", S_hyb))):
        _status(pid, "layout", 0.65 + 0.08 * k, f"「{METHODS[key]}」の配置と等高線を作っています")
        Y = _layout(S, seed)
        if ref is None:
            ref = Y
            Z = Y
        else:
            _, Z, _ = align_to(ref, Y)
        Z, n_islands = _pack_islands(np.asarray(Z), S)
        xy = _normalize(Z)
        labs = cluster_labels(xy, doc_concepts, onto, k=int(min(14, max(5, n // 60))), seed=seed)
        methods[key] = {
            "label": METHODS[key],
            "xy": np.round(xy, 4).tolist(),
            "contours": _contours(xy),
            "labels": [{"x": round(float(c[0]), 3), "y": round(float(c[1]), 3), "lines": lines, "n": int(m)} for c, lines, m in labs],
            "neighbors": _topk(S),
            "islands": n_islands,
        }

    _status(pid, "facets", 0.92, "用途と出願人を整理しています")
    src = pd.read_csv(d / "source.csv", dtype=str, keep_default_na=False).set_index("doc_id").loc[doc_ids]
    tmp = d / "_sections_for_usage.csv"
    src.reset_index()[["doc_id", "use_text"]].to_csv(tmp, index=False)
    u_onto = usage_labels(tmp, onto)
    tmp.unlink(missing_ok=True)
    docs = []
    mlabels = module_labels(meta["ontology"])
    for r in recs:
        row = src.loc[r.doc_id]
        codes = [c.strip() for c in re.split(r"[;；,、]", row.get("ipc", "")) if c.strip()]
        u_ipc = sorted({lab for pat, lab in APP_IPC_USAGE for c in codes if re.search(pat, unicodedata.normalize("NFKC", c).replace(" ", ""))})
        apps = [_short_applicant(a) for a in re.split(r"[;；\n]|、(?=\S)", row.get("applicants", "")) if a.strip()]
        docs.append({
            "id": r.doc_id,
            "title": row.get("title", ""),
            "pub": row.get("pub_number", ""),
            "applicants": apps,
            "year": _year(row.get("filing_date", "")),
            "ipc": r.meta.get("ipc_first", "") or (codes[0] if codes else ""),
            "usage_ipc": u_ipc,
            "usage_onto": sorted(u_onto.get(r.doc_id, set())),
            "modules": [mlabels.get(m, m) for m in doc_modules.get(r.doc_id, [])],
            "snippet": (r.sections.get("abstract") or r.sections.get("problem") or "")[:240],
        })
    usage_key = "usage_ipc" if sum(bool(x["usage_ipc"]) for x in docs) >= n * 0.3 else "usage_onto"

    # 用途をまたぐ「課題と解決手段が近い」組（関係方式の近傍のうち、用途が重ならないもの）
    emb_rank = np.argsort(np.argsort(-np.nan_to_num(S_emb, nan=-1), axis=1), axis=1)
    bridges = []
    Sr = np.nan_to_num(S_rel, nan=-1.0)
    for i, nb in enumerate(methods["rel"]["neighbors"]):
        for j, s in nb:
            if j <= i:
                continue
            ui, uj = set(docs[i][usage_key]), set(docs[j][usage_key])
            if ui and uj and not (ui & uj) and s >= 0.3:
                bridges.append({"a": i, "b": j, "rel": round(float(Sr[i, j]), 3), "emb": round(float(S_emb[i, j]), 3), "emb_rank": int(emb_rank[i, j]), "ua": sorted(ui), "ub": sorted(uj)})
    bridges.sort(key=lambda b: -b["rel"])
    bridges = bridges[:300]

    app_count = Counter(a for x in docs for a in set(x["applicants"]))
    use_count = Counter(u for x in docs for u in x[usage_key])
    years = [x["year"] for x in docs if x["year"]]
    out = {
        "id": pid,
        "name": meta["name"],
        "ontology": meta["ontology"],
        "n_docs": n,
        "usage_source": "IPC（特許分類）" if usage_key == "usage_ipc" else "オントロジーの用途語",
        "usage_key": usage_key,
        "docs": docs,
        "methods": methods,
        "bridges": bridges,
        "facets": {
            "applicants": app_count.most_common(40),
            "usages": use_count.most_common(),
            "years": [min(years), max(years)] if years else None,
            "modules": Counter(m for x in docs for m in x["modules"]).most_common(),
        },
        "ontology_layers": {
            "base": meta["ontology"],
            "base_label": next((o["label"] for o in ontologies() if o["id"] == meta["ontology"]), meta["ontology"]),
            "base_version": onto.version,
            "use_extension": use_ext,
            "extension_version": ext.get("version", 0) if use_ext else None,
            "extension_concepts": len(ext.get("concepts", [])) if use_ext else 0,
            "extension_aliases": len(ext.get("aliases", [])) if use_ext else 0,
            "modular": mo is not None,
        },
        "use_check": _use_check(layer, doc_ids, src, onto),
        "stats": {"load": info, "graph": {k: v for k, v in gstats.items() if isinstance(v, (int, float, str))}, "no_triples": int(sum(1 for x in doc_ids if not layer.triples.get(x)))},
    }
    _write(d / "map.json", out)
    _CACHE.pop(pid, None)
    _status(pid, "done", 1.0, "地図ができました")
    # 地図を先に見せ、オントロジーの語の候補はそのあとで探す（失敗しても地図はそのまま）
    try:
        compute_candidates(pid)
    except Exception:  # noqa: BLE001
        _write(d / "candidates.json", {"state": "error", "error": traceback.format_exc()[-800:], "candidates": []})


def _use_check(layer, doc_ids: list[str], src: pd.DataFrame, onto: Ontology) -> dict:
    """関係の端の用途の概念のうち、用途の文で確かめられる割合（文書ごとに概念を 1 回と数える）。"""
    conf = use_confirmed_concepts(use_sentences(src).reindex(doc_ids).fillna(""), onto)
    n = ok = 0
    for x in doc_ids:
        us = {c for t in layer.triples.get(x, []) for c in (t.subject_id, t.object_id) if c not in onto.top_types and onto.top_type(c) == "U"}
        for c in us:
            n += 1
            ok += use_status(c, conf[x], onto) == "ok"
    has = int((use_sentences(src).reindex(doc_ids).fillna("").str.strip() != "").sum())
    return {"docs_with_sentences": has, "n_docs": len(doc_ids), "use_concepts": n, "confirmed": ok, "rate": round(ok / n, 3) if n else None}


def compute_candidates(pid: str) -> dict:
    """今の拡張層（下書き）を重ねたオントロジーで拾えない語を、候補として candidates.json に書く。"""
    from . import layers

    d = pdir(pid)
    _write(d / "candidates.json", {"state": "computing", "candidates": []})
    ext = layers.load_extension(d)
    onto = project_ontology(pid, True)
    sec = pd.read_csv(d / "source.csv", dtype=str, keep_default_na=False)
    cands = layers.candidates(sec, onto, set(ext.get("ignored", [])) | {c["label"] for c in ext.get("concepts", [])} | {a["alias"] for a in ext.get("aliases", [])})
    layers.suggest_targets(cands, onto, _encoder())
    out = {"state": "done", "computed": time.strftime("%Y-%m-%d %H:%M"), "extension_version": ext.get("version", 0), "candidates": cands}
    _write(d / "candidates.json", out)
    return out


def ontology_state(pid: str) -> dict:
    """オントロジーの画面に出すもの: 共通層・拡張層（下書き）・取りこぼし率・候補・選択肢。"""
    from . import layers

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    sec = pd.read_csv(d / "source.csv", dtype=str, keep_default_na=False)
    base = project_ontology(pid, False)
    draft = project_ontology(pid, True)
    ext = layers.load_extension(d)
    built = read_json(d / "extension_built.json", layers.empty_extension())
    mp = read_json(d / "map.json", {})
    return {
        "name": meta["name"],
        "base": {"id": meta["ontology"], "label": next((o["label"] for o in ontologies() if o["id"] == meta["ontology"]), meta["ontology"]), "version": base.version,
                 "n_concepts": sum(1 for c in base.concepts.values() if c.concept_id not in base.top_types),
                 "modules": _module_summary(pid, mp)},
        "extension": ext,
        "built": {"version": built.get("version", 0), "use_extension": bool(meta.get("use_extension", True)), "layers": mp.get("ontology_layers")},
        "dirty": json.dumps({k: ext.get(k) for k in ("concepts", "aliases")}, sort_keys=True) != json.dumps({k: built.get(k) for k in ("concepts", "aliases")}, sort_keys=True),
        "coverage": {"base": _coverage(pid, sec, base, False), "draft": _coverage(pid, sec, draft, True)},
        "shadowed": layers.shadowed(sec, base, draft),
        "candidates": read_json(d / "candidates.json", {"candidates": []}),
        "common_upgrade": common_upgrade(pid),
        "choices": layers.concept_choices(draft),
        "status": read_json(d / "status.json", {}),
    }


def _coverage(pid: str, sec: pd.DataFrame, onto: Ontology, use_extension: bool) -> dict:
    """取りこぼし率。部品で切り替える共通層なら、地図の計算と同じく文書ごとに部品を切り替えて数える。"""
    from . import layers

    mo = project_modular(pid, use_extension)
    if mo is None:
        return layers.coverage(sec, onto)
    cache: dict[frozenset, layers.Matcher] = {}
    out = {}
    for col, top in layers.SECTION_TOP.items():
        n = zero = 0
        for _, r in sec.iterrows():
            t = layers._norm(r.get(col, ""))
            if not t.strip():
                continue
            act = frozenset(mo.modules_for(";".join([r.get("ipc", ""), r.get("fi", "")])))
            if act not in cache:
                m = layers.Matcher(mo.onto)
                m.index = {k: [(s_, c) for s_, c in v if mo.allowed(s_, c, set(act))] for k, v in m.index.items()}
                cache[act] = m
            n += 1
            zero += not any(mo.onto.top_type(c) == top for _, _, _, c in cache[act].find(t))
        out[col] = {"label": layers.SECTION_LABEL[col], "n": n, "zero": zero, "rate": round(zero / n, 3) if n else None}
    return out


def _module_summary(pid: str, mp: dict) -> list[dict] | None:
    """部品で切り替える共通層なら、部品ごとの概念数と、その部品を使った文書の数。"""
    mo = project_modular(pid, False)
    if mo is None:
        return None
    cnt = Counter(v for k, v in mo.module_of.items() if k not in mo.onto.top_types)
    used = Counter(m for x in mp.get("docs", []) for m in x.get("modules", []))
    labels = module_labels(read_json(pdir(pid) / "meta.json")["ontology"])
    return [{"id": m, "label": labels.get(m, m), "concepts": cnt[m], "docs": used.get(labels.get(m, m), 0),
             "routes": mo.routes.get(m, [])} for m in ["upper", *sorted(mo.routes)]]


def adopt(pid: str, req: dict) -> dict:
    """候補を拡張層（下書き）に入れる。mode = concept（新しい概念）/ alias（既存の概念の別名）/ ignore（無視）。"""
    from . import layers

    d = pdir(pid)
    ext = layers.load_extension(d)
    term = unicodedata.normalize("NFKC", str(req.get("term", ""))).strip()
    if not term:
        raise ValueError("語が空です")
    onto = project_ontology(pid, True)
    mode = req.get("mode")
    if mode == "concept":
        top, parent = req.get("top"), req.get("parent")
        if top not in ("P", "M", "U"):
            raise ValueError("上位型が不正です")
        if parent not in onto.concepts or onto.top_type(parent) != top:
            raise ValueError("置き場所の概念が見つからないか、上位型が合いません")
        mids = {p for c in onto.concepts.values() for p in c.parent_ids}
        if parent not in onto.top_types and parent not in mids:
            # 末端の概念の下に置くと、その概念が中間概念になり本文で拾われなくなるため
            raise ValueError("置き場所には中間概念か上位型を選んでください。末端の概念に足すときは別名にします")
        aliases = [unicodedata.normalize("NFKC", a).strip() for a in req.get("aliases", []) if a and a.strip() and a.strip() != term]
        cid = layers.concept_id(term, top)
        ext["concepts"] = [c for c in ext["concepts"] if c["id"] != cid] + [{"id": cid, "label": term, "aliases": aliases, "parent": parent, "top": top, "added": time.strftime("%Y-%m-%d %H:%M")}]
    elif mode == "alias":
        cid = req.get("concept_id")
        if cid not in onto.concepts or cid in onto.top_types:
            raise ValueError("別名を足す先の概念が見つかりません")
        ext["aliases"] = [a for a in ext["aliases"] if not (a["concept_id"] == cid and a["alias"] == term)] + [{"concept_id": cid, "label": onto.label(cid), "alias": term, "added": time.strftime("%Y-%m-%d %H:%M")}]
    elif mode == "ignore":
        if term not in ext["ignored"]:
            ext["ignored"].append(term)
    else:
        raise ValueError("指定が不正です")
    layers.save_extension(d, ext)
    return ext


def unadopt(pid: str, kind: str, key: str) -> dict:
    """拡張層（下書き）から取り消す。kind = concept（key は概念 ID）/ alias（key は「概念ID\t別名」）/ ignore（key は語）。"""
    from . import layers

    d = pdir(pid)
    ext = layers.load_extension(d)
    if kind == "concept":
        ext["concepts"] = [c for c in ext["concepts"] if c["id"] != key]
        ext["aliases"] = [a for a in ext["aliases"] if a["concept_id"] != key]
    elif kind == "alias":
        cid, _, alias = key.partition("\t")
        ext["aliases"] = [a for a in ext["aliases"] if not (a["concept_id"] == cid and a["alias"] == alias)]
    elif kind == "ignore":
        ext["ignored"] = [t for t in ext["ignored"] if t != key]
    layers.save_extension(d, ext)
    return ext


def common_upgrade(pid: str) -> dict | None:
    """自作の共通層を使うマップで、使っている版より新しい版が発行されていれば、その情報。"""
    from . import common

    meta = read_json(pdir(pid) / "meta.json")
    m = re.fullmatch(r"common:([a-z0-9]{10})@v(\d+)", meta.get("ontology", ""))
    if not m:
        return None
    try:
        cm = common.meta(m.group(1))
    except Exception:  # noqa: BLE001 - 共通層が消された
        return None
    latest = cm["versions"][-1]["version"] if cm["versions"] else 0
    return {"common": m.group(1), "name": cm["name"], "current": int(m.group(2)), "latest": latest,
            "notes": [v for v in cm["versions"] if v["version"] > int(m.group(2))]}


def _upgrade_common(pid: str) -> dict:
    """新しい版に切り替える。拡張層のうち、新しい版の共通層に入った語（格上げされた語）は外して記録する。"""
    from . import common, layers

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    up = common_upgrade(pid)
    if not up or up["latest"] <= up["current"]:
        return {"removed": []}
    new_id = f"common:{up['common']}@v{up['latest']}"
    known = common.surfaces_of(ontology_path(new_id).parent)
    ext = layers.load_extension(d)
    removed = [c["label"] for c in ext["concepts"] if common._key(c["label"]) in known] + [a["alias"] for a in ext["aliases"] if common._key(a["alias"]) in known]
    ext["concepts"] = [c for c in ext["concepts"] if common._key(c["label"]) not in known]
    ext["aliases"] = [a for a in ext["aliases"] if common._key(a["alias"]) not in known]
    ext.setdefault("removed_by_upgrade", []).extend({"term": t, "to": new_id, "at": time.strftime("%Y-%m-%d %H:%M")} for t in removed)
    layers.save_extension(d, ext)
    meta["ontology"] = new_id
    _write(d / "meta.json", meta)
    return {"removed": removed, "ontology": new_id}


def rebuild(pid: str, use_extension: bool, upgrade_common: bool = False) -> None:
    """描き直す。拡張層を使い、前に描いた版から変わっていれば版を上げる。upgrade_common なら共通層を新しい版に切り替える。"""
    from . import layers

    d = pdir(pid)
    if upgrade_common:
        _upgrade_common(pid)
    meta = read_json(d / "meta.json")
    meta["use_extension"] = bool(use_extension)
    _write(d / "meta.json", meta)
    if use_extension:
        # 版の付け方: 下書きが今の版の控え（extension_v{n}.json）と違えば n+1 にして控えを残す
        ext = layers.load_extension(d)
        v = int(ext.get("version", 0))
        snap = read_json(d / f"extension_v{v}.json", layers.empty_extension()) if v else layers.empty_extension()
        key = lambda e: json.dumps({k: e.get(k) for k in ("concepts", "aliases")}, sort_keys=True)  # noqa: E731
        if key(ext) != key(snap):
            ext["version"] = v + 1
            layers.save_extension(d, ext)
            (d / f"extension_v{ext['version']}.json").write_text(json.dumps(ext, ensure_ascii=False, indent=1), encoding="utf-8")
    _status(pid, "queued", 0.0, "計算の順番を待っています")
    threading.Thread(target=run_project, args=(pid,), daemon=True).start()


# ---------- 用途の確認（使い分け: 地図は用途の段落、説明は用途の文で確かめる） ----------
def use_sentences(src: pd.DataFrame) -> pd.Series:
    """用途の文。取り込み時に作っていない古いマップでは、用途の段落から用途の文だけを選び直す。"""
    if "use_sentences" in src.columns:
        return src["use_sentences"].fillna("")
    from ..patents.use_extract import extract_use

    return pd.Series([extract_use(t, ti)[0] for t, ti in zip(src["use_text"], src["title"])], index=src.index)


def use_confirmed_concepts(texts: pd.Series, onto: Ontology) -> dict[str, set[str]]:
    """文書ごとに、用途の文から拾える用途（上位型 U）の概念。"""
    from .layers import Matcher, _norm

    m = Matcher(onto)
    return {doc: {c for _, _, _, c in m.find(_norm(t)) if onto.top_type(c) == "U"} for doc, t in texts.items()}


def use_status(cid: str, confirmed: set[str], onto: Ontology) -> str | None:
    """関係の端の概念が用途なら、用途の文で確かめられるか。同じ概念か、上下の関係（電池 と リチウムイオン二次電池）なら確認済み。"""
    if cid in onto.top_types or onto.top_type(cid) != "U":
        return None
    anc = onto._ancestors
    ok = any(cid in anc[c] or c in anc[cid] for c in confirmed if c not in onto.top_types)
    return "ok" if ok else "weak"


def _triple_use(t, confirmed: set[str], onto: Ontology) -> str | None:
    st = [x for x in (use_status(t.subject_id, confirmed, onto), use_status(t.object_id, confirmed, onto)) if x]
    if not st:
        return None
    return "ok" if all(x == "ok" for x in st) else "weak"


# ---------- 画面からの問い合わせ ----------
def _ctx(pid: str) -> dict:
    """説明の計算に使うもの（三つ組・類似度）を読み込んで覚えておく。"""
    if pid in _CACHE:
        return _CACHE[pid]
    from ..similarity.graph_sim import RelationConfig, TripleIndex

    d = pdir(pid)
    meta = read_json(d / "meta.json")
    mp = read_json(d / "map.json")
    doc_ids = [x["id"] for x in mp["docs"]]
    onto = project_ontology(pid, bool(meta.get("use_extension", True)), built=True)
    layer = GraphLayer.load(d / "graphs", "dict", doc_ids)
    ti = TripleIndex(layer, doc_ids, onto, RelationConfig.from_dict(RELATION))
    df = Counter(c for x in doc_ids for c in {y for t in layer.triples[x] for y in (t.subject_id, t.object_id)})
    sims = dict(np.load(d / "sims.npz"))
    src = pd.read_csv(d / "source.csv", dtype=str, keep_default_na=False).set_index("doc_id")
    conf = use_confirmed_concepts(use_sentences(src).reindex(doc_ids).fillna(""), onto)
    ctx = {"use_conf": conf, "onto": onto, "ti": ti, "ids": doc_ids, "idx": {x: i for i, x in enumerate(doc_ids)}, "generic": {c for c, v in df.items() if v / max(1, len(doc_ids)) > 0.2}, "sims": sims, "map": mp}
    _CACHE[pid] = ctx
    return ctx


def doc_detail(pid: str, doc_id: str) -> dict:
    d = pdir(pid)
    src = pd.read_csv(d / "source.csv", dtype=str, keep_default_na=False).set_index("doc_id")
    if doc_id not in src.index:
        raise ValueError("文書が見つかりません")
    row = src.loc[doc_id].to_dict()
    ctx = _ctx(pid)
    i = ctx["idx"][doc_id]
    lab = ctx["onto"].label
    pl = {p: v.label for p, v in ctx["onto"].predicates.items()}
    a0, a1 = ctx["ti"].offsets[i]
    conf = ctx["use_conf"].get(doc_id, set())
    triples = [{"s": lab(t.subject_id), "p": pl.get(t.predicate, t.predicate), "o": lab(t.object_id), "generic": bool({t.subject_id, t.object_id} & ctx["generic"]),
                "use": _triple_use(t, conf, ctx["onto"])} for t in ctx["ti"].triples[a0:a1]]
    if "use_sentences" not in row:
        row["use_sentences"] = use_sentences(src.loc[[doc_id]].reset_index()).iloc[0]
        row["use_sentences_source"] = "用途の段落から選び直した文"
    return {"id": doc_id, "fields": row, "triples": triples}


def explain(pid: str, a: str, b: str) -> dict:
    """文書 a と b が近い理由: 一致した関係（一般的な概念を含むものは印を付ける）と、各方式の類似度。"""
    ctx = _ctx(pid)
    i, j = ctx["idx"][a], ctx["idx"][b]
    ti, onto = ctx["ti"], ctx["onto"]
    pl = {p: v.label for p, v in onto.predicates.items()}
    score, matches = ti.doc_pair(i, j)
    w = ti.weights
    rows = []
    for x, y, s in sorted(matches, key=lambda m: -m[2] * (w[m[0]] + w[m[1]])):
        if s <= 0:
            continue
        t, u = ti.triples[x], ti.triples[y]
        rows.append({
            "score": round(float(s), 2),
            "a": {"s": onto.label(t.subject_id), "p": pl.get(t.predicate, t.predicate), "o": onto.label(t.object_id)},
            "b": {"s": onto.label(u.subject_id), "p": pl.get(u.predicate, u.predicate), "o": onto.label(u.object_id)},
            "generic": bool({t.subject_id, t.object_id, u.subject_id, u.object_id} & ctx["generic"]),
            # 用途の概念が、それぞれの文書の用途の文で確かめられるか（ok / weak / None = 用途を含まない関係）
            "a_use": _triple_use(t, ctx["use_conf"].get(a, set()), onto),
            "b_use": _triple_use(u, ctx["use_conf"].get(b, set()), onto),
        })
    # 根拠の原文（関係を取り出した文）を、一致した関係ごとに付ける
    by_rank = sorted(matches, key=lambda m: -m[2] * (w[m[0]] + w[m[1]]))
    k = 0
    for x, y, s_ in by_rank:
        if s_ <= 0:
            continue
        t, u = ti.triples[x], ti.triples[y]
        rows[k]["a_ev"] = {"text": (t.evidence_text or "")[:260], "s": t.subject_text, "o": t.object_text}
        rows[k]["b_ev"] = {"text": (u.evidence_text or "")[:260], "s": u.subject_text, "o": u.object_text}
        k += 1
    S = ctx["sims"]
    rank = lambda M: int((np.nan_to_num(M[i], nan=-1) > np.nan_to_num(M[i, j], nan=-1)).sum())  # noqa: E731
    ra, rb = _doc_roles(ctx, i), _doc_roles(ctx, j)
    return {
        "a": a, "b": b,
        "scores": {k: (None if np.isnan(S[k][i, j]) else round(float(S[k][i, j]), 3)) for k in ("emb", "rel", "hyb")},
        "ranks": {k: rank(S[k]) for k in ("emb", "rel", "hyb")},
        "matches": rows[:12],
        "unmatched_a": int(ti.offsets[i][1] - ti.offsets[i][0] - len(rows)),
        **_compare_roles(ctx, a, b, ra, rb, rows),
    }


ROLE_KEYS = (("課題", "problem"), ("手段", "solution"), ("用途", "use"))


def _doc_roles(ctx: dict, i: int) -> dict[str, Counter]:
    """文書 i の関係に出る概念を役割（課題・手段・用途）ごとに数える。一般的な概念も含む（印は呼び出し側で付ける）。"""
    ti = ctx["ti"]
    a0, a1 = ti.offsets[i]
    out = {r: Counter() for r, _ in ROLE_KEYS}
    for t in ti.triples[a0:a1]:
        for cid, role in ((t.subject_id, t.subject_role), (t.object_id, t.object_role)):
            if role in out:
                out[role][cid] += 1
    return out


def _compare_roles(ctx: dict, a: str, b: str, ra: dict, rb: dict, rows: list[dict]) -> dict:
    """役割ごとの共通・相違と、ルールで組み立てる要約文（生成 AI は使わない。根拠が足りなければ推測しない）。"""
    onto, gen = ctx["onto"], ctx["generic"]
    lab = onto.label
    conf_a, conf_b = ctx["use_conf"].get(a, set()), ctx["use_conf"].get(b, set())
    shared, only_a, only_b = {}, {}, {}
    for role, key in ROLE_KEYS:
        A = [c for c, _ in ra[role].most_common() if c not in gen]
        B = [c for c, _ in rb[role].most_common() if c not in gen]
        shared[key] = [lab(c) for c in A if c in set(B)][:4]
        only_a[key] = [lab(c) for c in A if c not in set(B)][:5]
        only_b[key] = [lab(c) for c in B if c not in set(A)][:5]
    P, M, U = shared["problem"], shared["solution"], shared["use"]
    j = lambda xs: "・".join(f"「{x}」" for x in xs[:2])  # noqa: E731
    sents = []
    if P and M:
        sents.append(f"両文書は{j(P)}を課題とし、{j(M)}を解決手段に用いる点が共通しています。")
    elif P:
        sents.append(f"両文書は{j(P)}を課題とする点が共通しています（解決手段の概念は一致していません）。")
    elif M:
        sents.append(f"両文書は{j(M)}を解決手段に用いる点が共通しています（課題の概念は一致していません）。")
    elif any(r["score"] > 0 and not r["generic"] for r in rows):
        sents.append("共通する技術関係はありますが、同じ概念どうしの一致ではなく、似た概念どうしの一致です。下の一致した技術関係で確かめてください。")
    elif rows:
        g = sorted({lab(c) for c in gen if any(c in (ra[r] | rb[r]) for r, _ in ROLE_KEYS)})[:2]
        sents.append(f"一致した関係は、どの文書にも出る一般的な概念{('（' + '・'.join(g) + '）') if g else ''}を含むものだけです。具体的な一致内容を説明するには根拠が不足しています。")
    else:
        sents.append("技術関係の一致はありません。意味の類似性で近いと判定されています。")
    ua = [c for c, _ in ra["用途"].most_common() if c not in gen]
    ub = [c for c, _ in rb["用途"].most_common() if c not in gen]
    if U:
        sents.append(f"用途（{j(U)}）も共通です。")
    elif ua and ub:
        sents.append(f"用途は異なります（この特許: {j([lab(c) for c in ua])}／相手: {j([lab(c) for c in ub])}）。")
    elif ua:
        sents.append(f"用途はこの特許だけで抽出されています（{j([lab(c) for c in ua])}）。相手の用途は抽出されていません。")
    elif ub:
        sents.append(f"用途は相手だけで抽出されています（{j([lab(c) for c in ub])}）。この特許の用途は抽出されていません。")
    else:
        sents.append("用途は、どちらの特許でも抽出されていません。")
    weak = [lab(c) for c in ua + ub if use_status(c, conf_a if c in ua else conf_b, onto) == "weak"]
    if weak:
        sents.append(f"用途のうち {j(sorted(set(weak)))} は用途を述べた文に無く、周辺の段落から抽出したものです（要確認）。")
    return {"summary": "".join(sents), "shared": shared, "only_a": only_a, "only_b": only_b}


_IPC_MAIN = re.compile(r"^([A-H]\d{2}[A-Z])\s*(\d+)")


def stats(pid: str) -> dict:
    """統計分析の画面に渡す、文書ごとの属性と辞書の概念（集計は画面側で行い、絞り込みに追従させる）。
    概念は文書の技術関係（三つ組）に出たものを、役割（課題・解決手段・用途）ごとに持つ。"""
    ctx = _ctx(pid)
    mp, onto, gen = ctx["map"], ctx["onto"], ctx["generic"]
    src = pd.read_csv(pdir(pid) / "source.csv", dtype=str, keep_default_na=False).set_index("doc_id")
    docs, used = [], set()
    for i, d in enumerate(mp["docs"]):
        roles = _doc_roles(ctx, i)
        rec = {k: sorted(roles[r]) for r, k in ROLE_KEYS}
        for v in rec.values():
            used.update(v)
        codes = [unicodedata.normalize("NFKC", c).replace(" ", "") for c in re.split(r"[;；,、]", src.at[d["id"], "ipc"] if d["id"] in src.index and "ipc" in src.columns else "") if c.strip()]
        mains = sorted({m.group(1) + m.group(2) for c in codes if (m := _IPC_MAIN.match(c))})
        docs.append({"id": d["id"], "title": d["title"], "pub": d.get("pub", ""), "year": d.get("year"), "applicants": d.get("applicants", []),
                     "usage": d.get("usage_ipc", []), "ipc": mains, **rec})
    concepts = {c: {"label": onto.label(c), "top": onto.top_type(c), "generic": c in gen} for c in sorted(used)}
    return {"id": pid, "name": mp.get("name", ""), "n": len(docs), "docs": docs, "concepts": concepts,
            "ontology": mp.get("ontology_layers", {}).get("base_label", "")}


def bridges_detail(pid: str, limit: int = 60) -> list[dict]:
    """異用途の類似技術: 関係の近さが高いのに用途が重ならない組に、共通の課題・解決手段と用途の確認状態を付ける。"""
    ctx = _ctx(pid)
    mp = ctx["map"]
    onto, gen = ctx["onto"], ctx["generic"]
    out = []
    for br in mp.get("bridges", [])[:limit]:
        i, j = br["a"], br["b"]
        a, b = mp["docs"][i]["id"], mp["docs"][j]["id"]
        ra, rb = _doc_roles(ctx, i), _doc_roles(ctx, j)
        sh = {key: [onto.label(c) for c in ra[role] if c in rb[role] and c not in gen][:3] for role, key in ROLE_KEYS}
        ua = [c for c in ra["用途"] if c not in gen]
        ub = [c for c in rb["用途"] if c not in gen]
        conf = [use_status(c, ctx["use_conf"].get(a, set()), onto) for c in ua] + [use_status(c, ctx["use_conf"].get(b, set()), onto) for c in ub]
        out.append({**br, "shared": sh, "use_a": [onto.label(c) for c in ua][:3], "use_b": [onto.label(c) for c in ub][:3],
                    "use_status": "ok" if conf and all(x == "ok" for x in conf) else ("weak" if "weak" in conf else None)})
    return out


def overlay(pid: str, method: str, group_by: str, groups: list[str]) -> list[dict]:
    """出願人・用途ごとの範囲と、出願年に沿った重心の軌跡。"""
    from ..landscape import _smooth, trajectory

    mp = _ctx(pid)["map"]
    xy = np.array(mp["methods"][method]["xy"])
    years = np.array([x["year"] or 0 for x in mp["docs"]], dtype=float)
    out = []
    for g in groups[:3]:
        if group_by == "applicant":
            member = np.array([g in x["applicants"] for x in mp["docs"]])
        elif group_by == "module":
            member = np.array([g in x.get("modules", []) for x in mp["docs"]])
        else:
            member = np.array([g in x[mp["usage_key"]] for x in mp["docs"]])
        ok = member & (years > 0)
        res = {"group": g, "n": int(member.sum()), **_group_contours(xy[member])}
        if ok.sum() >= 4:
            pts, labs = trajectory(xy[ok], years[ok])
            res["trajectory"] = np.round(_smooth(pts), 3).tolist()
            res["points"] = np.round(pts, 3).tolist()
            res["point_labels"] = labs
        out.append(res)
    return out
