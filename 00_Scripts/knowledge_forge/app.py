from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import services
from .core_processing import CoreTextProcessor
from .db import DATA_DIR, DB_PATH
from .enhancement import KnowledgeEnhancementQueue, OfflineEnhancementAdapter
from .ingestion import PersistentTextImportQueue, RecycledDuplicateError
from .recycle_bin import KnowledgeRecycleBin


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="知识炼制台", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


enhancement_queue = KnowledgeEnhancementQueue(
    DB_PATH,
    services.ARTIFACT_DIR,
    OfflineEnhancementAdapter(),
)
recycle_bin = KnowledgeRecycleBin(
    DB_PATH,
    (
        services.UPLOAD_DIR,
        services.ARTIFACT_DIR,
        Path(services.load_config()["paths"]["std_dir"]),
    ),
)
core_text_processor = CoreTextProcessor(
    DB_PATH,
    Path(services.load_config()["paths"]["std_dir"]),
)
ingestion_queue = PersistentTextImportQueue(
    DB_PATH,
    services.ingest_upload,
    core_text_processor.process,
    completion_callback=enhancement_queue.enqueue_for_task,
    task_settled_callback=recycle_bin.finalize_pending,
)


def import_job_view(task):
    progress = {
        "waiting": 5,
        "processing": 50,
        "completed": 100,
        "needs_attention": 100,
    }.get(task.status, 0)
    stage_progress = {
        "extract_text": 15,
        "standard_document": 35,
        "quality_validation": 55,
        "chunk_indexing": 75,
        "promote_version": 90,
    }
    stage_labels = {
        "processing": "准备处理",
        "extract_text": "提取文本",
        "standard_document": "生成标准知识文档",
        "quality_validation": "检查文档完整性",
        "chunk_indexing": "生成检索分块",
        "promote_version": "切换当前版本",
    }
    return {
        **task.__dict__,
        "file_name": task.filename,
        "step": task.current_stage,
        "progress": stage_progress.get(task.current_stage, progress),
        "failed_stage_display": stage_labels.get(task.failed_stage, task.failed_stage),
        "next_attempt_display": (
            datetime.fromtimestamp(task.next_attempt_at).strftime("%H:%M:%S")
            if task.next_attempt_at is not None
            else None
        ),
    }


@app.on_event("startup")
def startup() -> None:
    services.seed_defaults()
    ingestion_queue.start()
    recycle_bin.finalize_pending()
    recycle_bin.start()
    enhancement_queue.start()


@app.on_event("shutdown")
def shutdown() -> None:
    recycle_bin.stop()
    enhancement_queue.stop()
    ingestion_queue.stop()


def ctx(request: Request, active: str, **kwargs):
    base = {"request": request, "active": active, "data_dir": DATA_DIR}
    base.update(kwargs)
    return base


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", ctx(request, "dashboard", **services.dashboard_stats()))


@app.post("/scan")
def scan():
    services.scan_existing_library()
    return RedirectResponse("/", status_code=303)


@app.get("/ingest", response_class=HTMLResponse)
def ingest(request: Request):
    jobs = [import_job_view(task) for task in ingestion_queue.list_tasks()]
    return templates.TemplateResponse(
        request,
        "ingest.html",
        ctx(request, "ingest", jobs=jobs, job_summary=ingestion_queue.summary()),
    )


@app.get("/ingest/jobs", response_class=HTMLResponse)
def ingest_jobs(request: Request):
    jobs = [import_job_view(task) for task in ingestion_queue.list_tasks()]
    return templates.TemplateResponse(
        request,
        "_job_queue.html",
        {"request": request, "jobs": jobs, "job_summary": ingestion_queue.summary()},
    )


