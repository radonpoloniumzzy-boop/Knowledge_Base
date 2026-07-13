# Domain Docs

## 开发前读取

- 根目录的 `CONTEXT.md`
- 与当前修改相关的 `docs/adr/` 文件

文件不存在时继续工作，不因此阻塞。`/domain-modeling`、`/grill-with-docs` 和 `/improve-codebase-architecture` 会在需要时逐步建立内容。

## 项目结构

本项目采用单一上下文：

```text
/
├── CONTEXT.md
├── docs/adr/
└── 00_Scripts/
```

开发任务、测试名称和设计文档应采用 `CONTEXT.md` 中定义的项目术语。如果方案与现有 ADR 冲突，必须明确指出冲突，不能静默覆盖。
