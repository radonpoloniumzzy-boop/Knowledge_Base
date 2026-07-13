from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import bleach
import markdown
from markupsafe import Markup

from .db import DATA_DIR, ROOT_DIR, connect, init_db


CONFIG_PATH = ROOT_DIR / "00_Scripts" / "config.json"
PACK_DIR = DATA_DIR / "Knowledge_Packs"
UPLOAD_DIR = DATA_DIR / "Uploads"
ARTIFACT_DIR = DATA_DIR / "Artifacts"


@dataclass(frozen=True)
class MissingPackArtifact:
    file_id: int | None
    title: str
    artifact_type: str
    message: str


@dataclass(frozen=True)
class PackExportPreflight:
    pack_id: int
    file_count: int
    missing: tuple[MissingPackArtifact, ...]

    @property
    def ready(self) -> bool:
        return self.file_count > 0 and not self.missing


class PackExportBlockedError(ValueError):
    def __init__(self, preflight: PackExportPreflight) -> None:
        super().__init__("能力包缺少必需内容，请先查看导出预检。")
        self.preflight = preflight


@dataclass(frozen=True)
class LibraryPage:
    items: list[Any]
    total: int
    page: int
    page_size: int
    page_count: int


ROOT_LABELS = {
    "Uncategorized": "未归类",
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

PACK_EMBLEM_COLORS = (
    {"value": "#7C6CCF", "label": "奥术紫"},
    {"value": "#3F74C7", "label": "星辉蓝"},
    {"value": "#B05F8F", "label": "吟游莓"},
    {"value": "#5F6F9C", "label": "守夜灰蓝"},
    {"value": "#9A654B", "label": "行商铜"},
)
PACK_ARCHETYPES = {
    "adventurer": {"label": "冒险者", "title": "未知道路的探索者"},
    "bard": {"label": "吟游诗人", "title": "灵感与表达的编织者"},
    "merchant": {"label": "行商者", "title": "机会与资源的联结者"},
    "ranger": {"label": "游侠", "title": "实践与训练的引路人"},
    "artificer": {"label": "奥术技师", "title": "系统与工具的构造者"},
    "warden": {"label": "守卫", "title": "边界与风险的看守者"},
    "scholar": {"label": "学者", "title": "知识脉络的研习者"},
}
DEFAULT_PACK_COLOR = PACK_EMBLEM_COLORS[0]["value"]
DEFAULT_PACK_ARCHETYPE = "adventurer"


def suggest_pack_archetype(tags: list[str]) -> str:
    roots = " ".join(tag.split("/", 1)[0].lower() for tag in tags)
    rules = (
        (("art", "media", "艺术", "传媒", "剪辑", "摄影"), "bard"),
        (("sales", "marketing", "ops", "销售", "营销", "企业管理"), "merchant"),
        (("coding", "ai", "编程", "人工智能", "工具"), "artificer"),
        (("finance", "law", "金融", "法律", "风控", "合规"), "warden"),
        (("sport", "training", "education", "运动", "训练", "教育"), "ranger"),
        (("general", "通用", "学术", "研究"), "scholar"),
    )
    return next((key for needles, key in rules if any(needle in roots for needle in needles)), DEFAULT_PACK_ARCHETYPE)


def _pack_color(value: str | None) -> str:
    normalized = (value or "").strip().upper()
    allowed = {item["value"] for item in PACK_EMBLEM_COLORS}
    return normalized if normalized in allowed else DEFAULT_PACK_COLOR


def _pack_archetype(value: str | None) -> str:
    return value if value in PACK_ARCHETYPES else DEFAULT_PACK_ARCHETYPE


DEFAULT_PROMPTS = {
    "clean_transcript": """清洗学习资料转写稿。去除口癖、重复寒暄、无意义语气词，保留知识点、案例和步骤。输出 Markdown。""",
    "structure_note": """把清洗后的学习资料整理为结构化笔记：主题、适用场景、核心概念、步骤、误区、案例、可复用原则、来源。""",
    "sop_generator": """从结构化笔记中提取可执行 SOP。若资料纯理论则说明不可生成 SOP。输出 Markdown 检查清单。""",
    "insight_generator": """从结构化笔记中提取 Insight：关键事实、逻辑、模型、判断、证据和可迁移结论。输出 Markdown。""",
    "taxonomy_classifier": """判断资料主分类、强标签、弱标签。文件级强标签必须代表主要内容；一句话相关只能作为片段级弱标签。""",
}


def _active_file_clause(alias: str = "f") -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM knowledge_sources hidden_source
            WHERE hidden_source.source_file_id={alias}.id
              AND (hidden_source.deleted_at IS NOT NULL
                   OR hidden_source.recycle_requested_at IS NOT NULL)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM source_versions hidden_version
            JOIN knowledge_sources hidden_source
              ON hidden_source.id=hidden_version.source_id
            WHERE (hidden_version.upload_file_id={alias}.id
                   OR hidden_version.standard_file_id={alias}.id)
              AND (hidden_source.deleted_at IS NOT NULL
                   OR hidden_source.recycle_requested_at IS NOT NULL)
        )
    """


def _current_standard_clause(alias: str = "f") -> str:
    return f"""
        {alias}.library_type='standard'
        AND {_active_file_clause(alias)}
        AND (
            NOT EXISTS (
                SELECT 1 FROM source_versions known_version
                WHERE known_version.standard_file_id={alias}.id
            )
            OR EXISTS (
                SELECT 1
                FROM source_versions current_version
                JOIN knowledge_sources current_source
                  ON current_source.id=current_version.source_id
                WHERE current_version.standard_file_id={alias}.id
                  AND current_source.current_version_id=current_version.id
                  AND current_source.deleted_at IS NULL
                  AND current_source.recycle_requested_at IS NULL
            )
        )
    """


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
            confidence = CASE
                WHEN tag_assignments.status LIKE 'user_%' OR tag_assignments.source='human'
                THEN tag_assignments.confidence ELSE excluded.confidence END,
            status = CASE
                WHEN tag_assignments.status LIKE 'user_%' THEN tag_assignments.status
                ELSE excluded.status
            END,
            source = CASE
                WHEN tag_assignments.status LIKE 'user_%' OR tag_assignments.source='human'
                THEN tag_assignments.source ELSE excluded.source END,
            evidence = CASE
                WHEN tag_assignments.status LIKE 'user_%' OR tag_assignments.source='human'
                THEN tag_assignments.evidence ELSE excluded.evidence END,
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
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('import_concurrency', '1')")


def knowledge_map(root_limit: int = 6, child_limit: int = 4) -> dict[str, Any]:
    unclassified_values = ("", "未分类", "Uncategorized", "00_Unsorted")
    with connect() as conn:
        current = _current_standard_clause("f")
        placeholders = ", ".join("?" for _ in unclassified_values)
        root_rows = conn.execute(
            f"""
            SELECT f.main_category AS name, COUNT(*) AS count
            FROM files f
            WHERE {current}
              AND COALESCE(f.main_category, '') NOT IN ({placeholders})
            GROUP BY f.main_category
            ORDER BY count DESC, f.main_category
            LIMIT ?
            """,
            (*unclassified_values, root_limit),
        ).fetchall()
        unclassified_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM files f
                WHERE {current}
                  AND COALESCE(f.main_category, '') IN ({placeholders})
                """,
                unclassified_values,
            ).fetchone()[0]
        )

        nodes = []
        for root in root_rows:
            key = root["name"]
            tag_rows = conn.execute(
                f"""
                SELECT t.name, COUNT(DISTINCT f.id) AS count
                FROM files f
                JOIN tag_assignments ta
                  ON ta.target_type='file' AND ta.target_id=f.id
                JOIN tags t ON t.id=ta.tag_id
                WHERE {current}
                  AND f.main_category=?
                  AND ta.scope='file_strong'
                  AND ta.status!='user_rejected'
                  AND t.name LIKE ?
                GROUP BY t.name
                ORDER BY count DESC, t.name
                LIMIT ?
                """,
                (key, f"{key}/%", child_limit),
            ).fetchall()
            children = [
                {
                    "key": row["name"],
                    "label": display_tag_leaf(row["name"]),
                    "count": int(row["count"]),
                    "url": f"/library?tag={quote(row['name'])}",
                    "source": "tag",
                }
                for row in tag_rows
            ]
            if not children:
                fallback_rows = conn.execute(
                    f"""
                    SELECT f.sub_category AS name, COUNT(*) AS count
                    FROM files f
                    WHERE {current}
                      AND f.main_category=?
                      AND TRIM(COALESCE(f.sub_category, ''))!=''
                    GROUP BY f.sub_category
                    ORDER BY count DESC, f.sub_category
                    LIMIT ?
                    """,
                    (key, child_limit),
                ).fetchall()
                children = [
                    {
                        "key": f"{key}/{row['name']}",
                        "label": row["name"],
                        "count": int(row["count"]),
                        "url": f"/library?category={quote(key)}&q={quote(row['name'])}",
                        "source": "sub_category",
                    }
                    for row in fallback_rows
                ]
            nodes.append(
                {
                    "key": key,
                    "label": display_root_label(key),
                    "count": int(root["count"]),
                    "url": f"/library?category={quote(key)}",
                    "children": children,
                }
            )
    return {"nodes": nodes, "unclassified_count": unclassified_count}


