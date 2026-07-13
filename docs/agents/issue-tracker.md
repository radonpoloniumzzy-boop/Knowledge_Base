# Issue Tracker: Local Markdown

项目的需求规格和开发任务存放在 `.scratch/`。

## 目录约定

- 每项功能一个目录：`.scratch/<feature-slug>/`
- 规格文件：`.scratch/<feature-slug>/spec.md`
- 开发任务：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- 每个任务单独一个文件，从 `01` 开始编号
- 任务状态写在文件顶部的 `Status:` 字段
- 讨论记录追加到文件底部的 `## Comments`

## Skill 操作规则

- “发布到任务跟踪器”：在 `.scratch/<feature-slug>/` 创建文件
- “读取任务”：读取用户指定的任务路径或编号
- `/wayfinder` 使用 `.scratch/<effort>/map.md` 记录调查地图
- 子任务使用 `Blocked by:` 声明依赖
- 未被阻塞且未被领取的最小编号任务优先处理
