# Durable Knowledge Ingestion

Status: ready-for-agent

## Problem Statement

资料导入目前依赖应用进程内的临时后台执行。程序关闭、电脑重启或处理中途失败时，任务无法可靠恢复；数据库与磁盘可能留下部分结果；任务状态、重试、版本、分类和衍生产物混在同一流程中。用户无法放心批量导入后离开，也无法清楚判断一份知识资料是可用、未复核、需要处理，还是仅有部分知识增强失败。

现有知识库已经包含大量资料、标签、分块、衍生产物、能力包、提示词和反馈，改造必须保护这些资产，不能要求重新导入或通过猜测重建关系。

## Solution

建立一个持久、可恢复的资料导入 module。每份原始资料形成导入任务，由任务队列单任务串行推进。任务由可完整提交的处理阶段组成，程序重启后从第一个未完成阶段恢复；临时失败最多自动重试三次，确定性失败进入需要处理。用户可以暂停和继续任务，暂停不删除任何资料。

知识资料采用不可变资料版本。完全相同的内容复用已有版本；内容变化产生新版本；新版本完成核心处理前，旧的当前版本继续可用。核心处理成功后，新版本立即成为可用·未复核的当前版本。自动分类、标签、Structure、SOP 和 Insight 作为独立知识增强，不阻塞资料使用。

删除资料采用三十天回收站。现有数据库通过备份、演练和增量迁移进入新模型，无法可靠判断的关系进入迁移报告，不猜测合并。

## User Stories

