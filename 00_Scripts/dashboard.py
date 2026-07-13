import json
import webbrowser
import os
from pathlib import Path
from datetime import datetime

# ================= ⚙️ 配置加载 =================
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, 'r', encoding='utf-8') as f: CONFIG = json.load(f)

STD_DIR = Path(CONFIG["paths"]["std_dir"])
SOP_DIR = Path(CONFIG["paths"]["sop_dir"])
INSIGHT_DIR = Path(CONFIG["paths"]["insight_dir"])

# ================= 🧠 数据索引核心 =================

def scan_library(directory, tag_prefix):
    """
    扫描目录，生成用于搜索的索引数据
    """
    stats = {"count": 0, "size": 0, "cats": {}}
    file_index = [] # 用于前端搜索的数据

    for f in directory.rglob("*.md"):
        stats["count"] += 1
        size = f.stat().st_size
        stats["size"] += size
        
        # 提取分类
        try:
            cat = f.relative_to(directory).parts[0]
            stats["cats"][cat] = stats["cats"].get(cat, 0) + 1
        except: cat = "Uncategorized"

        # 构建索引条目 (只取最近修改的 1000 个，防止 HTML 太大)
        mtime = f.stat().st_mtime
        file_index.append({
            "name": f.name,
            "path": str(f),  # 绝对路径，用于点击打开
            "cat": cat,
            "type": tag_prefix, # Standard/SOP/Insight
            "time": mtime,
            "date_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        })

    # 按时间倒序排列
    file_index.sort(key=lambda x: x["time"], reverse=True)
    return stats, file_index

# ================= 🎨 UI 生成核心 =================

def generate_html():
    print("📊 正在建立全库索引...")
    
    # 扫描三大库
    std_s, std_idx = scan_library(STD_DIR, "📚 Standard")
    sop_s, sop_idx = scan_library(SOP_DIR, "🛠️ SOP")
    ins_s, ins_idx = scan_library(INSIGHT_DIR, "🧠 Insight")
    
    # 合并搜索索引 (取最新的 2000 个文件作为预览)
    all_files = (std_idx + sop_idx + ins_idx)
    all_files.sort(key=lambda x: x["time"], reverse=True)
    search_data = json.dumps(all_files[:2000]) 

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>知识库指挥中心 (Pro)</title>
        <meta charset="utf-8">
        <style>
            :root {{ --primary: #2563eb; --bg: #f8fafc; --card: #ffffff; --text: #1e293b; }}
            body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 20px; max-width: 1400px; margin: 0 auto; }}
            
            /* Header */
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }}
            h1 {{ font-size: 24px; margin: 0; display: flex; align-items: center; gap: 10px; }}
            .badge {{ background: #dbeafe; color: #1e40af; padding: 5px 10px; border-radius: 20px; font-size: 14px; }}

            /* Cards */
            .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px; }}
            .card {{ background: var(--card); padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }}
            .stat-num {{ font-size: 32px; font-weight: 700; color: var(--primary); }}
            .stat-sub {{ font-size: 14px; color: #64748b; margin-bottom: 15px; }}
            .cat-row {{ display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; border-bottom: 1px dashed #f1f5f9; }}

            /* Search Bar */
            .search-section {{ background: var(--card); padding: 20px; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); }}
            .search-input {{ width: 100%; padding: 15px; font-size: 16px; border: 2px solid #e2e8f0; border-radius: 8px; outline: none; transition: 0.2s; box-sizing: border-box; }}
            .search-input:focus {{ border-color: var(--primary); }}

            /* File List */
            .file-list {{ margin-top: 20px; max-height: 600px; overflow-y: auto; }}
            .file-item {{ display: flex; align-items: center; padding: 12px; border-bottom: 1px solid #f1f5f9; transition: 0.1s; }}
            .file-item:hover {{ background: #f1f5f9; }}
            .file-icon {{ margin-right: 15px; font-size: 20px; width: 30px; text-align: center; }}
            .file-info {{ flex: 1; }}
            .file-name {{ font-weight: 500; color: #334155; text-decoration: none; cursor: pointer; }}
            .file-name:hover {{ color: var(--primary); text-decoration: underline; }}
            .file-meta {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
            .tag {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-right: 5px; }}
            .tag-std {{ background: #e0f2fe; color: #0284c7; }}
            .tag-sop {{ background: #dcfce7; color: #16a34a; }}
            .tag-ins {{ background: #fce7f3; color: #db2777; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🚀 知识库指挥中心 <span class="badge">V3.0 Pro</span></h1>
            <div style="color: #64748b;">最后更新: {now}</div>
        </div>
        
        <div class="grid">
            <div class="card">
                <div>📚 Standard Library</div>
                <div class="stat-num">{std_s['count']}</div>
                <div class="stat-sub">容量: {std_s['size']/1024/1024:.1f} MB</div>
                <div>{''.join(f'<div class="cat-row"><span>{k}</span><span>{v}</span></div>' for k,v in std_s['cats'].items())}</div>
            </div>
            <div class="card">
                <div>🛠️ SOP Production</div>
                <div class="stat-num">{sop_s['count']}</div>
                <div class="stat-sub">实操手册</div>
                <div>{''.join(f'<div class="cat-row"><span>{k}</span><span>{v}</span></div>' for k,v in sop_s['cats'].items())}</div>
            </div>
            <div class="card">
                <div>🧠 Insight Library</div>
                <div class="stat-num">{ins_s['count']}</div>
                <div class="stat-sub">情报/研报</div>
                <div>{''.join(f'<div class="cat-row"><span>{k}</span><span>{v}</span></div>' for k,v in ins_s['cats'].items())}</div>
            </div>
        </div>

        <div class="search-section">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 搜索知识库... (输入标题、分类、ID或日期)">
            <div id="fileList" class="file-list">
                </div>
        </div>

        <script>
            // 嵌入的索引数据
            const allFiles = {search_data};

            const searchInput = document.getElementById('searchInput');
            const fileList = document.getElementById('fileList');

            function renderFiles(files) {{
                if (files.length === 0) {{
                    fileList.innerHTML = '<div style="padding:20px; text-align:center; color:#94a3b8">没有找到匹配的文件</div>';
                    return;
                }}
                
                const html = files.map(f => {{
                    let tagClass = 'tag-std';
                    if (f.type.includes('SOP')) tagClass = 'tag-sop';
                    if (f.type.includes('Insight')) tagClass = 'tag-ins';
                    
                    // 只有在 Windows 本地环境下，file:// 协议才能直接工作
                    // 或者使用 Python 开启一个简易 server。
                    // 这里直接显示路径，复制可用。
                    return `
                    <div class="file-item">
                        <div class="file-icon">${{f.type.includes('SOP') ? '🛠️' : (f.type.includes('Insight') ? '🧠' : '📚')}}</div>
                        <div class="file-info">
                            <a href="file:///${{f.path.replace(/\\\\/g, '/')}}" class="file-name" target="_blank">${{f.name}}</a>
                            <div class="file-meta">
                                <span class="tag ${{tagClass}}">${{f.type}}</span>
                                <span class="tag" style="background:#f1f5f9">${{f.cat}}</span>
                                <span>📅 ${{f.date_str}}</span>
                            </div>
                        </div>
                    </div>
                    `;
                }}).join('');
                fileList.innerHTML = html;
            }}

            // 初始渲染前 50 条
            renderFiles(allFiles.slice(0, 50));

            // 搜索过滤逻辑
            searchInput.addEventListener('input', (e) => {{
                const term = e.target.value.toLowerCase();
                const filtered = allFiles.filter(f => 
                    f.name.toLowerCase().includes(term) || 
                    f.cat.toLowerCase().includes(term) ||
                    f.date_str.includes(term)
                );
                // 限制显示数量，防止卡顿
                renderFiles(filtered.slice(0, 100));
            }});
        </script>
    </body>
    </html>
    """
    
    report_path = Path(__file__).parent / "dashboard.html"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"📊 仪表盘生成完毕: {report_path}")
    print("🌍 正在打开浏览器...")
    webbrowser.open(report_path.as_uri())

if __name__ == "__main__":
    generate_html()