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
INSIGHT_ROOT = Path(CONFIG["paths"]["insight_dir"]) # 注意这里是 Insight 目录
FOLDER_MAP = CONFIG["folder_mapping"] # 复用同一套映射，但加上后缀

API_KEY = os.getenv(CONFIG["api_settings"].get("api_key_env", "DEEPSEEK_API_KEY")) or CONFIG["api_settings"].get("api_key", "")
BASE_URL = CONFIG["api_settings"]["base_url"]
MAX_WORKERS = 3
# =================================================

client = OpenAI(api_key=API_KEY, base_url=BASE_URL) if API_KEY else None

LOG_FILE = INSIGHT_ROOT / "_insight_gen.log"
if not INSIGHT_ROOT.exists(): INSIGHT_ROOT.mkdir(parents=True)
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO, 
    format='%(asctime)s - %(message)s', encoding='utf-8'
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

def determine_target_path(file_path):
    try:
        rel_path = file_path.relative_to(STD_DIR)
        top_category = rel_path.parts[0]
        
        # 查表：金融 -> 01_Finance
        base_name = FOLDER_MAP.get(top_category, FOLDER_MAP["__DEFAULT__"])
        # 为了区分，Insight 库可以在名字后面加个后缀，或者直接复用
        # 这里建议加后缀: 01_Finance_Intel
        target_folder_name = f"{base_name}_Intel"
        
        root_target = INSIGHT_ROOT / target_folder_name
        
        if len(rel_path.parts) > 2:
            sub_path = Path(*rel_path.parts[1:-1])
            final_dir = root_target / sub_path
        else:
            final_dir = root_target

        final_dir.mkdir(parents=True, exist_ok=True)
        return final_dir, top_category
    except:
        return INSIGHT_ROOT / "99_General_Intel", "Unknown"

def generate_insight(title, content):
    prompt = f"""
    Role: Analyst. Task: Extract Facts, Data, Logic.
    Input: {title}\n{content[:4000]}
    Output Structure:
    # Brief: {title}
    ## TL;DR
    ## Key Findings
    ## Critical Data
    ## Logical Analysis
    """
    return call_ai_robust(prompt, CONFIG["api_settings"]["model"])

# ==========================================
# 👇 把下面这个函数完整复制，替换掉你原来的 process_file
# ==========================================

def process_file(file_path):
    target_dir, category = determine_target_path(file_path)
    
    match = re.search(r'_\[([a-f0-9]{12})\]\.md$', file_path.name)
    if match:
        fid = match.group(1)
    else:
        fid = hashlib.md5(file_path.stem.encode('utf-8')).hexdigest()[:12]
    
    if any(target_dir.glob(f"*_{fid}.md")): 
        return 

    try:
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
    except Exception as e:
        logging.error(f"❌ 读取失败: {file_path.name}")
        return

    logging.info(f"⚙️ 正在生成: {file_path.stem}")

    # 🔥 【关键修正 1】：这里调用的是 generate_insight，并且只传两个参数！
    result_text = generate_insight(file_path.stem, content)
    
    if not result_text: return

    today = time.strftime('%Y%m%d')
    safe_title = re.sub(r'[\\/:*?"<>|]', '', file_path.stem)[:30]
    
    # 🔥 【关键修正 2】：前缀改成 [Insight]
    final_name = f"[Insight]_{today}_{safe_title}_{fid}.md"
    
    try:
        with open(target_dir / final_name, 'w', encoding='utf-8') as f:
            f.write(result_text)
        logging.info(f"✅ [生成完毕] {final_name}")
        print(f"✅ 生成: {final_name}") 
    except Exception as e:
        logging.error(f"❌ 写入失败: {final_name} -> {e}")

def main():
    print("🚀 Insight 分库生成 (Config驱动版) 启动...")
    files = [f for f in STD_DIR.rglob("*.md")]
    print(f"📊 扫描: {len(files)} 文件")
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        list(ex.map(process_file, files))
    print(f"✅ 完成")

if __name__ == "__main__":
    main()
