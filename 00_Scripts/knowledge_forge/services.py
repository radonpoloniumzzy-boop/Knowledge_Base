from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from markitdown import MarkItDown

from .db import DATA_DIR, ROOT_DIR, connect, init_db


CONFIG_PATH = ROOT_DIR / "00_Scripts" / "config.json"
PACK_DIR = DATA_DIR / "Knowledge_Packs"
UPLOAD_DIR = DATA_DIR / "Uploads"
ARTIFACT_DIR = DATA_DIR / "Artifacts"


ROOT_LABELS = {
    "00_Unsorted": "未归类",
    "00_Pending_Review": "待复核",
    "01_Finance": "金融",
    "01_Charlie_Munger": "查理芒格",
    "01_Warren_Buffett": "沃伦巴菲特",
    "01_易经智慧": "易经智慧",
    "02_Media": "传媒",
    "03_Sales": "销售",
    "04_Coding": "编程",
    "05_Ops": "企业管理",
    "06_Emotion": "情绪关系",
    "07_Law": "法律合规",
    "08_Art": "艺术",
    "99_General": "通用知识",
}


SUPPORTED_EXTS = {
    ".md",
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".html",
    ".csv",
    ".json",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
}


DEFAULT_PROMPTS = {
    "clean_transcript": """清洗学习资料转写稿。去除口癖、重复寒暄、无意义语气词，保留知识点、案例和步骤。输出 Markdown。""",
    "structure_note": """把清洗后的学习资料整理为结构化笔记：主题、适用场景、核心概念、步骤、误区、案例、可复用原则、来源。""",
    "sop_generator": """从结构化笔记中提取可执行 SOP。若资料纯理论则说明不可生成 SOP。输出 Markdown 检查清单。""",
    "insight_generator": """从结构化笔记中提取 Insight：关键事实、逻辑、模型、判断、证据和可迁移结论。输出 Markdown。""",
    "taxonomy_classifier": """判断资料主分类、强标签、弱标签。文件级强标签必须代表主要内容；一句话相关只能作为片段级弱标签。""",
}


DEFAULT_PACKS = [
    {
        "name": "画家知识包",
        "description": "面向绘画、视觉训练和基础造型能力的知识组合。",
        "tags": [
            "08_Art/AI绘画创作体系",
            "08_Art/平面构成法则",
            "08_Art/光影色彩机制",
            "08_Art/人体结构",
            "08_Art/透视结构体系",
            "08_Art/场景空间",
            "剪辑摄影/一级调色与节点",
            "剪辑摄影/一级调色与节点逻辑",
            "剪辑摄影/白平衡与色温调节",
            "剪辑摄影/实战布光与构图",
        ],
    },
    {
        "name": "营销型画家知识包",
        "description": "画家能力 + 内容运营、用户心理、销售表达。",
        "tags": [
            "08_Art/AI绘画创作体系",
            "08_Art/平面构成法则",
            "02_Media/爆款内容生产",
            "02_Media/账号定位体系",
            "02_Media/商业变现模式",
            "03_Sales/销售实战技法",
            "03_Sales/沟通说服逻辑",
        ],
    },
    {
        "name": "金融风控知识包",
        "description": "面向金融风险识别、合规和周期判断。",
        "tags": [
            "01_Finance/交易风控系统",
            "01_Finance/基本面投研体系",
            "01_Finance/技术分析实战",
            "07_Law/金融行业合规监管",
            "07_Law/证券从业职业道德",
            "05_Ops/阿米巴经营体系",
            "05_Ops/绩效管理工具",
        ],
    },
]


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def normalize_title(path: Path) -> str:
    return path.stem.replace("_原文", "").strip() or path.name


def clean_text(text: str) -> str:
    text = re.sub(r"# >>> 来源:.*?<<<", "", text, flags=re.DOTALL)
    text = re.sub(r"> 🔗 Source:.*?(\n|$)", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(\d{1,2}:)?\d{1,2}:\d{2}\]|\(\d{1,2}:\d{2}\)", "", text)
    text = re.sub(r"(嗯|呃|啊|这个|那个)[，,、 ]{0,2}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def display_root_label(segment: str) -> str:
    is_intel = segment.endswith("_Intel")
    base = segment.removesuffix("_Intel")
    label = ROOT_LABELS.get(base)
    if label is None:
        match = re.match(r"^\d+_(.+)$", base)
        label = match.group(1).replace("_", " ") if match else base.replace("_", " ")
    if is_intel:
        label = f"{label} Insight"
    return label


def display_tag_label(tag_name: str) -> str:
    parts = [part for part in tag_name.split("/") if part]
    if not parts:
        return tag_name
    labels = [display_root_label(parts[0])]
    labels.extend(part.replace("_", " ") for part in parts[1:])
    return " / ".join(labels)


def display_tag_leaf(tag_name: str) -> str:
    parts = [part for part in tag_name.split("/") if part]
    if not parts:
        return tag_name
    if len(parts) == 1:
        return display_root_label(parts[0])
    if len(parts) <= 3:
        return " / ".join(part.replace("_", " ") for part in parts[1:])
    return " / ".join([parts[1].replace("_", " "), parts[2].replace("_", " "), "..."])


def chunk_text(text: str, max_chars: int = 1800) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current.strip())
    return chunks or ([text[:max_chars]] if text else [])