@app.post("/ingest/upload")
async def upload(files: list[UploadFile] = File(...)):
    uploads = []
    for uploaded in files:
        data = await uploaded.read()
        uploads.append((uploaded.filename or "upload.txt", data))
    try:
        submitted = ingestion_queue.submit_many(uploads)
    except RecycledDuplicateError as exc:
        return RedirectResponse(f"/recycle-bin?duplicate_source={exc.source_id}", status_code=303)
    if len(submitted) == 1:
        existing_file_id = ingestion_queue.knowledge_entry_for_task(submitted[0].id)
        if existing_file_id is not None:
            return RedirectResponse(f"/files/{existing_file_id}", status_code=303)
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/tasks/{task_id}/pause")
def pause_import(task_id: int):
    try:
        ingestion_queue.pause(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Import task not found")
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/tasks/{task_id}/resume")
def resume_import(task_id: int):
    try:
        ingestion_queue.resume(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Import task not found")
    return RedirectResponse("/ingest", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", category: str = "", tag: str = "", status: str = "", artifact_type: str = "", selected: int = 0):
    selected_detail = services.file_detail(selected) if selected else None
    return templates.TemplateResponse(
        request,
        "library.html",
        ctx(
            request,
            "library",
            files=services.list_files(q=q, category=category, tag=tag, status=status, artifact_type=artifact_type),
            categories=services.list_category_options(),
            tag_groups=services.tag_picker_groups(),
            q=q,
            category=category,
            tag=tag,
            status=status,
            artifact_type=artifact_type,
            selected=selected,
            selected_detail=selected_detail,
        ),
    )


@app.get("/files/{file_id}", response_class=HTMLResponse)
def detail(request: Request, file_id: int):
    data = services.file_detail(file_id)
    if not data:
        raise HTTPException(status_code=404, detail="File not found")
    data["version_history"] = ingestion_queue.version_history_for_file(file_id)
    current = next((item for item in data["version_history"] if item["is_current"]), None)
    data["enhancement_jobs"] = (
        enhancement_queue.list_jobs(current["id"])
        if current is not None and current.get("id") is not None
        else []
    )
    data["source_id"] = recycle_bin.source_id_for_file(file_id)
    return templates.TemplateResponse(request, "file_detail.html", ctx(request, "library", **data))


@app.post("/files/{file_id}/recycle")
def recycle_file(file_id: int):
    source_id = recycle_bin.source_id_for_file(file_id)
    if source_id is None:
        raise HTTPException(status_code=404, detail="知识资料不存在")
    recycle_bin.recycle(source_id)
    return RedirectResponse("/recycle-bin", status_code=303)


@app.get("/recycle-bin", response_class=HTMLResponse)
def recycled_sources(request: Request, duplicate_source: int = 0):
    recycle_bin.finalize_pending()
    recycle_bin.purge_expired()
    return templates.TemplateResponse(
        request,
        "recycle_bin.html",
        ctx(
            request,
            "recycle-bin",
            sources=recycle_bin.list_recycled(),
            duplicate_source=duplicate_source,
        ),
    )


@app.post("/recycle-bin/{source_id}/restore")
def restore_source(source_id: int):
    try:
        recycle_bin.restore(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="回收站中没有这份知识资料")
    except ValueError:
        raise HTTPException(status_code=410, detail="知识资料已超过 30 天保留期限")
    return RedirectResponse("/recycle-bin", status_code=303)


@app.post("/files/{file_id}/tags")
def update_tag(file_id: int, tag: str = Form(...), action: str = Form("add")):
    services.update_file_tag(file_id, tag, action)
    return RedirectResponse(f"/files/{file_id}", status_code=303)


@app.post("/files/{file_id}/regenerate")
def regenerate_file(file_id: int):
    history = ingestion_queue.version_history_for_file(file_id)
    current = next((item for item in history if item["is_current"]), None)
    if current is None:
        raise HTTPException(status_code=404, detail="Current version not found")
    for kind in ("structure", "sop", "insight"):
        enhancement_queue.regenerate(current["id"], kind)
    return RedirectResponse(f"/files/{file_id}", status_code=303)


@app.post("/files/{file_id}/enhancements/{kind}/regenerate")
def regenerate_enhancement(file_id: int, kind: str):
    history = ingestion_queue.version_history_for_file(file_id)
    current = next((item for item in history if item["is_current"]), None)
    if current is None:
        raise HTTPException(status_code=404, detail="Current version not found")
    try:
        enhancement_queue.regenerate(current["id"], kind)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="Enhancement not found")
    return RedirectResponse(f"/files/{file_id}", status_code=303)


@app.get("/packs", response_class=HTMLResponse)
def packs(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request,
        "packs.html",
        ctx(request, "packs", packs=services.list_packs(), tag_groups=services.tag_picker_groups(), error=error),
    )


@app.post("/packs/{pack_id}/export")
def export_pack(pack_id: int, include_low_confidence: bool = Form(False)):
    path = services.export_pack(pack_id, "zip", include_low_confidence)
    return RedirectResponse(f"/download?path={path}", status_code=303)


@app.post("/packs/{pack_id}/delete")
def delete_pack(pack_id: int):
    if not services.delete_pack(pack_id):
        raise HTTPException(status_code=404, detail="Pack not found")
    return RedirectResponse("/packs", status_code=303)


@app.post("/packs/{pack_id}/save")
def save_pack(
    pack_id: int,
    name: str = Form(...),
    description: str = Form(""),
    include_tags: str = Form(""),
    selected_tags: list[str] = Form([]),
    min_confidence: float = Form(0.7),
    include_sop: bool = Form(False),
    include_insight: bool = Form(False),
    include_source: bool = Form(False),
):
    tag_text = "\n".join([include_tags, *selected_tags])
    services.save_pack_recipe(pack_id, name, description, tag_text, min_confidence, include_sop, include_insight, include_source)
    return RedirectResponse("/packs", status_code=303)


@app.post("/packs/create")
def create_pack(
    name: str = Form(""),
    description: str = Form(""),
    include_tags: str = Form(""),
    selected_tags: list[str] = Form([]),
    min_confidence: float = Form(0.7),
    include_sop: bool = Form(False),
    include_insight: bool = Form(False),
    include_source: bool = Form(False),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/packs?error=missing_name", status_code=303)
    tag_text = "\n".join([include_tags, *selected_tags])
    services.save_pack_recipe(None, name, description, tag_text, min_confidence, include_sop, include_insight, include_source)
    return RedirectResponse("/packs", status_code=303)


@app.get("/download")
def download(path: str):
    target = Path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(str(target), filename=target.name)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", ctx(request, "settings", **services.settings_data()))


@app.post("/settings/prompts")
def save_prompt(name: str = Form(...), content: str = Form(...)):
    services.create_prompt_version(name, content)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/values")
def save_setting(key: str = Form(...), value: str = Form("")):
    services.update_setting(key, value)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/tags")
def save_tag(name: str = Form(...), description: str = Form("")):
    services.create_or_update_tag(name, description)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/tags/{tag_id}")
def edit_tag(tag_id: int, name: str = Form(...), description: str = Form(""), action: str = Form("save")):
    if action == "delete":
        services.delete_unused_tag(tag_id)
    else:
        services.rename_tag(tag_id, name, description)
    return RedirectResponse("/settings", status_code=303)


@app.get("/api/files")
def api_files(q: str = "", category: str = "", tag: str = "", status: str = "", artifact_type: str = ""):
    return [dict(row) for row in services.list_files(q=q, category=category, tag=tag, status=status, artifact_type=artifact_type, limit=200)]


@app.get("/api/packs")
def api_packs():
    return [
        {"pack": dict(item["pack"]), "recipe": item["recipe"], "file_count": item["file_count"]}
        for item in services.list_packs()
    ]


def main() -> None:
    import uvicorn

    port = int(os.environ.get("KNOWLEDGE_FORGE_PORT", "8765"))
    uvicorn.run("knowledge_forge.app:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
