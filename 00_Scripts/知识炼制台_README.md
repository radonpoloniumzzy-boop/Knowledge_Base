# 知识炼制台 MVP

## 快捷启动

推荐入口在 `D:\Knowledge_Base` 根目录：

- `启动知识炼制台.bat`：双击后自动检查依赖、启动本地服务并打开浏览器。
- `安装或修复环境.bat`：安装或修复所需 Python 依赖，并初始化本地数据库。
- `创建桌面快捷方式.bat`：在桌面创建“知识炼制台”快捷方式。
- `停止知识炼制台.bat`：关闭后台运行的知识炼制台服务。

桌面快捷方式已经创建到：

```text
C:\Users\ZHONG SHIXING\Desktop\知识炼制台.lnk
```

## 访问地址

默认地址：

```text
http://127.0.0.1:8765
```

如果 8765 已被其他本地项目占用，启动脚本会自动使用 8766、8767 等后续空闲端口。当前实际地址会写入：

```text
D:\Knowledge_Base\Knowledge_Forge\last_url.txt
```

本次检测到 8765 被其他项目占用，知识炼制台已运行在：

```text
http://127.0.0.1:8766
```

## 手动启动

如果需要从命令行启动：

```powershell
cd D:\Knowledge_Base\00_Scripts
python knowledge_forge.py
```

也可以指定端口：

```powershell
$env:KNOWLEDGE_FORGE_PORT = "8766"
python knowledge_forge.py
```

## 第一版能力

- 同步现有 `Standard_Library`、`SOP_Production`、`Insight_Library` 到 SQLite。
- 查看仪表盘、知识库、右侧详情抽屉、文件详情、标签、SOP/Insight 预览。
- 知识库支持按主分类下拉、层级标签、状态和产物筛选。
- 能力包支持从两到三级标签树勾选大类或子类，并支持滚动选择。
- 上传 md/txt/pdf/doc/docx/ppt/xlsx 等资料，转换为清洗 Markdown 后进入 `Standard_Library\00_Pending_Review`。
- 上传后自动生成离线草稿：`structure`、`sop`、`insight`；文件详情页可重新生成，旧版本会保留。
- 按能力包配方导出 `ZIP + chunks.jsonl + manifest.json`，并按开关输出 `sources/`、`artifacts/sop/`、`artifacts/insight/`。
- 能力包配方可编辑，也可以新建能力包。
- 设置页可编辑普通配置、提示词版本和标签树。

当前版本默认离线优先，不会主动发起 OpenAI API 请求。