def dashboard_stats() -> dict[str, Any]:
    seed_defaults()
    with connect() as conn:
        active = _active_file_clause("f")
        def count_files(library_type: str | None = None) -> int:
            type_clause = "" if library_type is None else "AND f.library_type=?"
            params = () if library_type is None else (library_type,)
            return int(conn.execute(
                f"SELECT COUNT(*) FROM files f WHERE {active} {type_clause}", params
            ).fetchone()[0])

        stats = {
            "files": count_files(),
            "standard": count_files("standard"),
            "sop": count_files("sop"),
            "insight": count_files("insight"),
            "chunks": conn.execute(
                f"SELECT COUNT(*) FROM chunks c JOIN files f ON f.id=c.file_id WHERE {active}"
            ).fetchone()[0],
            "tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
            "packs": conn.execute("SELECT COUNT(*) FROM packs").fetchone()[0],
            "failed": conn.execute("SELECT COUNT(*) FROM import_tasks WHERE status='needs_attention'").fetchone()[0],
            "review": conn.execute(
                f"""
                SELECT COUNT(*)
                FROM tag_assignments ta
                JOIN files f ON ta.target_type='file' AND ta.target_id=f.id
                WHERE ta.status='needs_review' AND {active}
                """
            ).fetchone()[0],
        }
        categories = conn.execute(
            """
            SELECT COALESCE(main_category, '未分类') AS name, COUNT(*) AS count
            FROM files f
            WHERE library_type='standard'
              AND """ + active + """
            GROUP BY COALESCE(main_category, '未分类')
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()
        categories = [
            {"name": row["name"], "label": display_root_label(row["name"]), "count": row["count"]}
            for row in categories
        ]
        jobs = conn.execute(
            """
            SELECT filename AS file_name, status, current_stage, updated_at
            FROM import_tasks
            ORDER BY id DESC
            LIMIT 8
            """
        ).fetchall()
        packs = conn.execute("SELECT * FROM packs ORDER BY name").fetchall()
    map_data = knowledge_map()
    return {
        "stats": stats,
        "categories": categories,
        "jobs": jobs,
        "packs": packs,
        "knowledge_map": map_data["nodes"],
        "unclassified_count": map_data["unclassified_count"],
    }


def list_files(q: str = "", category: str = "", tag: str = "", status: str = "", artifact_type: str = "", limit: int = 80) -> list[Any]:
    clauses = [
        """
        (NOT EXISTS (
            SELECT 1 FROM source_versions visible_version
            WHERE visible_version.standard_file_id=f.id
        ) OR EXISTS (
            SELECT 1
            FROM source_versions current_version
            JOIN knowledge_sources current_source
              ON current_source.id=current_version.source_id
            WHERE current_version.standard_file_id=f.id
              AND current_source.current_version_id=current_version.id
              AND current_source.deleted_at IS NULL
              AND current_source.recycle_requested_at IS NULL
        ))
        """
    ]
    params: list[Any] = []
    if q:
        clauses.append(
            """
            (title LIKE ? OR filename LIKE ? OR source_path LIKE ? OR main_category LIKE ? OR sub_category LIKE ?
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
        params.extend([like, like, like, like, like, like])
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
                (SELECT COUNT(*) FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type='insight') AS insight_count,
                COALESCE(
                    (SELECT sv.source_id FROM source_versions sv WHERE sv.standard_file_id=f.id LIMIT 1),
                    (SELECT sv.source_id FROM source_versions sv WHERE sv.upload_file_id=f.id LIMIT 1),
                    (SELECT ks.id FROM knowledge_sources ks WHERE ks.source_file_id=f.id LIMIT 1)
                ) AS source_id
            FROM files f
            WHERE {' AND '.join(clauses)}
            ORDER BY f.updated_at DESC, f.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def _library_filter_parts(
    q: str = "", category: str = "", tag: str = "", status: str = "",
    artifact_type: str = "", domain: int = 0,
) -> tuple[list[str], list[Any]]:
    clauses = [
        _active_file_clause("f"),
        """
        (NOT EXISTS (SELECT 1 FROM source_versions vv WHERE vv.standard_file_id=f.id)
         OR EXISTS (
            SELECT 1 FROM source_versions cv
            JOIN knowledge_sources cs ON cs.id=cv.source_id
            WHERE cv.standard_file_id=f.id AND cs.current_version_id=cv.id
              AND cs.deleted_at IS NULL AND cs.recycle_requested_at IS NULL
         ))
        """,
    ]
    params: list[Any] = []
    if q:
        like = f"%{q}%"
        clauses.append("""
            (f.title LIKE ? OR f.filename LIKE ? OR f.source_path LIKE ?
             OR f.main_category LIKE ? OR f.sub_category LIKE ? OR EXISTS (
                SELECT 1 FROM tag_assignments qa JOIN tags qt ON qt.id=qa.tag_id
                WHERE qa.target_type='file' AND qa.target_id=f.id AND qt.name LIKE ?))
        """)
        params.extend([like] * 6)
    if category:
        clauses.append("f.main_category=?")
        params.append(category)
    if tag:
        clauses.append("""
            EXISTS (SELECT 1 FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id
                    WHERE ta.target_type='file' AND ta.target_id=f.id
                      AND ta.scope='file_strong' AND ta.status!='user_rejected'
                      AND (t.name=? OR t.name LIKE ?))
        """)
        params.extend([tag, f"{tag}/%"])
    if status:
        clauses.append("f.status=?")
        params.append(status)
    if artifact_type:
        clauses.append("EXISTS (SELECT 1 FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type=?)")
        params.append(artifact_type)
    if domain:
        clauses.append("""
            EXISTS (
                SELECT 1 FROM knowledge_domain_rules dr
                JOIN knowledge_domains kd ON kd.id=dr.domain_id
                WHERE dr.domain_id=? AND kd.enabled=1 AND (
                    (dr.rule_type='main_category' AND f.main_category=dr.match_value)
                    OR (dr.rule_type='tag_prefix' AND EXISTS (
                        SELECT 1 FROM tag_assignments da JOIN tags dt ON dt.id=da.tag_id
                        WHERE da.target_type='file' AND da.target_id=f.id
                          AND da.scope='file_strong' AND da.status!='user_rejected'
                          AND (dt.name=dr.match_value OR dt.name LIKE dr.match_value || '/%')
                    ))
                )
            )
        """)
        params.append(domain)
    return clauses, params


def search_library_page(
    q: str = "", category: str = "", tag: str = "", status: str = "",
    artifact_type: str = "", domain: int = 0, page: int = 1,
    page_size: int = 50, sort: str = "updated_desc",
) -> LibraryPage:
    page_size = page_size if page_size in {25, 50, 100} else 50
    sort_sql = {
        "updated_desc": "f.updated_at DESC, f.id DESC",
        "title_asc": "f.title COLLATE NOCASE ASC, f.id ASC",
        "category_asc": "COALESCE(f.main_category, '') ASC, f.title COLLATE NOCASE ASC",
        "created_desc": "f.created_at DESC, f.id DESC",
    }.get(sort, "f.updated_at DESC, f.id DESC")
    clauses, params = _library_filter_parts(q, category, tag, status, artifact_type, domain)
    where = " AND ".join(f"({clause})" for clause in clauses)
    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM files f WHERE {where}", params).fetchone()[0])
        page_count = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), page_count))
        rows = conn.execute(
            f"""
            SELECT f.*,
              (SELECT GROUP_CONCAT(t.name, '、') FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id WHERE ta.target_type='file' AND ta.target_id=f.id AND ta.scope='file_strong') AS tags,
              (SELECT COUNT(*) FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type='sop') AS sop_count,
              (SELECT COUNT(*) FROM artifacts a WHERE a.file_id=f.id AND a.artifact_type='insight') AS insight_count,
              COALESCE((SELECT sv.source_id FROM source_versions sv WHERE sv.standard_file_id=f.id LIMIT 1),
                       (SELECT ks.id FROM knowledge_sources ks WHERE ks.source_file_id=f.id LIMIT 1)) AS source_id
            FROM files f WHERE {where}
            ORDER BY {sort_sql} LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return LibraryPage(list(rows), total, page, page_size, page_count)


def list_knowledge_domains(enabled_only: bool = True) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_domains " + ("WHERE enabled=1 " if enabled_only else "") + "ORDER BY sort_order, id"
        ).fetchall()
        output = []
        for row in rows:
            clauses, params = _library_filter_parts(domain=int(row["id"]))
            count = int(conn.execute(
                f"SELECT COUNT(*) FROM files f WHERE {' AND '.join(f'({c})' for c in clauses)}", params
            ).fetchone()[0])
            rules = conn.execute(
                "SELECT * FROM knowledge_domain_rules WHERE domain_id=? ORDER BY rule_type, match_value",
                (row["id"],),
            ).fetchall()
            output.append({"domain": row, "count": count, "rules": rules})
    return output


def list_categories() -> list[Any]:
    with connect() as conn:
        active = _active_file_clause("f")
        return conn.execute(
            f"""
            SELECT COALESCE(main_category, '未分类') AS name, COUNT(*) AS count
            FROM files f
            WHERE {active}
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
        active = _active_file_clause("tagged_file")
        rows = conn.execute(
            f"""
            SELECT
                t.name,
                COUNT(DISTINCT CASE
                    WHEN ta.target_type='file'
                     AND ta.scope='file_strong'
                     AND ta.status != 'user_rejected'
                     AND EXISTS (
                         SELECT 1 FROM files tagged_file
                         WHERE tagged_file.id=ta.target_id AND {active}
                     )
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
        active = _active_file_clause("f")
        file = conn.execute(
            f"SELECT f.* FROM files f WHERE f.id=? AND {active}", (file_id,)
        ).fetchone()
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


SAFE_MARKDOWN_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "li", "ol", "p", "pre", "strong", "table", "tbody",
    "td", "th", "thead", "tr", "ul",
}


def render_markdown_document(text: str) -> dict[str, Markup]:
    renderer = markdown.Markdown(extensions=["toc", "tables", "fenced_code"], output_format="html")
    raw_html = renderer.convert(text)
    clean_html = bleach.clean(
        raw_html,
        tags=SAFE_MARKDOWN_TAGS,
        attributes={"a": ["href", "title"], "h1": ["id"], "h2": ["id"], "h3": ["id"], "h4": ["id"], "h5": ["id"], "h6": ["id"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    clean_toc = bleach.clean(
        renderer.toc, tags={"div", "ul", "li", "a"},
        attributes={"div": ["class"], "a": ["href"]}, protocols=set(), strip=True,
    )
    return {"html": Markup(clean_html), "toc": Markup(clean_toc)}


def document_reader(file_id: int, view: str = "standard") -> dict[str, Any] | None:
    detail = file_detail(file_id)
    if detail is None:
        return None
    allowed = {"standard", "structure", "sop", "insight", "chunks"}
    view = view if view in allowed else "standard"
    text = ""
    available = ["standard"]
    if view == "standard":
        path = Path(detail["file"]["source_path"])
        if path.exists() and path.suffix.lower() in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
    elif view == "chunks":
        with connect() as conn:
            chunks = conn.execute(
                "SELECT * FROM chunks WHERE file_id=? ORDER BY chunk_index", (file_id,)
            ).fetchall()
        text = "\n\n".join(f"## 知识块 {row['chunk_index']}\n\n{row['text']}" for row in chunks)
    else:
        artifact = next((item for item in detail["artifacts"] if item["artifact"]["artifact_type"] == view), None)
        if artifact:
            path = Path(artifact["artifact"]["path"])
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="ignore")
    artifact_types = {item["artifact"]["artifact_type"] for item in detail["artifacts"]}
    available.extend(kind for kind in ("structure", "sop", "insight") if kind in artifact_types)
    available.append("chunks")
    rendered = render_markdown_document(text or "_当前内容尚未生成。_")
    return {"view": view, "available_views": available, "text": text, **rendered}


def update_file_metadata(
    file_id: int,
    title: str,
    main_category: str = "",
    sub_category: str = "",
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("资料标题不能为空")
    main_category = main_category.strip() or None
    sub_category = sub_category.strip() or None
    with connect() as conn:
        file = conn.execute("SELECT id FROM files WHERE id=?", (file_id,)).fetchone()
        if file is None:
            raise KeyError(file_id)
        source = conn.execute(
            """
            SELECT ks.source_file_id, current_version.standard_file_id
            FROM knowledge_sources ks
            LEFT JOIN source_versions matched_version ON matched_version.source_id=ks.id
            LEFT JOIN source_versions current_version ON current_version.id=ks.current_version_id
            WHERE ks.source_file_id=?
               OR matched_version.upload_file_id=?
               OR matched_version.standard_file_id=?
            ORDER BY CASE WHEN current_version.standard_file_id=? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (file_id, file_id, file_id, file_id),
        ).fetchone()
        target_ids = {file_id}
        if source is not None:
            target_ids.add(int(source["source_file_id"]))
            if source["standard_file_id"] is not None:
                target_ids.add(int(source["standard_file_id"]))
        placeholders = ",".join("?" for _ in target_ids)
        conn.execute(
            f"""
            UPDATE files
            SET title=?, main_category=?, sub_category=?, updated_at=CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            (title, main_category, sub_category, *sorted(target_ids)),
        )


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


def accept_import_upload(filename: str, data: bytes) -> int:
    seed_defaults()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", filename)
    target = UPLOAD_DIR / f"{now_id()}_{safe_name}"
    target.write_bytes(data)
    with connect() as conn:
        file_id = register_file(conn, target, "upload", UPLOAD_DIR, None)
    return file_id


def list_packs() -> list[dict[str, Any]]:
    with connect() as conn:
        packs = conn.execute("SELECT * FROM packs ORDER BY name").fetchall()
        output = []
        for pack in packs:
            recipe = json.loads(pack["recipe_json"])
            files = files_for_pack(pack["id"], conn=conn)
            preflight = _pack_export_preflight(conn, pack, files)
            file_ids = [int(file["id"]) for file in files]
            artifact_counts = {"sop": 0, "insight": 0}
            if file_ids:
                placeholders = ",".join("?" for _ in file_ids)
                for row in conn.execute(
                    f"""
                    SELECT artifact_type, COUNT(DISTINCT file_id) AS count
                    FROM artifacts
                    WHERE file_id IN ({placeholders}) AND artifact_type IN ('sop', 'insight')
                    GROUP BY artifact_type
                    """,
                    file_ids,
                ):
                    artifact_counts[row["artifact_type"]] = int(row["count"])
            inventory: dict[str, list[dict[str, str]]] = {}
            for tag in recipe.get("include_tags", []):
                root = tag.split("/", 1)[0]
                inventory.setdefault(display_root_label(root), []).append(
                    {"value": tag, "label": display_tag_leaf(tag)}
                )
            archetype_key = _pack_archetype(pack["archetype_key"])
            output.append({
                "pack": pack,
                "recipe": recipe,
                "file_count": len(files),
                "preflight": preflight,
                "artifact_counts": artifact_counts,
                "inventory_groups": [
                    {"label": label, "items": items} for label, items in inventory.items()
                ],
                "archetype": {"key": archetype_key, **PACK_ARCHETYPES[archetype_key]},
            })
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
    emblem_color: str | None = None,
    archetype_key: str | None = None,
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
            existing = conn.execute(
                "SELECT emblem_color, archetype_key FROM packs WHERE id=?", (pack_id,)
            ).fetchone()
            if not existing:
                raise ValueError("能力包不存在")
            color = existing["emblem_color"] if emblem_color is None else _pack_color(emblem_color)
            archetype = existing["archetype_key"] if archetype_key is None else _pack_archetype(archetype_key)
            conn.execute(
                """
                UPDATE packs
                SET name=?, description=?, recipe_json=?, include_sop=?, include_insight=?, include_source=?,
                    emblem_color=?, archetype_key=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (name, description.strip(), json.dumps(recipe, ensure_ascii=False), int(include_sop), int(include_insight), int(include_source), color, archetype, pack_id),
            )
            return pack_id
        color = _pack_color(emblem_color)
        archetype = _pack_archetype(archetype_key) if archetype_key is not None else suggest_pack_archetype(tags)
        cur = conn.execute(
            """
            INSERT INTO packs(
                name, description, recipe_json, include_sop, include_insight, include_source,
                emblem_color, archetype_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, description.strip(), json.dumps(recipe, ensure_ascii=False), int(include_sop), int(include_insight), int(include_source), color, archetype),
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
              AND {_active_file_clause('f')}
              AND NOT EXISTS (
                  SELECT 1
                  FROM source_versions historical_version
                  JOIN knowledge_sources historical_source
                    ON historical_source.id=historical_version.source_id
                  WHERE historical_version.standard_file_id=f.id
                    AND historical_source.current_version_id != historical_version.id
              )
            ORDER BY f.main_category, f.sub_category, f.title
            """,
            params,
        ).fetchall()
    finally:
        if owns_conn:
            conn.close()


def pack_export_preflight(
    pack_id: int, include_low_confidence: bool = False
) -> PackExportPreflight:
    with connect() as conn:
        pack = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        if pack is None:
            raise ValueError("能力包不存在")
        files = files_for_pack(
            pack_id, conn=conn, include_low_confidence=include_low_confidence
        )
        return _pack_export_preflight(conn, pack, files)


def _pack_export_preflight(conn, pack, files) -> PackExportPreflight:
    missing: list[MissingPackArtifact] = []
    if not files:
        missing.append(MissingPackArtifact(None, pack["name"], "knowledge", "标签配方没有匹配到可用知识资料。"))
    for file in files:
        source_path = Path(file["source_path"])
        if pack["include_source"] and not source_path.is_file():
            missing.append(MissingPackArtifact(file["id"], file["title"], "source", "源资料文件不存在。"))
        if file["library_type"] != "standard":
            continue
        for artifact_type, enabled in (
            ("sop", bool(pack["include_sop"])),
            ("insight", bool(pack["include_insight"])),
        ):
            if not enabled:
                continue
            row = conn.execute(
                """
                SELECT path FROM artifacts
                WHERE file_id=? AND artifact_type=?
                ORDER BY id DESC LIMIT 1
                """,
                (file["id"], artifact_type),
            ).fetchone()
            if row is None or not Path(row["path"]).is_file():
                missing.append(MissingPackArtifact(
                    file["id"], file["title"], artifact_type,
                    f"缺少可用的 {artifact_type.upper()} 产物。",
                ))
    return PackExportPreflight(int(pack["id"]), len(files), tuple(missing))


def export_pack(pack_id: int, export_format: str = "zip", include_low_confidence: bool = False) -> Path:
    seed_defaults()
    with connect() as conn:
        pack = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        if not pack:
            raise ValueError("能力包不存在")
        files = files_for_pack(pack_id, conn=conn, include_low_confidence=include_low_confidence)
        preflight = _pack_export_preflight(conn, pack, files)
        if not preflight.ready:
            raise PackExportBlockedError(preflight)
        PACK_DIR.mkdir(parents=True, exist_ok=True)
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
            "snapshot": [],
        }
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
            snapshot_artifacts: dict[str, str] = {}
            if pack["include_source"]:
                source_rel = write_export_file(source_dir, index, file["title"], source, "_source")
                if source_rel:
                    links.append(f"[原文]({source_rel})")
                    snapshot_artifacts["source"] = source_rel
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
                        snapshot_artifacts["sop"] = sop_rel
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
                        snapshot_artifacts["insight"] = insight_rel
            identity = conn.execute(
                """
                SELECT ks.id AS source_id, sv.id AS version_id, sv.version_number
                FROM source_versions sv
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                WHERE sv.standard_file_id=? AND ks.current_version_id=sv.id
                LIMIT 1
                """,
                (file["id"],),
            ).fetchone()
            manifest["snapshot"].append({
                "file_id": file["id"],
                "source_id": identity["source_id"] if identity else None,
                "version_id": identity["version_id"] if identity else None,
                "version_number": identity["version_number"] if identity else None,
                "title": file["title"],
                "library_type": file["library_type"],
                "artifacts": snapshot_artifacts,
            })
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
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
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


