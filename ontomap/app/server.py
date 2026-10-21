"""特許俯瞰マップ アプリの API と画面配信（FastAPI）。手元の PC（127.0.0.1）でだけ動かす。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engine

WEB = Path(__file__).parent / "web"
app = FastAPI(title="OntoMap")


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """画面のファイル（HTML・JS・CSS）は毎回確かめさせる。アプリを更新したあとに古い画面が残らないように。"""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


class CreateReq(BaseModel):
    name: str
    upload_id: str
    mapping: dict[str, str]
    ontology: str


@app.get("/api/health")
def health() -> dict:
    return {"app": "ontomap", "home": str(engine.home())}


@app.get("/api/ontologies")
def get_ontologies() -> list[dict]:
    return engine.ontologies()


@app.post("/api/ontologies")
async def post_ontology(file: UploadFile = File(...)) -> dict:
    try:
        return engine.add_ontology(file.filename or "ontology.yaml", await file.read())
    except ValueError as e:
        raise _bad(e) from e


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "ファイルが大きすぎます（200MB まで）")
    uid = engine.save_upload(file.filename or "data.csv", data)
    try:
        return engine.preview(uid)
    except ValueError as e:
        raise _bad(e) from e


@app.get("/api/projects")
def projects() -> list[dict]:
    return engine.list_projects()


@app.post("/api/projects")
def create(req: CreateReq) -> dict:
    try:
        return {"id": engine.create_project(req.name, req.upload_id, req.mapping, req.ontology)}
    except ValueError as e:
        raise _bad(e) from e


@app.get("/api/projects/{pid}/status")
def status(pid: str) -> dict:
    return engine.read_json(engine.pdir(pid) / "status.json", {"stage": "unknown"})


@app.post("/api/projects/{pid}/rerun")
def rerun(pid: str) -> dict:
    import threading

    engine._status(pid, "queued", 0.0, "計算の順番を待っています")
    threading.Thread(target=engine.run_project, args=(pid,), daemon=True).start()
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def delete(pid: str) -> dict:
    engine.delete_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/map")
def get_map(pid: str):
    p = engine.pdir(pid) / "map.json"
    if not p.exists():
        raise HTTPException(404, "地図はまだできていません")
    return FileResponse(p, media_type="application/json")


@app.get("/api/projects/{pid}/docs/{doc_id}")
def doc(pid: str, doc_id: str) -> dict:
    try:
        return engine.doc_detail(pid, doc_id)
    except (ValueError, KeyError) as e:
        raise _bad(e) from e


@app.get("/api/projects/{pid}/explain")
def explain(pid: str, a: str, b: str) -> dict:
    try:
        return engine.explain(pid, a, b)
    except (ValueError, KeyError) as e:
        raise _bad(e) from e


@app.get("/api/projects/{pid}/overlay")
def overlay(pid: str, method: str, group_by: str, groups: str) -> JSONResponse:
    if method not in engine.METHODS or group_by not in ("applicant", "usage"):
        raise HTTPException(400, "指定が不正です")
    return JSONResponse(engine.overlay(pid, method, group_by, [g for g in groups.split("\t") if g]))


class AdoptReq(BaseModel):
    term: str
    mode: str
    top: str | None = None
    parent: str | None = None
    concept_id: str | None = None
    aliases: list[str] = []


class RebuildReq(BaseModel):
    use_extension: bool = True
    upgrade_common: bool = False


@app.get("/api/projects/{pid}/ontology")
def ontology_state(pid: str) -> dict:
    return engine.ontology_state(pid)


@app.post("/api/projects/{pid}/ontology/adopt")
def ontology_adopt(pid: str, req: AdoptReq) -> dict:
    try:
        return engine.adopt(pid, req.model_dump())
    except ValueError as e:
        raise _bad(e) from e


@app.post("/api/projects/{pid}/ontology/unadopt")
def ontology_unadopt(pid: str, kind: str, key: str) -> dict:
    return engine.unadopt(pid, kind, key)


@app.post("/api/projects/{pid}/ontology/candidates")
def ontology_candidates(pid: str) -> dict:
    return engine.compute_candidates(pid)


@app.post("/api/projects/{pid}/rebuild")
def rebuild(pid: str, req: RebuildReq) -> dict:
    engine.rebuild(pid, req.use_extension, req.upgrade_common)
    return {"ok": True}


@app.get("/api/projects/{pid}/bridges")
def bridges(pid: str, limit: int = 60) -> list:
    return engine.bridges_detail(pid, limit)


# ---------- 共通層（辞書）を育てる ----------
from . import common  # noqa: E402


class CommonCreateReq(BaseModel):
    source: str
    name: str = ""


def _call(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise _bad(e) from e


@app.get("/api/common")
def common_list() -> list:
    return common.list_common()


@app.post("/api/common")
def common_create(req: CommonCreateReq) -> dict:
    return _call(common.create, req.source, req.name)


@app.get("/api/common/{cid}")
def common_state(cid: str) -> dict:
    return _call(common.state, cid)


@app.delete("/api/common/{cid}")
def common_delete(cid: str) -> dict:
    _call(common.delete, cid)
    return {"ok": True}


@app.get("/api/common/{cid}/concepts")
def common_concepts(cid: str, module: str = "", q: str = "") -> list:
    return _call(common.concepts, cid, module, q)


@app.post("/api/common/{cid}/concept")
def common_concept(cid: str, req: dict) -> dict:
    return _call(common.edit_concept, cid, req)


@app.post("/api/common/{cid}/module")
def common_module(cid: str, req: dict) -> dict:
    return _call(common.set_module, cid, req)


@app.post("/api/common/{cid}/scope")
def common_scope(cid: str, req: dict) -> dict:
    return _call(common.set_scope, cid, req)


@app.get("/api/common/{cid}/promotions")
def common_promotions(cid: str) -> list:
    return _call(common.promotions, cid)


@app.post("/api/common/{cid}/promote")
def common_promote(cid: str, req: dict) -> dict:
    return _call(common.promote, cid, req)


@app.get("/api/common/{cid}/corpus")
def common_corpus(cid: str, project: str, module: str = "") -> dict:
    return _call(common.corpus_candidates, cid, project, module)


class PromptReq(BaseModel):
    field: str = ""
    modules: list[dict] = []
    n_concepts: int = 200
    sample: str = ""


@app.post("/api/common_prompt")
def common_prompt(req: PromptReq) -> dict:
    return {"prompt": common.build_prompt(req.field, req.modules, req.n_concepts, req.sample)}


@app.get("/api/common_template.csv")
def common_template() -> Response:
    body = "型,分野,中間概念,概念,別名\n課題,,安全性の課題,内部短絡,短絡|ショート\n解決手段,,電池の部材,セパレータ,セパレーター|隔離膜\n用途,,機器・移動体,電気自動車,電動車両|ハイブリッド自動車\n"
    return Response(("\ufeff" + body).encode("utf-8"), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=common_layer_template.csv"})


@app.post("/api/common_import")
async def common_import(file: UploadFile = File(...), name: str = "", cid: str = "") -> dict:
    data = await file.read()
    return _call(common.import_csv, data, name, file.filename or "", cid or None)


@app.get("/api/common/{cid}/export.csv")
def common_export(cid: str) -> Response:
    body = _call(common.export_csv, cid)
    return Response(("\ufeff" + body).encode("utf-8"), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=common_{cid}.csv"})


@app.post("/api/common/{cid}/rename")
def common_rename(cid: str, req: dict) -> dict:
    return _call(common.rename, cid, req.get("name", ""))


@app.get("/api/common/{cid}/trial")
def common_trial(cid: str, project: str) -> dict:
    return _call(common.trial, cid, project)


@app.post("/api/common/{cid}/publish")
def common_publish(cid: str, req: dict) -> dict:
    return _call(common.publish, cid, req.get("note", ""))


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