1. As a knowledge-base user, I want to upload multiple learning files at once, so that I do not need to wait beside the application.
2. As a knowledge-base user, I want each uploaded file to create a visible import task, so that I know nothing was silently lost.
3. As a knowledge-base user, I want queued files processed one at a time, so that my computer remains responsive and task order is understandable.
4. As a knowledge-base user, I want the task queue to survive application shutdown, so that closing the window does not discard work.
5. As a knowledge-base user, I want processing to resume from the first incomplete stage after restart, so that completed work and future AI cost are not repeated.
6. As a knowledge-base user, I want to see the current processing stage in plain language, so that I understand what the system is doing.
7. As a knowledge-base user, I want to see waiting, processing, paused, completed, and needs-attention counts, so that I can understand the queue at a glance.
8. As a knowledge-base user, I want transient failures retried automatically, so that temporary file locks or network problems do not require my attention.
9. As a knowledge-base user, I want automatic retries limited to three, so that a broken task cannot loop forever.
10. As a knowledge-base user, I want deterministic failures to stop immediately with a useful explanation, so that repeated attempts do not waste time.
11. As a knowledge-base user, I want failed tasks to identify the failed stage and suggested correction, so that I know what to fix.
12. As a knowledge-base user, I want to continue a task after correcting its input or configuration, so that I do not need to upload it again.
13. As a knowledge-base user, I want to pause a queued task immediately, so that I can control upcoming work.
14. As a knowledge-base user, I want an active task to pause safely after its current stage, so that no result is corrupted.
15. As a knowledge-base user, I want to continue a paused task, so that processing resumes without losing completed stages.
16. As a knowledge-base user, I want task controls separated from data deletion, so that pausing cannot accidentally remove knowledge.
17. As a knowledge-base user, I want exact duplicate content detected, so that the same lesson is not processed and stored repeatedly.
18. As a knowledge-base user, I want an exact duplicate upload to open the existing knowledge source, so that I can find what was already processed.
19. As a knowledge-base user, I want changed content to create a new source version, so that revisions remain traceable.
20. As a knowledge-base user, I want the previous current version to remain usable while a new version is processing, so that updates do not cause downtime.
21. As a knowledge-base user, I want a failed new version to leave the current version unchanged, so that an unsuccessful update cannot damage available knowledge.
22. As a knowledge-base user, I want only a fully processed source version promoted to current, so that partial content never replaces usable knowledge.
23. As a knowledge-base user, I want half-written documents and partial chunk sets hidden, so that search and packs never consume incomplete results.
24. As a knowledge-base user, I want interrupted stage output discarded or overwritten on recovery, so that a resumed task starts from a known state.
25. As a knowledge-base user, I want the standard knowledge document searchable as soon as core processing succeeds, so that AI generation failures do not block learning material.
26. As a knowledge-base user, I want successful unreviewed knowledge marked as available and unreviewed, so that I can use it without mistaking it for human-verified content.
27. As a knowledge-base user, I want ordinary quality concerns shown as warnings, so that I can correct them when they matter.
28. As a knowledge-base user, I want unusable extraction results blocked, so that empty, encrypted, damaged, or overwhelmingly garbled content does not enter the library.
29. As a knowledge-base user, I want short or unusual content warned rather than rejected, so that valid edge cases remain usable.
30. As a knowledge-base user, I want automatic classification to run independently, so that classifier downtime does not block search.
31. As a knowledge-base user, I want unclassified knowledge searchable, so that it remains useful before tagging finishes.
32. As a knowledge-base user, I want unclassified knowledge excluded from tag-dependent packs, so that packs do not include material without a justified match.
33. As a knowledge-base user, I want automatic tags to retain confidence, source, and evidence, so that I can judge why a tag was proposed.
34. As a knowledge-base user, I want my tag corrections preserved, so that future automatic classification cannot undo my feedback.
35. As a knowledge-base user, I want Structure, SOP, and Insight generated independently, so that one failed artifact does not block the others.
36. As a knowledge-base user, I want each derived artifact retried independently, so that regenerating Insight does not regenerate the source or SOP.
37. As a knowledge-base user, I want a pure-theory source to mark SOP as not applicable, so that absence is not misreported as failure.
38. As a pack creator, I want missing required artifacts reported before export, so that exports never silently omit expected content.
39. As a pack creator, I want exported packs to remain immutable snapshots, so that later source updates or deletion do not alter delivered files.
40. As a knowledge-base user, I want deleted knowledge moved to a recycle bin for thirty days, so that accidental deletion is recoverable.
41. As a knowledge-base user, I want all managed versions, chunks, tags, and artifacts isolated together in the recycle bin, so that deleted knowledge cannot leak into search or packs.
42. As a knowledge-base user, I want to restore a deleted knowledge source with its managed relationships, so that recovery is complete.
43. As a knowledge-base user, I want permanent cleanup limited to system-managed content, so that unrelated personal files are never removed.
44. As a knowledge-base user, I want existing data backed up before migration, so that the current workstation can be restored.
45. As a knowledge-base user, I want migration rehearsed against a database copy, so that failures are found before changing live data.
46. As a knowledge-base user, I want existing usable records mapped to first versions without reprocessing, so that migration does not consume time or AI cost.
47. As a knowledge-base user, I want uncertain legacy relationships listed in a migration report, so that the system does not invent incorrect links.
48. As a knowledge-base user, I want legacy packs, prompt versions, feedback, and export history preserved, so that prior work survives the upgrade.
49. As a knowledge-base user, I want stale legacy jobs archived rather than restarted, so that installation does not unexpectedly process old files.
50. As a maintainer, I want migration counts reconciled before activation, so that missing records are detected before live replacement.
51. As a maintainer, I want a failed migration to restore the backup automatically, so that the application remains usable.
52. As a maintainer, I want task and stage errors represented consistently, so that the UI does not expose raw framework exceptions.
53. As a maintainer, I want the FastAPI routes and tests to use the same ingestion interface, so that tested behavior is production behavior.
54. As a maintainer, I want deterministic extraction and enhancement adapters in tests, so that retry and recovery scenarios are reproducible.
55. As a maintainer, I want task history retained separately from active queue state, so that past failures can be diagnosed without confusing current work.

## Implementation Decisions

