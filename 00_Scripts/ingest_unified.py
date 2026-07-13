import os
import re
import json
import shutil
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from markitdown import MarkItDown  # pip install markitdown
from openai import OpenAI

# ================= ⚙️ 配置加载 =================
CURRENT_DIR = Path(__file__).parent
CONFIG_PATH = CURRENT_DIR / "config.json"

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
except Exception as e:
    print(f"❌ [System Error] 无法读取配置文件: {e}")
    sys.exit(1)

# 路径映射
RAW_DIR = Path(CONFIG["paths"]["raw_dir"])
STD_DIR = Path(CONFIG["paths"]["std_dir"])
ARCHIVE_DIR = RAW_DIR / "_Archived"

# 🔥 关键改动：读取完整的分类树，不仅仅是 Keys
FULL_TAXONOMY = CONFIG.get("taxonomy", {})

# API 配置
API_KEY = os.getenv(CONFIG["api_settings"].get("api_key_env", "DEEPSEEK_API_KEY")) or CONFIG["api_settings"].get("api_key", "")
BASE_URL = CONFIG["api_settings"]["base_url"]
MODEL_NAME = CONFIG["api_settings"]["model"]

client = OpenAI(api_key=API_KEY, base_url=BASE_URL) if API_KEY else None

# 确保目录存在
for p in [STD_DIR, ARCHIVE_DIR]:
    if not p.exists(): p.mkdir(parents=True)

# 日志设置
LOG_FILE = RAW_DIR / "_ingestion_history.log"
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO,
    format='%(asctime)s - [DeepTaxonomy] - %(message)s', 
    encoding='utf-8'
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

MAX_WORKERS = 5

# ================= 🧹 基础清洗 =================
def murphy_nuke_garbage(content: str) -> str:
    if not content: return ""
    content = re.sub(r'# >>> 来源:.*?<<<', '', content, flags=re.DOTALL)
    content = re.sub(r'> 🔗 Source:.*?(\n|$)', '', content, flags=re.MULTILINE)
    content = re.sub(r'\[\d{1,2}:\d{2}(:\d{2})?\]|\(\d{1,2}:\d{2}\)', '', content)
    return content.strip()

# ================= 🧠 AI 二级分类引擎 (The Sniper) =================
def ask_ai_to_classify_deep(filename: str, content: str) -> tuple:
    """
    要求 AI 返回 '一级分类/二级分类'
    """
    snippet = content[:3000]
    if client is None:
        logging.error("❌ API key missing. Set DEEPSEEK_API_KEY or update api_settings.api_key_env.")
        return "Uncategorized", "API_Key_Missing"
    
    # 将你的分类树转成字符串，喂给 AI
    taxonomy_str = json.dumps(FULL_TAXONOMY, ensure_ascii=False, indent=2)

    prompt = f"""
    你是一个严谨的档案管理员。
    请分析以下文件的内容，将其归类到给定的分类体系中。
    
    📂 分类体系 (JSON):
    {taxonomy_str}

    📄 文件信息:
    文件名: {filename}
    内容摘要: {snippet}

    ⚡ 任务要求:
    1. 必须从分类体系中选择最匹配的 [一级分类] 和 [二级子分类]。
    2. 如果内容匹配一级分类但没有合适的二级子分类，二级分类请输出 "其他"。
    3. 如果完全不匹配任何一级分类，请输出 "Uncategorized/Unknown"。
    4. 输出格式必须严格为: "一级分类/二级分类"
    5. 不要解释，只要结果。

    示例输出: "金融/主力资金博弈" 或 "计算机/Python核心语法"
    """

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是一个分类引擎。只输出路径格式 'Main/Sub'。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=20
        )
        result = response.choices[0].message.content.strip()
        
        # 清理可能出现的引号或句号
        result = re.sub(r'["\'。]', '', result)
        
        # 尝试分割
        if "/" in result:
            parts = result.split("/")
            main_cat = parts[0].strip()
            sub_cat = parts[1].strip()
            
            # 简单校验：一级分类是否在我们的 Key 里？
            if main_cat in FULL_TAXONOMY:
                return main_cat, sub_cat
            else:
                return "Uncategorized", "Unknown"
        else:
            return "Uncategorized", "Unknown"

    except Exception as e:
        logging.error(f"❌ API 调用失败: {e}")
        return "Uncategorized", "API_Error"

# ================= 🚀 主处理逻辑 =================
def process_file(file_path: Path):
    try:
        logging.info(f"🔄 [开始] {file_path.name}")
        
        # Step 1: 格式转换
        md = MarkItDown()
        result = md.convert(str(file_path))
        raw_text = result.text_content
        
        if not raw_text: return

        # Step 2: 清洗
        clean_text = murphy_nuke_garbage(raw_text)
        
        # Step 3: AI 深度分类 (Deep Classification)
        main_cat, sub_cat = ask_ai_to_classify_deep(file_path.name, clean_text)
        logging.info(f"🤖 AI 判决: [{main_cat}] -> [{sub_cat}]")
        
        # Step 4: 写入嵌套目录 (Standard / 金融 / 主力资金博弈 / xxx.md)
        # 如果是 Uncategorized，就只放一层
        if main_cat == "Uncategorized":
            target_dir = STD_DIR / "Uncategorized"
        else:
            target_dir = STD_DIR / main_cat / sub_cat
            
        if not target_dir.exists():
            target_dir.mkdir(parents=True)
            
        new_filename = file_path.stem + ".md"
        target_path = target_dir / new_filename
        
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(clean_text)

        # Step 5: 归档
        target_archive = ARCHIVE_DIR / file_path.name
        if target_archive.exists(): target_archive.unlink()
        shutil.move(str(file_path), str(target_archive))
        
        # 处理关联文件
        for related in RAW_DIR.glob(f"{re.escape(file_path.stem)}*"):
            if related != file_path:
                try:
                    target_rel = ARCHIVE_DIR / related.name
                    if target_rel.exists(): target_rel.unlink()
                    shutil.move(str(related), str(target_rel))
                except: pass

    except Exception as e:
        logging.error(f"❌ 处理炸了 {file_path.name}: {e}")

# 新代码 (修改 main 函数)
def main():
    print("💎 [Murphy Deep Taxonomy Ingestion] Activated.")
    print(f"🧠 Taxonomy Loaded: {len(FULL_TAXONOMY)} main categories.")
    
    # 扩大支持的后缀名集合 (MarkItDown 官方支持大多数主流格式)
    SUPPORTED_EXTS = {
        # 文档类
        '.md', '.pdf', '.docx', '.doc', '.txt', '.html', '.csv', '.json',
        # 演示与表格类 (MarkItDown 原生支持)
        '.pptx', '.ppt', '.xlsx', '.xls',
        # 甚至可以加上代码或常见配置
        '.py', '.java', '.xml'
    }
    
    files = [f for f in RAW_DIR.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS and f.name != "config.json"]
    
    if not files:
        print("zzz... 没文件。")
        return

    print(f"🔥 发现 {len(files)} 个文件，开始深度分类...")
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_file, f): f for f in files}
        for future in as_completed(futures):
            future.result()
            
    print(f"\n✅ 全部搞定。现在的目录结构应该很漂亮了。")

if __name__ == "__main__":
    main()