DOMAIN_ACCENTS = {"forest", "wine", "brass", "blue", "plum"}


def settings_data(tag_q: str = "", tag_usage: str = "all") -> dict[str, Any]:
    with connect() as conn:
        prompts = conn.execute("SELECT * FROM prompts ORDER BY name, version").fetchall()
        active = _active_file_clause("usage_file")
        tag_filters = []
        tag_params: list[Any] = []
        if tag_q.strip():
            tag_filters.append("t.name LIKE ?")
            tag_params.append(f"%{tag_q.strip()}%")
        if tag_usage == "used":
            tag_filters.append("EXISTS (SELECT 1 FROM tag_assignments used_ta WHERE used_ta.tag_id=t.id)")
        elif tag_usage == "unused":
            tag_filters.append("NOT EXISTS (SELECT 1 FROM tag_assignments used_ta WHERE used_ta.tag_id=t.id)")
        tags = conn.execute(
            f"""
            SELECT t.*, (
                SELECT COUNT(*) FROM tag_assignments ta
                JOIN files usage_file ON ta.target_type='file' AND ta.target_id=usage_file.id
                WHERE ta.tag_id=t.id AND {active}
            ) AS usage_count
            FROM tags t
            {('WHERE ' + ' AND '.join(tag_filters)) if tag_filters else ''}
            ORDER BY usage_count DESC, name
            LIMIT 120
            """,
            tag_params,
        ).fetchall()
        settings = conn.execute("SELECT * FROM settings ORDER BY key").fetchall()
        raw_main_categories = [
            row["main_category"] for row in conn.execute(
                "SELECT DISTINCT main_category FROM files WHERE main_category IS NOT NULL AND main_category<>'' ORDER BY main_category"
            )
        ]
        main_categories = sorted({category.removesuffix("_Intel") for category in raw_main_categories})
        raw_tag_roots = [
            row["root"] for row in conn.execute(
                "SELECT DISTINCT CASE WHEN instr(name, '/')>0 THEN substr(name, 1, instr(name, '/')-1) ELSE name END AS root FROM tags ORDER BY root"
            )
        ]
        tag_roots = sorted({root.removesuffix("_Intel") for root in raw_tag_roots})
        migration_runs = conn.execute(
            "SELECT * FROM legacy_migration_runs ORDER BY id DESC LIMIT 5"
        ).fetchall()
    setting_values = {row["key"]: row["value"] for row in settings}
    domains = [
        {
            **dict(item["domain"]),
            "count": item["count"],
            "rules": [dict(rule) for rule in item["rules"]],
        }
        for item in list_knowledge_domains(enabled_only=False)
    ]
    return {
        "prompts": prompts,
        "tags": tags,
        "settings": settings,
        "setting_values": setting_values,
        "domains": domains,
        "main_categories": main_categories,
        "tag_roots": tag_roots,
        "domain_accents": sorted(DOMAIN_ACCENTS),
        "migration_runs": migration_runs,
    }


