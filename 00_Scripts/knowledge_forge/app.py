from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import services
from .db import DATA_DIR, DB_PATH, connect
from .ingestion import PersistentTextImportQueue


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="知识炼制台", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def process_text_upload(file_id: int) -> None:
    services.process_upload(file_id)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status, error FROM jobs
            WHERE file_id=? AND job_type IN ('ingest_upload', 'process_upload')
            ORDER BY id DESC LIMIT 1
            """,
            (file_id,),
        ).fetchone()
    if row is not None and row["status"] == "failed":
        raise RuntimeError(row["error"] or "Text import failed")


ingestion_queue = PersistentTextImportQueue(
    DB_PATH,
    services.ingest_upload,
    process_text_upload,
)


def import_job_view(task):
    progress = {
        "waiting": 5,
        "processing": 50,
        "completed": 100,
        "needs_attention": 100,
    }.get(task.status, 0)
    return {
        **task.__dict__,
        "file_name": task.filename,
        "step": task.current_stage,
        "progress": progress,
    }


@app.on_event("startup")
def startup() -> None:
    services.seed_defaults()
    ingestion_queue.start()


@app.on_event("shutdown")
def shutdown() -> None:
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
    ingestion_queue.submit_many(uploads)
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
    return templates.TemplateResponse(request, "file_detail.html", ctx(request, "library", **data))


@app.post("/files/{file_id}/tags")
def update_tag(file_id: int, tag: str = Form(...), action: str = Form("add")):
    services.update_file_tag(file_id, tag, action)
    return RedirectResponse(f"/files/{file_id}", status_code=303)


@app.post("/files/{file_id}/regenerate")
def regenerate_file(file_id: int):
    services.regenerate_file_artifacts(file_id)
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
