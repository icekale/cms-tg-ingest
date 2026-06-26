# Web UI Productization Design

## Goal

Refactor the `cms-tg-ingest` Web UI into a simple, product-grade operations console. The UI should help a personal media-library operator quickly answer three questions:

1. Is the ingest system healthy right now?
2. Which tasks need attention?
3. What safe action can I take next?

The redesign must not turn the app into a complex admin console. It must keep the current server-rendered Python approach and avoid adding frontend frameworks, auto-refresh loops, new external calls, or extra 115/CMS pressure.

## Product Direction

Use the recommended **single-page product console** direction:

- Homepage is the calm cockpit: status summary, key counts, and attention list.
- Task detail, quality, and health pages stay available as secondary pages.
- Raw diagnostics remain accessible but no longer dominate the first screen.
- The visual tone is clean, trustworthy, restrained, and low-noise.

This follows `PRODUCT.md`: default minimal, anomaly-first, progressive disclosure, cautious actions, and lightweight deployment.

## Scope

### In Scope

- Redesign the server-rendered HTML/CSS in `app/web.py`.
- Improve homepage hierarchy and copy.
- Add a reusable visual vocabulary for layout, cards, status badges, tables, action buttons, and diagnostic blocks.
- Make `/task/<id>`, `/quality`, and `/health` visually consistent with the homepage.
- Keep existing routes and POST behavior intact.
- Update Web UI tests to assert product-facing structure and important copy.
- Preserve current security token handling.

### Out of Scope

- No React/Vue/Svelte or build step.
- No new database schema for UI only.
- No high-frequency polling or live auto-refresh.
- No new 115/CMS/Emby scans from page render.
- No major backend workflow changes.
- No complex settings editor or chart dashboard.

## Information Architecture

### Homepage: Operations Console

Homepage should show:

1. **Header**
   - Product name: `cms-tg-ingest`
   - Short subtitle: Telegram 115 入库外挂 / 自分享 STRM 工作流
   - Status badge derived from local TaskStore state:
     - Healthy when no failed/needs-action tasks exist.
     - Attention when failed or needs-action tasks exist.
     - Busy when running/pending tasks exist but no problem tasks exist.

2. **Metric Cards**
   - 处理中 / 待执行
   - 需处理 / 失败
   - 等待资源
   - 已完成历史

   These are computed from `store.list_recent_tasks(limit=100)` only. Completed tasks stay folded by default.

3. **Need Attention Section**
   - First priority: `NEEDS_ACTION` and `FAILED` tasks.
   - Second priority: active tasks that are waiting on a lock or deferred stage.
   - Display compact task rows/cards with title, stage, status badge, short error/wait reason, and detail link.
   - Empty state: “暂无需要处理的任务”.

4. **Current Queue Section**
   - Show active non-succeeded tasks in a clean table/card list.
   - Keep completed tasks hidden from default view.
   - Preserve the existing summary text that completed history can be cleared locally.

5. **Utility Actions**
   - `TaskStore 本地轻量巡检`
   - `TaskStore 本地健康`
   - `清除历史记录`

   Actions should be visually secondary except destructive/history cleanup, which must keep a confirmation.

### Task Detail Page

The task detail page should become a focused incident/detail view:

- Header with task ID, display title, status badge, and current stage.
- Summary card with media library, destination path, lock/wait reason, retry recommendation, and short error.
- Action bar containing only valid actions:
  - retry current stage when allowed by `decide_retry`
  - check Emby
  - restore STRM
  - reprocess from scratch
- Timeline section rendered as a readable vertical list, not raw text dump.
- Back link to homepage.

### Quality Page

`/quality` should remain local and low-risk:

- Explain clearly: “只读取本地 TaskStore 和 STRM 文件路径，不扫描 115。”
- Render the existing report in a readable diagnostic panel.
- Keep “修复全部巡检问题” behind the existing confirmation.
- Do not add automatic periodic scans.

### Health Page

`/health` should become a readable local health report:

- Explain that it reflects local TaskStore queue health.
- Render the existing `format_taskstore_health` report in a diagnostic panel.
- Link back to homepage.
- Do not add extra network checks from page render.

## Visual System

Rewrite level: **medium restructure**.

Main visual failure today: the UI is technically usable but looks like a debug page. Everything has equal weight, raw text is exposed early, and action affordances are generic.

Intended direction:

- Near-neutral light background.
- White cards with subtle borders, not heavy shadows.
- Clear type hierarchy using system fonts.
- Status badges with restrained semantic colors.
- Buttons share one geometry and are only strong when primary.
- Diagnostic blocks use monospace only where content is genuinely diagnostic.
- Mobile layout stacks cards and task rows cleanly.

Approximate component vocabulary:

- `.shell`: centered max-width page container with responsive padding.
- `.topbar`: product heading, subtitle, and status badge.
- `.stats-grid`: responsive metric cards.
- `.panel`: primary content grouping.
- `.task-row`: compact task summary with title, metadata, and detail link.
- `.badge`: status/stage indicators.
- `.button`, `.button-secondary`, `.button-danger`: consistent action controls.
- `.diagnostic`: readable `<pre>` container for health/quality reports.

## Data Flow

All rendered data comes from existing local stores and helpers:

- `store.list_recent_tasks(limit=100)` for homepage counts and task lists.
- `task_display_title(task)` for user-facing task names.
- `_task_lock_label(task)` for lock/wait display.
- `stage_display_name(task.current_stage)` for stages.
- `decide_retry(task)` for retry visibility and reason.
- `scan_task_quality(store)` and `format_task_quality_report(...)` for `/quality`.
- `format_taskstore_health(store, enabled=True)` for `/health`.

No page render should call 115, CMS, Telegram, OpenAI, or Emby directly.

## Error Handling and Safety

- Missing task remains a simple “任务不存在” page with product styling.
- Malformed task IDs continue to return 404 text responses.
- `WEB_TOKEN` behavior remains unchanged.
- POST routes preserve current redirects and side effects.
- Cleanup, quality fix, and reprocess actions retain confirmation prompts where currently present or where risk is obvious.
- Long error/wait text should be escaped and visually constrained so it does not break layout.

## Testing Plan

Update and extend `tests/test_web_admin.py`:

- Homepage still folds completed history by default.
- Homepage includes the new product console text and metric labels.
- Failed/needs-action tasks appear in the attention section.
- Running/waiting tasks show lock or wait reasons.
- Task detail still shows timeline, retry form, Emby check, restore, and reprocess actions.
- Quality page still reports local-only behavior and fix action.
- Health page still includes TaskStore health summary and wait/lock detail text.
- Existing POST route behavior remains unchanged.

Run:

```sh
python3 -m unittest tests.test_web_admin -v
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

## Deployment Notes

After implementation passes tests, deploy using the existing Unraid-safe pattern:

1. Backup `/mnt/user/appdata/cms-tg-ingest`.
2. `rsync -az --delete` while excluding `.git/`, `.env`, `docker-compose.yml`, `data/`, `backups/`, caches, and worktrees.
3. Rebuild/recreate `cms-tg-ingest`.
4. Run `python /app/doctor.py --quiet` inside the container.
5. Verify `/`, `/quality`, and `/health` on the configured Web port.