def save_knowledge_domain(
    domain_id: int | None,
    name: str,
    accent_key: str,
    main_categories: list[str],
    tag_prefixes: list[str],
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("知识域名称不能为空")
    accent = accent_key if accent_key in DOMAIN_ACCENTS else "forest"
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if domain_id is None:
            order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM knowledge_domains").fetchone()[0]
            domain_id = int(conn.execute(
                "INSERT INTO knowledge_domains(name, accent_key, sort_order) VALUES (?, ?, ?)",
                (name, accent, order),
            ).lastrowid)
        else:
            conn.execute(
                "UPDATE knowledge_domains SET name=?, accent_key=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, accent, domain_id),
            )
            conn.execute("DELETE FROM knowledge_domain_rules WHERE domain_id=?", (domain_id,))
        def paired_roots(values: list[str]) -> list[str]:
            paired = []
            for value in values:
                base = value.strip().removesuffix("_Intel")
                if base:
                    paired.extend((base, f"{base}_Intel"))
            return paired

        normalized_categories = paired_roots(main_categories)
        normalized_prefixes = paired_roots(tag_prefixes)
        for rule_type, values in (("main_category", normalized_categories), ("tag_prefix", normalized_prefixes)):
            for value in dict.fromkeys(item.strip() for item in values if item.strip()):
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_domain_rules(domain_id, rule_type, match_value) VALUES (?, ?, ?)",
                    (domain_id, rule_type, value),
                )
    return int(domain_id)


