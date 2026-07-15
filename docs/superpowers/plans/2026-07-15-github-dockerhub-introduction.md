# GitHub 与 Docker Hub 中文介绍 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 GitHub 和 Docker Hub 的中文产品介绍，让用户在首屏理解项目用途、完整流程和安全边界。

**Architecture:** GitHub README 保留完整文档，只重写首屏定位和核心保障；Docker Hub 使用独立精简 Overview；两个平台的短描述保持一致。平台元信息通过 `gh` 和 Docker Hub API 更新，仓库中不保存认证信息。

**Tech Stack:** Markdown、GitHub CLI、Docker Hub HTTP API、Python 标准库、unittest

---

### Task 1: 重写 GitHub README 首屏

**Files:**
- Modify: `README.md:1`
- Test: `tests/test_docs_v02.py`

- [ ] **Step 1: 写入失败的文档断言**

在 `tests/test_docs_v02.py` 增加断言，要求 README 包含以下流程和保障：

```python
def test_readme_leads_with_current_product_workflow(self):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "115 分享链接 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源" in readme
    assert "共享别名保护" in readme
    assert "只入库自有分享 STRM" in readme
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python3 -m unittest tests.test_docs_v02.V02DocsTests.test_readme_leads_with_current_product_workflow`

Expected: FAIL，因为 README 尚无统一流程行或新保障标题。

- [ ] **Step 3: 重写 README 开头**

将 README 首段改成：

```markdown
`cms-tg-ingest` 是 Cloud Media Sync（CMS）的 115 分享自动入库外挂。把一个或多个 115 分享链接发给 Telegram 机器人，程序会让 CMS 完成整理和分类，再创建你自己的永久分享、生成分享 STRM、移动到媒体库、刷新并确认 Emby 入库，最后清理 115 转存源。

`115 分享链接 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源`
```

随后用四个短条目说明：CMS 优先分类、只入库自有分享 STRM、共享别名保护、低频 115 调用。保留后续完整配置文档。

- [ ] **Step 4: 运行文档与敏感信息测试**

Run: `python3 -m unittest tests.test_docs_v02 tests.test_secret_hygiene`

Expected: PASS。

- [ ] **Step 5: 提交 README 修改**

```bash
git add README.md tests/test_docs_v02.py
git commit -m "docs: refresh Chinese product introduction"
```

### Task 2: 更新 GitHub 仓库元信息

**Files:**
- No repository files modified.

- [ ] **Step 1: 更新 description 和 homepage**

Run:

```bash
gh repo edit icekale/cms-tg-ingest \
  --description "CMS 115 分享自动入库外挂：TG 裸链接、自有分享 STRM、Emby 确认与安全清理" \
  --homepage "https://hub.docker.com/r/icekale/cms-tg-ingest"
```

Expected: command exits 0。

- [ ] **Step 2: 验证 GitHub 元信息**

Run:

```bash
gh repo view icekale/cms-tg-ingest --json description,homepageUrl,url
```

Expected: description 与统一定位完全一致，homepageUrl 指向 Docker Hub。

### Task 3: 更新 Docker Hub 中文 Overview

**Files:**
- Create: `docs/dockerhub-overview.md`

- [ ] **Step 1: 创建精简 Docker Hub Overview**

Overview 必须包含：产品用途、六步核心流程、四项安全保障、`docker pull`/Compose 快速开始、依赖项、GitHub 文档链接。短描述使用：

```text
CMS 115 分享自动入库外挂：TG 裸链接、自有分享 STRM、Emby 确认与安全清理
```

- [ ] **Step 2: 通过 Docker Hub API 更新介绍**

从本机 Docker credential helper 获取已有登录凭据，只在进程内换取短期 JWT；请求体从 `docs/dockerhub-overview.md` 读取，不打印密码或 JWT。调用：

```text
PATCH https://hub.docker.com/v2/repositories/icekale/cms-tg-ingest/
```

请求字段为 `description` 和 `full_description`。

- [ ] **Step 3: 验证 Docker Hub 页面数据**

Run:

```bash
curl -fsSL https://hub.docker.com/v2/repositories/icekale/cms-tg-ingest/
```

Expected: short description 与统一定位一致，full description 包含“共享别名保护”和 GitHub 仓库链接。

- [ ] **Step 4: 提交并推送**

```bash
git add docs/dockerhub-overview.md
git commit -m "docs: add Chinese Docker Hub overview"
git push origin main
```

- [ ] **Step 5: 最终验证**

Run:

```bash
python3 -m unittest discover -s tests
git status --short
gh repo view icekale/cms-tg-ingest --json description,homepageUrl
```

Expected: 全部测试通过、工作区干净、GitHub 与 Docker Hub 介绍均为新版中文内容。