- Build one deep资料导入 module behind a narrow interface used by both FastAPI and tests.
- Keep the task queue persistent and automatically recover work after process restart.
- Process one active import task at a time in the first release; retain room for later configurable concurrency without exposing it now.
- Represent imports as durable tasks composed of ordered processing stages with explicit completion state.
- Model core stages as content fingerprinting, text extraction, faithful cleaning and standard-document creation, quality validation, chunk indexing, and current-version promotion.
- Treat automatic classification, tagging, Structure, SOP, and Insight as independent knowledge enhancement work after source availability.
- Make each stage idempotent and commit its result atomically. Incomplete temporary results are never visible.
- Classify failures as transient or deterministic. Persist retry count and next attempt time across restart.
- Retry transient failures at most three times with increasing delay. Deterministic failures enter needs attention immediately.
- Support paused state. Active work observes pause requests at stage completion; queued work pauses immediately.
- Do not add cancel semantics to the first release. Data deletion remains a separate workflow.
- Identify exact content with a stable content fingerprint rather than filename.
- Introduce knowledge sources and immutable source versions. Promote a version only after core processing succeeds.
- Preserve the previous current version until promotion succeeds.
- Store review state separately from processing state. Successful automatic processing produces available-unreviewed knowledge.
- Implement explainable blocking quality checks for unreadable, unsupported, encrypted, empty, overwhelmingly invalid, conversion-error, write-verification, and chunk-integrity cases.
- Treat short content, low classification confidence, missing optional artifacts, transcription concerns, and questionable claims as non-blocking warnings.
- Preserve automatic tag confidence, source, and evidence. Never overwrite human-owned tags during reclassification.
- Represent derived artifact outcomes independently, including completed, needs attention, paused where applicable, and not applicable.
- Make pack assembly report missing required artifacts before export. Existing exports remain snapshots.
- Add recycle-bin state and deletion timestamp to managed knowledge. Exclude recycled knowledge from every normal read path.
- Permanently purge only system-managed files after thirty days; do not purge exported pack snapshots.
- Introduce ordered schema migrations with a recorded schema version.
- Rehearse migration on a copied database and managed-data view, reconcile counts, produce an ambiguity report, and replace live data only after validation.
- Backfill each existing usable source as a first version without regenerating content.
- Preserve legacy tags, chunks, artifacts, prompts, settings, feedback, packs, and export history where relationships are reliable.
- Archive stale legacy jobs without executing them.
- Normalize domain errors before they reach Web rendering. Raw framework validation or Python exception payloads are not user-facing error messages.
- Keep the Web interface focused on upload, queue visibility, pause, continue, actionable failures, source version state, and independent enhancement state.
- Respect ADR-0001 through ADR-0007.

## Testing Decisions

- Use the资料导入 module interface as the primary test seam. Tests and FastAPI cross the same seam.
- Test external behavior and durable state transitions, not internal helper calls or SQL statement shape.
- Use temporary SQLite and filesystem adapters for integration tests.
- Use controllable extraction and knowledge-enhancement adapters to produce success, transient failure, deterministic failure, malformed output, and delayed completion.
- Add state-transition tests for queueing, serial execution, pause, continue, retry exhaustion, needs attention, and restart recovery.
- Add idempotency tests proving repeated stage execution does not duplicate formal results.
- Add atomicity tests proving interrupted stages expose no partial standard document, chunk set, tag set, or artifact.
- Add version tests for exact duplicates, changed content, promotion after success, and preservation after failed updates.
- Add quality tests that separate blocking conditions from warnings.
- Add knowledge-enhancement tests proving classification and artifact failures do not change current-version availability.
- Add ownership tests proving automatic tags cannot overwrite human tags.
- Add recycle-bin tests across search, statistics, library lists, pack selection, restore, and permanent purge.
- Add migration tests using a representative legacy database fixture, including success, ambiguous links, count mismatch, interrupted migration, and rollback.
- Add pack tests proving missing required artifacts are reported and exported snapshots remain unchanged after source deletion.
- Add a small number of HTTP smoke tests for upload, queue rendering, pause, continue, and actionable error display.
- The codebase has no existing automated test suite; this feature establishes the first durable test harness and fixtures.

## Out of Scope

- More than one concurrently active import task in the first release.
- User-facing worker-count or scheduling configuration.
- Hard cancellation of an active converter or AI request.
- Immediate permanent deletion from normal library views.
- Automatic deletion or mutation of exported ability-pack snapshots.
- Reprocessing all legacy knowledge during migration.
- Guessing ambiguous relationships between legacy records.
- Replacing SQLite with another database.
- Full RAG retrieval, vector embeddings, or a chat interface.
- OCR and audio/video transcription engines beyond preserving future adapter seams.
- A numeric quality score or an unvalidated universal quality threshold.
- Mandatory pre-use human approval.
- Redesigning the complete classification taxonomy or ability-pack user experience beyond required compatibility.

## Further Notes

- Domain vocabulary is defined in the project glossary and must be used in UI, tests, specs, and task names.
- The current implementation uses an in-process background mechanism, performs database and filesystem writes across multiple commits, and has no automated tests. The implementation plan must establish the durable seam before migrating route behavior.
- The live database is valuable user data. Migration and rollback are first-class acceptance work, not deployment cleanup.
- This spec deepens the资料导入 module; it does not authorize a cosmetic split of the existing large module into smaller shallow modules.