def set_knowledge_domain_enabled(domain_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE knowledge_domains SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(enabled), domain_id),
        )


def move_knowledge_domain(domain_id: int, direction: str) -> None:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT id, sort_order FROM knowledge_domains WHERE id=?", (domain_id,)).fetchone()
        if current is None:
            raise KeyError(domain_id)
        operator, order = ("<", "DESC") if direction == "up" else (">", "ASC")
        neighbor = conn.execute(
            f"SELECT id, sort_order FROM knowledge_domains WHERE sort_order {operator} ? ORDER BY sort_order {order}, id {order} LIMIT 1",
            (current["sort_order"],),
        ).fetchone()
        if neighbor:
            conn.execute("UPDATE knowledge_domains SET sort_order=? WHERE id=?", (neighbor["sort_order"], current["id"]))
            conn.execute("UPDATE knowledge_domains SET sort_order=? WHERE id=?", (current["sort_order"], neighbor["id"]))


def delete_knowledge_domain(domain_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_domains WHERE id=?", (domain_id,))


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


def restore_prompt_version(prompt_id: int) -> str:
    with connect() as conn:
        prompt = conn.execute("SELECT name, content FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if prompt is None:
        raise KeyError(prompt_id)
    return create_prompt_version(prompt["name"], prompt["content"])


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
