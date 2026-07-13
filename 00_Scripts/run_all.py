import subprocess
import time
import sys
from pathlib import Path

# ================= ⚙️ 动态配置 =================
# 获取当前脚本所在的目录 (00_Scripts)
CURRENT_DIR = Path(__file__).parent

# 定义要运行的脚本名称 (顺序很重要)
SCRIPT_NAMES = [
    "ingest_unified.py",      # 1. 吞吐清洗
    "sop_generator.py",       # 2. SOP 生产
    "insight_generator.py"    # 3. 研报生产
]

# =================================================

def run_script(script_name):
    # 动态拼接完整路径
    script_path = CURRENT_DIR / script_name
    
    print(f"\n{'='*50}")
    print(f"🚀 正在启动: {script_name}")
    print(f"{'='*50}\n")
    
    if not script_path.exists():
        print(f"❌ 致命错误: 找不到文件 {script_path}")
        return False

    try:
        # 调用当前环境的 python 解释器
        result = subprocess.run([sys.executable, str(script_path)], check=True)
        if result.returncode == 0:
            print(f"\n✅ 执行成功: {script_name}")
            return True
        else:
            print(f"\n❌ 执行失败 (Code {result.returncode})")
            return False
            
    except subprocess.CalledProcessError as e:
        print(f"\n❌ 运行出错: {e}")
        return False
    except Exception as e:
        print(f"\n❌ 未知错误: {e}")
        return False

def main():
    start_time = time.time()
    print("🌟 知识库全流程自动化 (便携版) 开始 🌟")
    print(f"📂 脚本工作目录: {CURRENT_DIR}")
    
    for name in SCRIPT_NAMES:
        success = run_script(name)
        if not success:
            print("\n⚠️ 警告: 上一步执行失败，流程可能不完整。")
            # 你可以在这里决定是否要中断后续步骤 (例如: break)
        
        # 稍微停顿，避免文件锁冲突
        time.sleep(2)

    duration = time.time() - start_time
    print(f"\n{'='*50}")
    print(f"🎉 所有任务全部完成！耗时: {duration:.2f} 秒")
    print(f"{'='*50}")
    input("按 Enter 键退出...")

if __name__ == "__main__":
    main()