def active_prompt_version(conn, name: str) -> str:
    row = conn.execute(
        "SELECT version FROM prompts WHERE name=? AND active=1 ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    return row["version"] if row else "v1"


def first_sentences(text: str, limit: int = 5) -> list[str]:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return []
    pieces = re.split(r"(?<=[。！？.!?])\s*", compact)
    return [p.strip() for p in pieces if len(p.strip()) > 8][:limit]


def key_lines(text: str, limit: int = 10) -> list[str]:
    candidates = []
    for raw in re.split(r"\n+", text):
        line = raw.strip(" -*#\t")
        if 14 <= len(line) <= 120:
            candidates.append(line)
    if len(candidates) < limit:
        candidates.extend(first_sentences(text, limit * 2))
    seen = set()
    output = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            output.append(item)
        if len(output) >= limit:
            break
    return output


def build_structure_note(title: str, text: str) -> str:
    lines = key_lines(text, 10)
    summary = first_sentences(text, 3)
    return "\n".join(
        [
            f"# 结构化笔记：{title}",
            "",
            "## 主题摘要",
            *(f"- {item}" for item in (summary or ["待补充摘要。"])),
            "",
            "## 核心要点",
            *(f"- {item}" for item in (lines[:6] or ["待补充要点。"])),
            "",
            "## 适用场景",
            "- 作为上传资料的离线结构化草稿，可在接入 AI 后重新生成更精细版本。",
            "",
            "## 案例与证据",
            *(f"- {item}" for item in (lines[6:10] or ["待从原文中抽取案例。"])),
            "",
            "## 来源",
            f"- {title}",
        ]
    )


def build_sop_draft(title: str, text: str) -> str:
    lines = key_lines(text, 8)
    return "\n".join(
        [
            f"# SOP 草稿：{title}",
            "",
            "> 离线草稿：未调用 AI。适合先入库和导出，后续可用强模型重新生成。",
            "",
            "## 使用前判断",
            "- [ ] 确认该资料确实包含可执行动作，而不是纯观点或案例。",
            "- [ ] 确认场景、对象、目标和约束条件。",
            "",
            "## 操作步骤",
            *(f"{idx}. {item}" for idx, item in enumerate(lines[:6] or ["阅读结构化笔记，提取可执行动作。"], start=1)),
            "",
            "## 检查清单",
            "- [ ] 步骤是否能被实际执行",
            "- [ ] 是否保留关键前提和风险",
            "- [ ] 是否需要人工或 AI 二次精炼",
        ]
    )


def build_insight_draft(title: str, text: str) -> str:
    lines = key_lines(text, 10)
    return "\n".join(
        [
            f"# Insight 草稿：{title}",
            "",
            "> 离线草稿：提取原文中的高信息密度句子，后续可用 AI 重新生成。",
            "",
            "## TL;DR",
            *(f"- {item}" for item in (first_sentences(text, 3) or ["待补充 TL;DR。"])),
            "",
            "## Key Findings",
            *(f"- {item}" for item in (lines[:6] or ["待补充发现。"])),
            "",
            "## Critical Data / Evidence",
            *(f"- {item}" for item in (lines[6:10] or ["待补充证据。"])),
            "",
            "## 后续处理建议",
            "- 用更强模型重新生成正式 Insight。",
            "- 根据使用反馈调整标签和能力包归属。",
        ]
    )


def save_generated_artifact(conn, file_id: int, artifact_type: str, title: str, content: str, prompt_name: str) -> Path:
    version = active_prompt_version(conn, prompt_name)
    out_dir = ARTIFACT_DIR / str(file_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_path = out_dir / f"{artifact_type}.md"
    if base_path.exists():
        path = out_dir / f"{artifact_type}_{now_id()}.md"
    else:
        path = base_path
    path.write_text(content, encoding="utf-8")
    conn.execute(
        """
        INSERT INTO artifacts(file_id, artifact_type, path, title, prompt_name, prompt_version)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (file_id, artifact_type, str(path), title, prompt_name, version),
    )
    return path


def generate_offline_artifacts(conn, file_id: int, title: str, text: str) -> None:
    save_generated_artifact(
        conn,
        file_id,
        "structure",
        "结构化笔记",
        build_structure_note(title, text),
        "structure_note",
    )
    save_generated_artifact(
        conn,
        file_id,
        "sop",
        "SOP 草稿",
        build_sop_draft(title, text),
        "sop_generator",
    )
    save_generated_artifact(
        conn,
        file_id,
        "insight",
        "Insight 草稿",
        build_insight_draft(title, text),
        "insight_generator",
    )


def category_from_path(path: Path, root: Path) -> tuple[str, str | None]:
    try:
        rel = path.relative_to(root)
        main = rel.parts[0] if len(rel.parts) > 1 else "未分类"
        sub = rel.parts[1] if len(rel.parts) > 2 else None
        return main, sub
    except ValueError:
        return "未分类", None


def ensure_tag(conn, name: str) -> int:
    name = name.strip()
    parent_id = None
    if "/" in name:
        parent_name = name.rsplit("/", 1)[0]
        parent_id = ensure_tag(conn, parent_name)
    conn.execute(
        "INSERT OR IGNORE INTO tags(name, parent_id) VALUES (?, ?)",
        (name, parent_id),
    )
    return int(conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0])


def assign_tag(
    conn,
    target_type: str,
    target_id: int,
    tag_name: str,
    scope: str = "file_strong",
    confidence: float = 0.9,
    status: str = "auto_accepted",
    source: str = "system",
    evidence: str | None = None,
) -> None:
    tag_id = ensure_tag(conn, tag_name)
    conn.execute(
        """
        INSERT INTO tag_assignments(target_type, target_id, tag_id, scope, confidence, status, source, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_type, target_id, tag_id, scope) DO UPDATE SET
            confidence = excluded.confidence,
            status = CASE
                WHEN tag_assignments.status LIKE 'user_%' THEN tag_assignments.status
                ELSE excluded.status
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (target_type, target_id, tag_id, scope, confidence, status, source, evidence),
    )


def register_file(
    conn,
    path: Path,
    library_type: str,
    root: Path,
    artifact_type: str | None = None,
) -> int:
    stat = path.stat()
    main, sub = category_from_path(path, root)
    conn.execute(
        """
        INSERT INTO files(source_path, library_type, title, filename, extension, size_bytes, mtime, main_category, sub_category, status, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 0.95)
        ON CONFLICT(source_path) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            mtime = excluded.mtime,
            main_category = excluded.main_category,
            sub_category = excluded.sub_category,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(path), library_type, normalize_title(path), path.name, path.suffix.lower(), stat.st_size, stat.st_mtime, main, sub),
    )
    file_id = int(conn.execute("SELECT id FROM files WHERE source_path = ?", (str(path),)).fetchone()[0])
    if main and main != "未分类":
        assign_tag(conn, "file", file_id, main, "file_strong", 0.95, "auto_accepted", "path", "由目录结构推断")
    if main and sub:
        assign_tag(conn, "file", file_id, f"{main}/{sub}", "file_strong", 0.92, "auto_accepted", "path", "由目录结构推断")
    if artifact_type:
        conn.execute(
            """
            INSERT OR IGNORE INTO artifacts(file_id, artifact_type, path, title)
            VALUES (?, ?, ?, ?)
            """,
            (file_id, artifact_type, str(path), path.name),
        )
    return file_id


def find_related_artifact(title: str, roots: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    related: list[tuple[str, Path]] = []
    title_key = re.sub(r"[_\s]+", "", title.replace("_原文", ""))[:24]
    if not title_key:
        return related
    for artifact_type, root in roots:
        for candidate in root.rglob("*.md"):
            key = re.sub(r"[_\s]+", "", candidate.stem.replace("_原文", ""))
            if title_key and title_key in key:
                related.append((artifact_type, candidate))
                break
    return related


def seed_defaults() -> None:
    init_db()
    with connect() as conn:
        for name, content in DEFAULT_PROMPTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO prompts(name, version, content, active) VALUES (?, 'v1', ?, 1)",
                (name, content),
            )
        for pack in DEFAULT_PACKS:
            recipe = {"include_tags": pack["tags"], "min_confidence": 0.7, "tag_statuses": ["auto_accepted", "user_approved", "user_corrected"]}
            conn.execute(
                """
                INSERT OR IGNORE INTO packs(name, description, recipe_json, include_sop, include_insight, include_source)
                VALUES (?, ?, ?, 1, 1, 0)
                """,
                (pack["name"], pack["description"], json.dumps(recipe, ensure_ascii=False)),
            )
            conn.execute(
                """
                UPDATE packs
                SET description=?, recipe_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE name=?
                """,
                (pack["description"], json.dumps(recipe, ensure_ascii=False), pack["name"]),
            )
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('active_provider', 'DeepSeek / OpenAI-compatible')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('api_base_url', 'https://api.deepseek.com')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('model_name', 'deepseek-chat')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('api_key_env', 'DEEPSEEK_API_KEY')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('server_port', '8765')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('offline_mode', 'true')")
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('export_dir', ?)", (str(PACK_DIR),))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('max_workers', '3')")


def scan_existing_library() -> dict[str, int]:
    seed_defaults()
    config = load_config()
    std = Path(config["paths"]["std_dir"])
    sop = Path(config["paths"]["sop_dir"])
    insight = Path(config["paths"]["insight_dir"])
    counts = {"standard": 0, "sop": 0, "insight": 0, "chunks": 0}
    with connect() as conn:
        def ensure_chunks(file_id: int, path: Path, library_type: str) -> None:
            nonlocal counts
            if conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()[0] != 0:
                return
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            for index, chunk in enumerate(chunk_text(text)):
                conn.execute(
                    "INSERT OR IGNORE INTO chunks(file_id, chunk_index, text, token_estimate, metadata_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        file_id,
                        index,
                        chunk,
                        estimate_tokens(chunk),
                        json.dumps({"source_path": str(path), "library_type": library_type}, ensure_ascii=False),
                    ),
                )
                counts["chunks"] += 1

        for path in std.rglob("*.md"):
            file_id = register_file(conn, path, "standard", std, "clean")
            counts["standard"] += 1
            related_roots = [("sop", sop), ("insight", insight)]
            for artifact_type, artifact_path in find_related_artifact(path.stem, related_roots):
                conn.execute(
                    "INSERT OR IGNORE INTO artifacts(file_id, artifact_type, path, title) VALUES (?, ?, ?, ?)",
                    (file_id, artifact_type, str(artifact_path), artifact_path.name),
                )
            ensure_chunks(file_id, path, "standard")
        for path in sop.rglob("*.md"):
            file_id = register_file(conn, path, "sop", sop, "sop")
            ensure_chunks(file_id, path, "sop")
            counts["sop"] += 1
        for path in insight.rglob("*.md"):
            file_id = register_file(conn, path, "insight", insight, "insight")
            ensure_chunks(file_id, path, "insight")
            counts["insight"] += 1
        conn.execute(
            "INSERT INTO jobs(job_type, status, step) VALUES ('scan_existing_library', 'completed', 'indexed current libraries')"
        )
    return counts


def dashboard_stats() -> dict[str, Any]:
    seed_defaults()
    with connect() as conn:
        stats = {
            "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "standard": conn.execute("SELECT COUNT(*) FROM files WHERE library_type='standard'").fetchone()[0],
            "sop": conn.execute("SELECT COUNT(*) FROM files WHERE library_type='sop'").fetchone()[0],
            "insight": conn.execute("SELECT COUNT(*) FROM files WHERE library_type='insight'").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
            "packs": conn.execute("SELECT COUNT(*) FROM packs").fetchone()[0],
            "failed": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0],
            "review": conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE status='needs_review'").fetchone()[0],
        }
        categories = conn.execute(
            """
            SELECT COALESCE(main_category, '未分类') AS name, COUNT(*) AS count
            FROM files
            WHERE library_type='standard'
            GROUP BY COALESCE(main_category, '未分类')
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()
        categories = [
            {"name": row["name"], "label": display_root_label(row["name"]), "count": row["count"]}
            for row in categories
        ]
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
        packs = conn.execute("SELECT * FROM packs ORDER BY name").fetchall()
    return {"stats": stats, "categories": categories, "jobs": jobs, "packs": packs}


def list_files(q: str = "", category: str = "", tag: str = "", status: str = "", artifact_type: str = "", limit: int = 80) -> list[Any]:
    clauses = ["1=1"]
    params: list[Any] = []
    if q:
        clauses.append(
            """
            (title LIKE ? OR filename LIKE ? OR source_path LIKE ?
             OR EXISTS (
                SELECT 1
                FROM tag_assignments qta
                JOIN tags qt ON qt.id=qta.tag_id
                WHERE qta.target_type='file'
                  AND qta.target_id=f.id
                  AND qt.name LIKE ?
             ))
            """
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if category:
        clauses.append("main_category = ?")
        params.append(category)
    if tag:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM tag_assignments fta
                JOIN tags ft ON ft.id=fta.tag_id
                WHERE fta.target_type='file'
                  AND fta.target_id=f.id
                  AND fta.scope='file_strong'
                  AND fta.status != 'user_rejected'
                  AND (ft.name = ? OR ft.name LIKE ?)
            )
            """
        )
        params.extend([tag, f"{tag}/%"])
    if status:
        clauses.append("status = ?")
        params.append(status)
    if artifact_type:
        clauses.append("EXISTS (SELECT 1 FROM artifacts a WHERE a.file_id = f.id AND a.artifact_type = ?)")
        params.append(artifact_type)
    params.append(limit)
    with connect() as conn:
        return conn.execute(
            f"""
            SELECT f.*,
                (SELECT GROUP_CONCAT(t.name, '、') FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id WHERE ta.target_type='file' AND ta.target_id=f.id AND ta.scope='file_strong') AS tags,
                (SELECT COUNT(*) FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type='sop') AS sop_count,
                (SELECT COUNT(*) FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type='insight') AS insight_count
            FROM files f
            WHERE {' AND '.join(clauses)}
            ORDER BY f.updated_at DESC, f.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def list_categories() -> list[Any]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT COALESCE(main_category, '未分类') AS name, COUNT(*) AS count
            FROM files
            GROUP BY COALESCE(main_category, '未分类')
            ORDER BY count DESC, name
            """
        ).fetchall()


def list_category_options() -> list[dict[str, Any]]:
    return [
        {"name": row["name"], "label": display_root_label(row["name"]), "count": row["count"]}
        for row in list_categories()
    ]


def tag_picker_groups() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                t.name,
                COUNT(DISTINCT CASE
                    WHEN ta.target_type='file'
                     AND ta.scope='file_strong'
                     AND ta.status != 'user_rejected'
                    THEN ta.target_id
                END) AS usage_count
            FROM tags t
            LEFT JOIN tag_assignments ta ON ta.tag_id=t.id
            GROUP BY t.id, t.name
            ORDER BY t.name
            """
        ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["name"]
        if not name:
            continue
        parts = [part for part in name.split("/") if part]
        if not parts:
            continue
        top = parts[0]
        group = groups.setdefault(
            top,
            {
                "value": top,
                "label": display_root_label(top),
                "count": 0,
                "children": [],
            },
        )
        count = int(row["usage_count"] or 0)
        if name == top:
            group["count"] = max(group["count"], count)
            continue
        group["children"].append(
            {
                "value": name,
                "label": display_tag_leaf(name),
                "full_label": display_tag_label(name),
                "count": count,
                "depth": min(len(parts), 3),
            }
        )
        if group["count"] == 0:
            group["count"] += count

    for group in groups.values():
        group["children"].sort(key=lambda item: (-int(item["count"]), item["label"]))
        child_total = sum(int(item["count"]) for item in group["children"])
        group["count"] = max(int(group["count"]), child_total)
    return sorted(groups.values(), key=lambda item: (-int(item["count"]), item["label"]))


def file_detail(file_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        file = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if not file:
            return None
        tags = conn.execute(
            """
            SELECT ta.*, t.name
            FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id
            WHERE ta.target_type='file' AND ta.target_id=?
            ORDER BY ta.scope, ta.confidence DESC
            """,
            (file_id,),
        ).fetchall()
        artifacts = conn.execute("SELECT * FROM artifacts WHERE file_id=? ORDER BY artifact_type", (file_id,)).fetchall()
        chunks = conn.execute("SELECT * FROM chunks WHERE file_id=? ORDER BY chunk_index LIMIT 5", (file_id,)).fetchall()
    previews = []
    for artifact in artifacts:
        path = Path(artifact["path"])
        text = ""
        if path.exists() and path.suffix.lower() in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="ignore")[:3000]
        previews.append({"artifact": artifact, "preview": text})
    return {"file": file, "tags": tags, "artifacts": previews, "chunks": chunks}


def update_file_tag(file_id: int, tag: str, action: str) -> None:
    with connect() as conn:
        if action == "add":
            assign_tag(conn, "file", file_id, tag, "file_strong", 1.0, "user_approved", "human", "用户手动添加")
        elif action == "reject":
            tag_id = ensure_tag(conn, tag)
            conn.execute(
                """
                INSERT INTO tag_assignments(target_type, target_id, tag_id, scope, confidence, status, source, evidence)
                VALUES ('file', ?, ?, 'file_strong', 0, 'user_rejected', 'human', '用户拒绝')
                ON CONFLICT(target_type, target_id, tag_id, scope) DO UPDATE SET status='user_rejected', updated_at=CURRENT_TIMESTAMP
                """,
                (file_id, tag_id),
            )
        elif action == "demote":
            tag_id = ensure_tag(conn, tag)
            conn.execute(
                "UPDATE tag_assignments SET scope='chunk_weak', status='user_corrected', updated_at=CURRENT_TIMESTAMP WHERE target_type='file' AND target_id=? AND tag_id=?",
                (file_id, tag_id),
            )
        conn.execute(
            "INSERT INTO feedback_events(target_type, target_id, action, note) VALUES ('file', ?, ?, ?)",
            (file_id, action, tag),
        )


def ingest_upload(filename: str, data: bytes) -> int:
    seed_defaults()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", filename)
    target = UPLOAD_DIR / f"{now_id()}_{safe_name}"
    target.write_bytes(data)
    with connect() as conn:
        file_id = register_file(conn, target, "upload", UPLOAD_DIR, None)
        conn.execute(
            "INSERT INTO jobs(file_id, job_type, status, step) VALUES (?, 'ingest_upload', 'pending', 'uploaded')",
            (file_id,),
        )
    return file_id


JOB_PROGRESS = {
    "pending": 5,
    "converting": 25,
    "completed": 100,
    "failed": 100,
}

JOB_STEP_PROGRESS = {
    "uploaded": 5,
    "convert to markdown": 25,
    "write standard markdown": 45,
    "offline structure/sop/insight drafts": 65,
    "chunk text": 85,
    "completed": 100,
    "failed": 100,
}


def list_ingest_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT j.*, f.title AS file_name, f.source_path
            FROM jobs j
            LEFT JOIN files f ON f.id = j.file_id
            WHERE j.job_type IN ('ingest_upload', 'process_upload')
              AND NOT EXISTS (
                  SELECT 1 FROM jobs newer
                  WHERE newer.file_id = j.file_id
                    AND newer.job_type IN ('ingest_upload', 'process_upload')
                    AND newer.id > j.id
              )
            ORDER BY j.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["progress"] = JOB_STEP_PROGRESS.get(item["step"], JOB_PROGRESS.get(item["status"], 0))
        output.append(item)
    return output


def process_upload(file_id: int) -> None:
    config = load_config()
    std_dir = Path(config["paths"]["std_dir"])
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return
        source = Path(row["source_path"])
        job = conn.execute(
            "SELECT id FROM jobs WHERE file_id=? AND job_type='ingest_upload' ORDER BY id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        if job:
            job_id = job["id"]
            conn.execute(
                "UPDATE jobs SET status='converting', step='convert to markdown', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
        else:
            job_id = conn.execute(
                "INSERT INTO jobs(file_id, job_type, status, step) VALUES (?, 'process_upload', 'converting', 'convert to markdown')",
                (file_id,),
            ).lastrowid
        conn.commit()
        try:
            if source.suffix.lower() not in SUPPORTED_EXTS:
                raise ValueError(f"暂不支持的文件类型: {source.suffix}")
            md = MarkItDown()
            result = md.convert(str(source))
            text = clean_text(result.text_content or "")
            if not text:
                raise ValueError("转换结果为空")
            target_dir = std_dir / "00_Pending_Review"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{source.stem}.md"
            target_path.write_text(text, encoding="utf-8")
            conn.execute("UPDATE jobs SET status='indexing', step='write standard markdown', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            conn.commit()
            new_id = register_file(conn, target_path, "standard", std_dir, "clean")
            assign_tag(conn, "file", new_id, "00_Pending_Review", "file_strong", 0.5, "needs_review", "system", "上传后尚未人工归类")
            conn.execute("UPDATE jobs SET status='generating', step='offline structure/sop/insight drafts', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            conn.commit()
            generate_offline_artifacts(conn, new_id, normalize_title(target_path), text)
            conn.execute("UPDATE jobs SET status='indexing', step='chunk text', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            conn.commit()
            for idx, chunk in enumerate(chunk_text(text)):
                conn.execute(
                    "INSERT OR IGNORE INTO chunks(file_id, chunk_index, text, token_estimate, metadata_json) VALUES (?, ?, ?, ?, ?)",
                    (new_id, idx, chunk, estimate_tokens(chunk), json.dumps({"source_upload": str(source)}, ensure_ascii=False)),
                )
            conn.execute("UPDATE files SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (file_id,))
            conn.execute("UPDATE jobs SET status='completed', step='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            conn.commit()
        except Exception as exc:
            conn.execute("UPDATE files SET status='failed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (file_id,))
            conn.execute(
                "UPDATE jobs SET status='failed', step='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), job_id),
            )
            conn.commit()


def regenerate_file_artifacts(file_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise ValueError("文件不存在")
        source = Path(row["source_path"])
        if not source.exists():
            raise ValueError("源文件不存在")
        job_id = conn.execute(
            "INSERT INTO jobs(file_id, job_type, status, step) VALUES (?, 'regenerate_artifacts', 'generating', 'offline draft generation')",
            (file_id,),
        ).lastrowid
        try:
            if source.suffix.lower() in {".md", ".txt"}:
                text = source.read_text(encoding="utf-8", errors="ignore")
            else:
                text = clean_text(MarkItDown().convert(str(source)).text_content or "")
            if not text.strip():
                raise ValueError("源文件内容为空")
            generate_offline_artifacts(conn, file_id, row["title"], text)
            conn.execute("UPDATE jobs SET status='completed', step='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        except Exception as exc:
            conn.execute(
                "UPDATE jobs SET status='failed', step='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), job_id),
            )
            raise


def list_packs() -> list[dict[str, Any]]:
    with connect() as conn:
        packs = conn.execute("SELECT * FROM packs ORDER BY name").fetchall()
        output = []
        for pack in packs:
            recipe = json.loads(pack["recipe_json"])
            files = files_for_pack(pack["id"], conn=conn)
            output.append({"pack": pack, "recipe": recipe, "file_count": len(files)})
        return output


def parse_tag_text(raw: str) -> list[str]:
    parts = re.split(r"[\n,，;；]+", raw)
    tags = []
    seen = set()
    for part in parts:
        tag = part.strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def save_pack_recipe(
    pack_id: int | None,
    name: str,
    description: str,
    include_tags_text: str,
    min_confidence: float = 0.7,
    include_sop: bool = True,
    include_insight: bool = True,
    include_source: bool = False,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("能力包名称不能为空")
    tags = parse_tag_text(include_tags_text)
    if not tags:
        raise ValueError("至少需要一个标签")
    min_confidence = max(0.0, min(float(min_confidence), 1.0))
    recipe = {
        "include_tags": tags,
        "min_confidence": min_confidence,
        "tag_statuses": ["auto_accepted", "user_approved", "user_corrected"],
    }
    with connect() as conn:
        for tag in tags:
            ensure_tag(conn, tag)
        if pack_id:
            conn.execute(
                """
                UPDATE packs
                SET name=?, description=?, recipe_json=?, include_sop=?, include_insight=?, include_source=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (name, description.strip(), json.dumps(recipe, ensure_ascii=False), int(include_sop), int(include_insight), int(include_source), pack_id),
            )
            return pack_id
        cur = conn.execute(
            """
            INSERT INTO packs(name, description, recipe_json, include_sop, include_insight, include_source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, description.strip(), json.dumps(recipe, ensure_ascii=False), int(include_sop), int(include_insight), int(include_source)),
        )
        return int(cur.lastrowid)


def delete_pack(pack_id: int) -> bool:
    """Delete a pack recipe and its export history, never source knowledge files."""
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM packs WHERE id=?", (pack_id,)).fetchone()
        if not exists:
            return False
        conn.execute("DELETE FROM exports WHERE pack_id=?", (pack_id,))
        conn.execute("DELETE FROM packs WHERE id=?", (pack_id,))
        return True


def files_for_pack(pack_id: int, conn=None, include_low_confidence: bool = False) -> list[Any]:
    owns_conn = conn is None
    if owns_conn:
        conn = connect()
    try:
        pack = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        if not pack:
            return []
        recipe = json.loads(pack["recipe_json"])
        tags = recipe.get("include_tags", [])
        if not tags:
            return []
        min_conf = 0.0 if include_low_confidence else float(recipe.get("min_confidence", 0.7))
        statuses = recipe.get("tag_statuses", ["auto_accepted", "user_approved", "user_corrected"])
        tag_clauses = []
        tag_params = []
        for tag in tags:
            tag_clauses.append("(t.name = ? OR t.name LIKE ?)")
            tag_params.extend([tag, f"{tag}/%"])
        status_placeholders = ",".join("?" for _ in statuses)
        params = [*tag_params, min_conf, *statuses]
        return conn.execute(
            f"""
            SELECT DISTINCT f.*
            FROM files f
            JOIN tag_assignments ta ON ta.target_type='file' AND ta.target_id=f.id
            JOIN tags t ON t.id=ta.tag_id
            WHERE ({' OR '.join(tag_clauses)})
              AND ta.confidence >= ?
              AND ta.status IN ({status_placeholders})
              AND ta.scope='file_strong'
              AND f.library_type IN ('standard', 'sop', 'insight')
            ORDER BY f.main_category, f.sub_category, f.title
            """,
            params,
        ).fetchall()
    finally:
        if owns_conn:
            conn.close()


def export_pack(pack_id: int, export_format: str = "zip", include_low_confidence: bool = False) -> Path:
    seed_defaults()
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        pack = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        if not pack:
            raise ValueError("能力包不存在")
        files = files_for_pack(pack_id, conn=conn, include_low_confidence=include_low_confidence)
        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", pack["name"]).strip("_")
        out_dir = PACK_DIR / f"{slug}_{now_id()}"
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": pack["name"],
            "description": pack["description"],
            "recipe": json.loads(pack["recipe_json"]),
            "file_count": len(files),
            "include_source": bool(pack["include_source"]),
            "include_sop": bool(pack["include_sop"]),
            "include_insight": bool(pack["include_insight"]),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        overview_lines = [f"# {pack['name']}", "", pack["description"] or "", "", f"- 文件数: {len(files)}", ""]
        jsonl_lines: list[str] = []
        source_dir = out_dir / "sources"
        sop_dir = out_dir / "artifacts" / "sop"
        insight_dir = out_dir / "artifacts" / "insight"

        def write_export_file(target_dir: Path, index: int, file_title: str, source_path: Path, suffix: str = "") -> str | None:
            if not source_path.exists():
                return None
            text = source_path.read_text(encoding="utf-8", errors="ignore")
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", file_title)[:80]
            target_dir.mkdir(parents=True, exist_ok=True)
            rel_path = target_dir / f"{index:03d}_{safe_title}{suffix}.md"
            rel_path.write_text(text, encoding="utf-8")
            return str(rel_path.relative_to(out_dir)).replace("\\", "/")

        for index, file in enumerate(files, 1):
            source = Path(file["source_path"])
            links = []
            if pack["include_source"]:
                source_rel = write_export_file(source_dir, index, file["title"], source, "_source")
                if source_rel:
                    links.append(f"[原文]({source_rel})")
            if pack["include_sop"]:
                sop_path = source if file["library_type"] == "sop" else None
                if sop_path is None:
                    row = conn.execute(
                        "SELECT path FROM artifacts WHERE file_id=? AND artifact_type='sop' ORDER BY id DESC LIMIT 1",
                        (file["id"],),
                    ).fetchone()
                    sop_path = Path(row["path"]) if row else None
                if sop_path:
                    sop_rel = write_export_file(sop_dir, index, file["title"], sop_path, "_sop")
                    if sop_rel:
                        links.append(f"[SOP]({sop_rel})")
            if pack["include_insight"]:
                insight_path = source if file["library_type"] == "insight" else None
                if insight_path is None:
                    row = conn.execute(
                        "SELECT path FROM artifacts WHERE file_id=? AND artifact_type='insight' ORDER BY id DESC LIMIT 1",
                        (file["id"],),
                    ).fetchone()
                    insight_path = Path(row["path"]) if row else None
                if insight_path:
                    insight_rel = write_export_file(insight_dir, index, file["title"], insight_path, "_insight")
                    if insight_rel:
                        links.append(f"[Insight]({insight_rel})")
            link_text = " · ".join(links) if links else "仅 JSONL 知识块"
            overview_lines.append(f"{index}. {file['title']} - {link_text}")
            chunks = conn.execute("SELECT * FROM chunks WHERE file_id=? ORDER BY chunk_index", (file["id"],)).fetchall()
            for chunk in chunks:
                jsonl_lines.append(
                    json.dumps(
                        {
                            "pack": pack["name"],
                            "file_id": file["id"],
                            "title": file["title"],
                            "chunk_index": chunk["chunk_index"],
                            "text": chunk["text"],
                            "metadata": json.loads(chunk["metadata_json"] or "{}"),
                        },
                        ensure_ascii=False,
                    )
                )
        (out_dir / "00_总览.md").write_text("\n".join(overview_lines), encoding="utf-8")
        (out_dir / "chunks.jsonl").write_text("\n".join(jsonl_lines), encoding="utf-8")
        if export_format == "zip":
            zip_path = out_dir.with_suffix(".zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in out_dir.rglob("*"):
                    zf.write(item, item.relative_to(out_dir))
            conn.execute(
                "INSERT INTO exports(pack_id, export_format, path) VALUES (?, ?, ?)",
                (pack_id, "zip", str(zip_path)),
            )
            return zip_path
        conn.execute(
            "INSERT INTO exports(pack_id, export_format, path) VALUES (?, ?, ?)",
            (pack_id, "folder", str(out_dir)),
        )
        return out_dir


def settings_data() -> dict[str, Any]:
    with connect() as conn:
        prompts = conn.execute("SELECT * FROM prompts ORDER BY name, version").fetchall()
        tags = conn.execute(
            """
            SELECT t.*, (SELECT COUNT(*) FROM tag_assignments ta WHERE ta.tag_id=t.id) AS usage_count
            FROM tags t
            ORDER BY usage_count DESC, name
            LIMIT 120
            """
        ).fetchall()
        settings = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()
    return {"prompts": prompts, "tags": tags, "settings": settings}


def create_prompt_version(name: str, content: str) -> str:
    name = name.strip()
    content = content.strip()
    if not name or not content:
        raise ValueError("提示词名称和内容不能为空")
    with connect() as conn:
        existing = conn.execute("SELECT version FROM prompts WHERE name=? ORDER BY id DESC", (name,)).fetchall()
        next_index = 1
        for row in existing:
            match = re.match(r"v(\d+)$", row["version"])
            if match:
                next_index = max(next_index, int(match.group(1)) + 1)
        version = f"v{next_index}"
        conn.execute("UPDATE prompts SET active=0 WHERE name=?", (name,))
        conn.execute(
            "INSERT INTO prompts(name, version, content, active) VALUES (?, ?, ?, 1)",
            (name, version, content),
        )
        return version


def update_setting(key: str, value: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("设置 key 不能为空")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, value.strip()),
        )


def create_or_update_tag(name: str, description: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("标签名称不能为空")
    with connect() as conn:
        tag_id = ensure_tag(conn, name)
        conn.execute("UPDATE tags SET description=? WHERE id=?", (description.strip(), tag_id))
        return tag_id


def rename_tag(tag_id: int, name: str, description: str = "") -> None:
    name = name.strip()
    if not name:
        raise ValueError("标签名称不能为空")
    with connect() as conn:
        parent_id = None
        if "/" in name:
            parent_id = ensure_tag(conn, name.rsplit("/", 1)[0])
        conn.execute(
            "UPDATE tags SET name=?, parent_id=?, description=? WHERE id=?",
            (name, parent_id, description.strip(), tag_id),
        )


def delete_unused_tag(tag_id: int) -> None:
    with connect() as conn:
        usage = conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE tag_id=?", (tag_id,)).fetchone()[0]
        if usage:
            raise ValueError("该标签仍有引用，不能删除")
        conn.execute("DELETE FROM tags WHERE id=?", (tag_id,))
