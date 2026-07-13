import hashlib
import os
import re
import json
import time
import random
import logging
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI

# ... (其他的 import)

# 🟢 [新增] 墨非的稳健重试装饰器逻辑
def call_ai_robust(prompt, model, max_retries=5):
    """
    带复活甲的 API 调用。
    遇到限流 (429) 会自动睡觉等待，遇到超长会跳过。
    """
    if client is None:
        logging.error("❌ [API Key Missing] Set DEEPSEEK_API_KEY or update api_settings.api_key_env.")
        return None
    for attempt in range(max_retries):
        try:
            # 这里的 timeout 很重要，防止挂死
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                timeout=60 
            )
            return response.choices[0].message.content.strip()

        except Exception as e:
            error_str = str(e)
            
            # 情况 A: 触发限流 (429) -> 睡一觉再战
            if "429" in error_str or "Too Many Requests" in error_str:
                wait_time = (2 ** attempt) + random.uniform(1, 3) # 指数避退: 2s, 4s, 8s...
                logging.warning(f"⏳ [API限流] 触发冷却，等待 {wait_time:.1f}秒... (第 {attempt+1} 次重试)")
                print(f"⏳ API 繁忙，休息 {wait_time:.1f}秒...") # 打印到控制台让你看见
                time.sleep(wait_time)
                continue
            
            # 情况 B: 上下文超长 -> 这种救不回来，直接放弃
            elif "context_length_exceeded" in error_str:
                logging.error(f"❌ [超长] 文件太长，超过模型限制。跳过。")
                return "SKIP_Context_Limit"
            
            # 情况 C: 其他网络错误 -> 稍微等等再试
            else:
                logging.error(f"⚠️ [API错误] {error_str} - 稍后重试")
                time.sleep(2)
                continue

    logging.error(f"💀 [死亡] 重试 {max_retries} 次后依然失败。")
    return None
# ================= ⚙️ 加载配置 =================
CONFIG_PATH = Path(__file__).parent / "config.json"
try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: CONFIG = json.load(f)
except: sys.exit("Missing config.json")

STD_DIR = Path(CONFIG["paths"]["std_dir"])
SOP_ROOT = Path(CONFIG["paths"]["sop_dir"])
FOLDER_MAP = CONFIG["folder_mapping"]

API_KEY = os.getenv(CONFIG["api_settings"].get("api_key_env", "DEEPSEEK_API_KEY")) or CONFIG["api_settings"].get("api_key", "")
BASE_URL = CONFIG["api_settings"]["base_url"]
MAX_WORKERS = 3
# =================================================

client = OpenAI(api_key=API_KEY, base_url=BASE_URL) if API_KEY else None

# 日志配置
LOG_FILE = SOP_ROOT / "_sop_gen.log"
if not SOP_ROOT.exists(): SOP_ROOT.mkdir(parents=True)
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO, 
    format='%(asctime)s - %(message)s', encoding='utf-8'
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

def determine_target_path(file_path):
    """
    🧭 动态路由：完全基于 config.json 的映射
    """
    try:
        rel_path = file_path.relative_to(STD_DIR)
        top_category = rel_path.parts[0] # 如 "金融"
        
        # 查表：金融 -> 01_Finance_KB
        # 如果 JSON 里没写这个分类，就丢到 __DEFAULT__
        target_folder_name = FOLDER_MAP.get(top_category, FOLDER_MAP["__DEFAULT__"])
        
        # 拼接: D:\Knowledge_Base\SOP_Production\01_Finance_KB
        root_target = SOP_ROOT / target_folder_name
        
        # 保持子目录: 01_Finance_KB\交易
        if len(rel_path.parts) > 2:
            sub_path = Path(*rel_path.parts[1:-1])
            final_dir = root_target / sub_path
        else:
            final_dir = root_target

        final_dir.mkdir(parents=True, exist_ok=True)
        return final_dir, top_category
    except:
        return SOP_ROOT / FOLDER_MAP["__DEFAULT__"], "Unknown"

def generate_sop(title, category, content):
    prompt = f"""
    Role: SOP Engineer. Domain: {category}.
    Input: {title}\n{content[:3500]}
    Task: Create an ACTIONABLE SOP.
    Rules:
    1. If pure theory -> "NO_SOP_POSSIBLE".
    2. Case Studies -> Extract steps.
    3. Output Markdown with Checklist.
    """
    return call_ai_robust(prompt, CONFIG["api_settings"]["model"])
    
# ==========================================
# 👇 把下面这个函数完整复制，替换掉你原来的 process_file
# ==========================================

def process_file(file_path):
    # 1. 确定目标目录和分类
    target_dir, category = determine_target_path(file_path)
    
    # 2. 🔥【修复核心】智能 ID 生成逻辑
    # 尝试从文件名提取 ID
    match = re.search(r'_\[([a-f0-9]{12})\]\.md$', file_path.name)
    if match:
        fid = match.group(1)
    else:
        # ⚠️ 如果文件名里没有 ID，就用文件名的哈希值做一个！
        # 这样保证每个文件都有唯一的 fid，不会全部变成 "ID" 导致撞车
        fid = hashlib.md5(file_path.stem.encode('utf-8')).hexdigest()[:12]
    
    # 3. 幂等检查 (跳过已存在的文件)
    # 检查目标文件夹里，是否已经有包含这个 fid 的 SOP/Insight
    if any(target_dir.glob(f"*_{fid}.md")): 
        # logging.info(f"⏩ [跳过] 已存在: {file_path.stem}") # 嫌吵可以注释掉
        return 

    # 4. 读取内容
    try:
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
    except Exception as e:
        logging.error(f"❌ 读取失败: {file_path.name}")
        return

    logging.info(f"⚙️ 正在生成: {file_path.stem}")

    # 5. 调用 AI (这里会自动使用不管是 generate_sop 还是 generate_insight)
    # 注意：下面这一行根据当前是哪个脚本，保留原来的调用函数！
    # 如果是 sop_generator.py 用这个:
    result_text = generate_sop(file_path.stem, category, content)
    # 如果是 insight_generator.py 用这个:
    # result_text = generate_insight(file_path.stem, content)
    
    # 6. 校验结果
    if not result_text: return
    # SOP 特有校验 (Insight 脚本请删掉这一行)
    if "NO_SOP_POSSIBLE" in result_text: return 

    # 7. 保存文件
    today = time.strftime('%Y%m%d')
    # 清洗文件名，防止非法字符
    safe_title = re.sub(r'[\\/:*?"<>|]', '', file_path.stem)[:30]
    
    # 构建最终文件名 (SOP 和 Insight 前缀不同，请注意保留原脚本的前缀)
    # SOP 脚本:
    final_name = f"[SOP]_{today}_{safe_title}_{fid}.md"
    # Insight 脚本:
    # final_name = f"[Insight]_{today}_{safe_title}_{fid}.md"
    
    try:
        with open(target_dir / final_name, 'w', encoding='utf-8') as f:
            f.write(result_text)
        logging.info(f"✅ [生成完毕] {final_name}")
        # 打印到控制台让用户看到进度
        print(f"✅ 生成: {final_name}") 
    except Exception as e:
        logging.error(f"❌ 写入失败: {final_name} -> {e}")

def main():
    print("🚀 SOP 分库生成 (Config驱动版) 启动...")
    files = [f for f in STD_DIR.rglob("*.md")]
    print(f"📊 扫描: {len(files)} 文件")
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        list(ex.map(process_file, files))
    print(f"✅ 完成")

if __name__ == "__main__":
    main()
