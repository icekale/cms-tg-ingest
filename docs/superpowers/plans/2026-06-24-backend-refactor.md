# Backend Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the backend so new self-share links are TaskRunner-owned, core logic is split out of `bridge.py`, and direct-link STRM files cannot be moved as final library output.

**Architecture:** Move code out of `bridge.py` in small compatibility-preserving slices: config, clients, media utilities, classification, then workflow. Keep `bridge.py` as a thin entry/compatibility module by re-exporting moved symbols during migration. Add tests around direct STRM rejection and TaskRunner ownership before changing behavior.

**Tech Stack:** Python 3 standard library, SQLite, `unittest`, Docker, existing CMS/115/Emby HTTP clients.

---

## File Structure

- `bridge.py`: remains executable entrypoint and compatibility facade; progressively imports and re-exports moved symbols.
- `app/config.py`: owns `Config`, `MoveConfig`, `MovePlan`, `SelfShareConfig`, parsing helpers, default library mappings, path helpers.
- `app/clients/http.py`: owns `HttpJson`, `FormHttp`, and cookie loading.
- `app/clients/cms.py`: owns `CmsClient`.
- `app/clients/p115.py`: owns `P115WebClient` and 115 item selection helpers.
- `app/clients/emby.py`: owns `EmbyClient`.
- `app/media/strm.py`: owns STRM file discovery, move planning, self-share validation, direct STRM cleanup, and merge behavior.
- `app/media/classify.py`: owns text normalization, TMDB/OpenAI category helpers, and final category selection.
- `app/workflows/self_share.py`: owns `SelfShareWorkflow`, `BridgeSelfShareTaskWorkflow`, and TaskRunner self-share stage behavior.
- `tests/test_refactor_imports.py`: new smoke tests proving moved modules import and `bridge.py` compatibility exports still work.
- Existing tests remain the primary regression suite.

---

### Task 1: Add Refactor Import Smoke Tests

**Files:**
- Create: `tests/test_refactor_imports.py`
- Modify: none
- Test: `tests/test_refactor_imports.py`

- [ ] **Step 1: Write the failing import tests**

Create `tests/test_refactor_imports.py` with:

```python
import unittest
from pathlib import Path


class RefactorImportTests(unittest.TestCase):
    def test_config_module_exports_core_config_types(self):
        from app.config import Config, MoveConfig, MovePlan, SelfShareConfig

        self.assertEqual(Config.__name__, "Config")
        self.assertEqual(MoveConfig.__name__, "MoveConfig")
        self.assertEqual(MovePlan.__name__, "MovePlan")
        self.assertEqual(SelfShareConfig.__name__, "SelfShareConfig")

    def test_client_modules_export_clients(self):
        from app.clients.cms import CmsClient
        from app.clients.emby import EmbyClient
        from app.clients.http import FormHttp, HttpJson
        from app.clients.p115 import P115WebClient

        self.assertEqual(CmsClient.__name__, "CmsClient")
        self.assertEqual(EmbyClient.__name__, "EmbyClient")
        self.assertEqual(FormHttp.__name__, "FormHttp")
        self.assertEqual(HttpJson.__name__, "HttpJson")
        self.assertEqual(P115WebClient.__name__, "P115WebClient")

    def test_media_modules_export_core_helpers(self):
        from app.media.classify import final_category_for_move, normalize_text
        from app.media.strm import MovePlan, has_strm_file, validate_self_share_strm_source

        self.assertEqual(normalize_text("J-杰克・莱恩-2018"), "j杰克莱恩2018")
        self.assertEqual(final_category_for_move({"category_choice": "外国电视"}, {}), "外国电视")
        self.assertEqual(MovePlan.__name__, "MovePlan")
        self.assertFalse(has_strm_file(Path("/path/that/does/not/exist")))
        self.assertEqual(validate_self_share_strm_source(Path("/path/that/does/not/exist"), {}), "")

    def test_workflow_module_exports_self_share_workflows(self):
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow, SelfShareWorkflow

        self.assertEqual(SelfShareWorkflow.__name__, "SelfShareWorkflow")
        self.assertEqual(BridgeSelfShareTaskWorkflow.__name__, "BridgeSelfShareTaskWorkflow")

    def test_bridge_keeps_compatibility_exports(self):
        import bridge
        from app.clients.p115 import P115WebClient
        from app.config import Config, MoveConfig, SelfShareConfig
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        self.assertIs(bridge.Config, Config)
        self.assertIs(bridge.MoveConfig, MoveConfig)
        self.assertIs(bridge.SelfShareConfig, SelfShareConfig)
        self.assertIs(bridge.P115WebClient, P115WebClient)
        self.assertIs(bridge.BridgeSelfShareTaskWorkflow, BridgeSelfShareTaskWorkflow)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_refactor_imports -v
```

Expected: FAIL or ERROR because `app.config`, `app.clients.*`, `app.media.*`, and `app.workflows.self_share` do not exist yet.

- [ ] **Step 3: Commit the failing tests**

Run:

```bash
git add tests/test_refactor_imports.py
git commit -m "test: add backend refactor import smoke tests"
```

Expected: commit succeeds. This commit intentionally contains failing tests that the following tasks make pass.

---

### Task 2: Extract Configuration and Path Helpers

**Files:**
- Create: `app/config.py`
- Modify: `bridge.py`
- Test: `tests/test_refactor_imports.py`, `tests/test_bridge_v02_integration.py`, `tests/test_self_share_workflow.py`

- [ ] **Step 1: Move config-related definitions into `app/config.py`**

Create `app/config.py` by moving these definitions from `bridge.py` without behavior changes:

```python
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def parse_bool_env(value: str | None, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "enable"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def split_env_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|,]", value or "") if part.strip()]


def default_library_roots() -> dict[str, Path]:
    base = Path("/mnt/user/Unraid/strm/转存")
    return {
        "华语电影": base / "Movie/电影/华语电影",
        "欧美电影": base / "Movie/电影/欧美电影",
        "亚洲电影": base / "Movie/电影/亚洲电影",
        "动漫电影": base / "Movie/电影/动漫电影",
        "国产电视": base / "TVCN",
        "外国电视": base / "TV",
        "番剧": base / "Dongman",
    }


def parse_library_map(value: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        key, path = item.split("=", 1)
        key = key.strip()
        path = path.strip()
        if key and path:
            result[key] = Path(path).expanduser()
    return result


def parse_parent_cid_category_map(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        parent_id, category = item.split("=", 1)
        parent_id = parent_id.strip()
        category = category.strip()
        if parent_id and category:
            result[parent_id] = category
    return result


def safe_resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    path = safe_resolve(path)
    root = safe_resolve(root)
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_under_any_root(path: Path, roots: list[Path]) -> bool:
    return any(is_relative_to(path, root) for root in roots)


@dataclass
class Config:
    tg_bot_token: str
    tg_allowed_chat_id: str
    cms_base_url: str
    cms_username: str
    cms_password: str
    poll_timeout: int = 30
    http_timeout: int = 60
    db_path: str = "/data/submissions.db"
    status_poll_seconds: int = 300
    status_poll_interval: int = 20
    emby_base_url: str = ""
    emby_api_key: str = ""
    emby_user_id: str = ""
    strm_source_roots: str = "/mnt/user/Unraid/strm/转存"
    strm_library_map: str = ""
    move_conflict_policy: str = "skip"
    strm_stable_seconds: int = 30
    openai_classify_enabled: bool = False
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_high_confidence: float = 0.75
    openai_suggest_confidence: float = 0.45
    tmdb_api_key: str = ""
    tmdb_bearer_token: str = ""
    workflow_mode: str = "direct"
    p115_cookie_path: str = "/config/115-cookies.txt"
    p115_min_request_interval_seconds: float = 2.0
    self_share_receive_cid: str = ""
    self_share_strm_root: str = "/mnt/user/Unraid/strm/share"
    self_share_cms_local_path: str = "/media/share"
    self_share_cms_cid: str = "0"
    self_share_cleanup_after_emby: bool = False
    self_share_source_cleanup_parent_ids: str = ""
    self_share_auto_organize_retry_seconds: int = 15
    status_repair_enabled: bool = True
    status_repair_interval_seconds: int = 300
    status_repair_limit: int = 50
    cms_parent_cid_category_map: str = ""
    self_share_organized_scan_parent_ids: str = ""
    task_db_path: str = "/data/tasks.db"
    task_engine_enabled: bool = False
    web_enabled: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_token: str = ""
    task_worker_interval_seconds: int = 5
    task_max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        required = ["TG_BOT_TOKEN", "TG_ALLOWED_CHAT_ID", "CMS_BASE_URL", "CMS_USERNAME", "CMS_PASSWORD"]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        return cls(
            tg_bot_token=os.environ["TG_BOT_TOKEN"],
            tg_allowed_chat_id=os.environ["TG_ALLOWED_CHAT_ID"],
            cms_base_url=os.environ["CMS_BASE_URL"].rstrip("/"),
            cms_username=os.environ["CMS_USERNAME"],
            cms_password=os.environ["CMS_PASSWORD"],
            poll_timeout=int(os.environ.get("TG_POLL_TIMEOUT", "30")),
            http_timeout=int(os.environ.get("HTTP_TIMEOUT", "60")),
            db_path=os.environ.get("DB_PATH", "/data/submissions.db"),
            status_poll_seconds=int(os.environ.get("STATUS_POLL_SECONDS", "300")),
            status_poll_interval=int(os.environ.get("STATUS_POLL_INTERVAL", "20")),
            emby_base_url=(os.environ.get("EMBY_BASE_URL") or os.environ.get("EMBY_HOST_PORT") or "").rstrip("/"),
            emby_api_key=os.environ.get("EMBY_API_KEY", ""),
            emby_user_id=os.environ.get("EMBY_USER_ID", ""),
            strm_source_roots=os.environ.get("STRM_SOURCE_ROOTS", "/mnt/user/Unraid/strm/转存"),
            strm_library_map=os.environ.get("STRM_LIBRARY_MAP", ""),
            move_conflict_policy=os.environ.get("MOVE_CONFLICT_POLICY", "skip"),
            strm_stable_seconds=int(os.environ.get("STRM_STABLE_SECONDS", "30")),
            openai_classify_enabled=parse_bool_env(os.environ.get("OPENAI_CLASSIFY_ENABLED"), False),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            openai_high_confidence=env_float("OPENAI_HIGH_CONFIDENCE", 0.75),
            openai_suggest_confidence=env_float("OPENAI_SUGGEST_CONFIDENCE", 0.45),
            tmdb_api_key=os.environ.get("TMDB_API_KEY", ""),
            tmdb_bearer_token=os.environ.get("TMDB_BEARER_TOKEN", ""),
            workflow_mode=os.environ.get("WORKFLOW_MODE", "direct").strip().lower() or "direct",
            p115_cookie_path=os.environ.get("P115_COOKIE_PATH", "/config/115-cookies.txt"),
            p115_min_request_interval_seconds=env_float("P115_MIN_REQUEST_INTERVAL_SECONDS", 2.0),
            self_share_receive_cid=os.environ.get("SELF_SHARE_RECEIVE_CID", ""),
            self_share_strm_root=os.environ.get("SELF_SHARE_STRM_ROOT", "/mnt/user/Unraid/strm/share"),
            self_share_cms_local_path=os.environ.get("SELF_SHARE_CMS_LOCAL_PATH", "/media/share"),
            self_share_cms_cid=os.environ.get("SELF_SHARE_CMS_CID", "0"),
            self_share_cleanup_after_emby=parse_bool_env(os.environ.get("SELF_SHARE_CLEANUP_AFTER_EMBY"), False),
            self_share_source_cleanup_parent_ids=os.environ.get("SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS", ""),
            self_share_auto_organize_retry_seconds=int(os.environ.get("SELF_SHARE_AUTO_ORGANIZE_RETRY_SECONDS", "15")),
            status_repair_enabled=parse_bool_env(os.environ.get("STATUS_REPAIR_ENABLED"), True),
            status_repair_interval_seconds=int(os.environ.get("STATUS_REPAIR_INTERVAL_SECONDS", "300")),
            status_repair_limit=int(os.environ.get("STATUS_REPAIR_LIMIT", "50")),
            cms_parent_cid_category_map=os.environ.get("CMS_PARENT_CID_CATEGORY_MAP", ""),
            self_share_organized_scan_parent_ids=os.environ.get("SELF_SHARE_ORGANIZED_SCAN_PARENT_IDS", ""),
            task_db_path=os.environ.get("TASK_DB_PATH", "/data/tasks.db"),
            task_engine_enabled=parse_bool_env(os.environ.get("TASK_ENGINE_ENABLED"), False),
            web_enabled=parse_bool_env(os.environ.get("WEB_ENABLED"), False),
            web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("WEB_PORT", "8787")),
            web_token=os.environ.get("WEB_TOKEN", ""),
            task_worker_interval_seconds=int(os.environ.get("TASK_WORKER_INTERVAL_SECONDS", "5")),
            task_max_retries=int(os.environ.get("TASK_MAX_RETRIES", "3")),
        )


@dataclass
class MoveConfig:
    source_roots: list[Path]
    library_roots: dict[str, Path]
    conflict_policy: str = "skip"
    stable_seconds: int = 0

    @classmethod
    def from_config(cls, config: Config) -> "MoveConfig":
        source_roots = [Path(part).expanduser() for part in split_env_list(config.strm_source_roots)]
        library_roots = default_library_roots()
        if config.strm_library_map.strip():
            library_roots.update(parse_library_map(config.strm_library_map))
        return cls(
            source_roots=source_roots,
            library_roots=library_roots,
            conflict_policy=config.move_conflict_policy or "skip",
            stable_seconds=max(0, int(config.strm_stable_seconds)),
        )


@dataclass
class MovePlan:
    status: str
    reason: str
    source_path: Path | None = None
    dest_path: Path | None = None
    category: str | None = None


@dataclass
class SelfShareConfig:
    enabled: bool = False
    strm_root: Path = Path("/mnt/user/Unraid/strm/share")
    cms_local_path: str = "/media/share"
    cms_cid: str = "0"
    excluded_parent_ids: set[str] | None = None
    cleanup_after_emby: bool = False
    source_cleanup_parent_ids: set[str] | None = None
    auto_organize_retry_seconds: int = 90
    parent_cid_category_map: dict[str, str] | None = None
    organized_scan_parent_ids: set[str] | None = None

    @classmethod
    def from_config(cls, config: Config, cms: Any | None = None) -> "SelfShareConfig":
        excluded = set()
        organized_scan_parent_ids = set(split_env_list(config.self_share_organized_scan_parent_ids))
        if cms:
            try:
                excluded.update(cms.auto_organize_excluded_parent_ids())
            except Exception:
                pass
            if not organized_scan_parent_ids:
                try:
                    organized_scan_parent_ids.update(cms.auto_organize_existing_parent_ids())
                except Exception:
                    pass
        return cls(
            enabled=config.workflow_mode == "self_share_sync",
            strm_root=Path(config.self_share_strm_root).expanduser(),
            cms_local_path=config.self_share_cms_local_path,
            cms_cid=config.self_share_cms_cid,
            excluded_parent_ids=excluded,
            cleanup_after_emby=config.self_share_cleanup_after_emby,
            source_cleanup_parent_ids=set(split_env_list(config.self_share_source_cleanup_parent_ids)),
            auto_organize_retry_seconds=max(0, int(config.self_share_auto_organize_retry_seconds)),
            parent_cid_category_map=parse_parent_cid_category_map(config.cms_parent_cid_category_map),
            organized_scan_parent_ids=organized_scan_parent_ids,
        )
```

- [ ] **Step 2: Update `bridge.py` to import config symbols**

At the top of `bridge.py`, after standard imports, add:

```python
from app.config import (
    Config,
    MoveConfig,
    MovePlan,
    SelfShareConfig,
    default_library_roots,
    env_float,
    is_relative_to,
    is_under_any_root,
    parse_bool_env,
    parse_library_map,
    parse_parent_cid_category_map,
    safe_resolve,
    split_env_list,
)
```

Then delete the moved duplicate definitions from `bridge.py` once imports are in place.

- [ ] **Step 3: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_bridge_v02_integration tests.test_self_share_workflow -v
```

Expected: `test_config_module_exports_core_config_types` and bridge compatibility assertions pass. Other tests should remain green.

- [ ] **Step 4: Run full tests**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/config.py bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract backend config types"
```

---

### Task 3: Extract HTTP Helpers and Service Clients

**Files:**
- Create: `app/clients/__init__.py`
- Create: `app/clients/http.py`
- Create: `app/clients/cms.py`
- Create: `app/clients/p115.py`
- Create: `app/clients/emby.py`
- Modify: `bridge.py`
- Test: `tests/test_refactor_imports.py`, `tests/test_self_share_workflow.py`, `tests/test_quality_checks.py`

- [ ] **Step 1: Create client package**

Create `app/clients/__init__.py`:

```python
"""External service clients for CMS, 115, and Emby."""
```

- [ ] **Step 2: Extract HTTP helpers**

Create `app/clients/http.py` by moving `HttpJson`, `FormHttp`, and `load_cookie_value` from `bridge.py`. Keep method signatures unchanged:

```python
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class HttpJson:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def request(self, url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Accept": "application/json"}
        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


class FormHttp:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def request(
        self,
        url: str,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
        req_headers = {"Accept": "application/json, text/plain, */*"}
        if data is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body_text[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


def load_cookie_value(value_or_path: str) -> str:
    value = str(value_or_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value
```

- [ ] **Step 3: Extract CMS client**

Create `app/clients/cms.py` by moving `CmsClient` from `bridge.py`. Imports must be:

```python
from __future__ import annotations

import urllib.parse

from app.config import Config
from app.clients.http import HttpJson


def iter_items(data):
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "data", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    return []
```

Then paste the existing `CmsClient` class unchanged under those imports.

- [ ] **Step 4: Extract 115 client and helpers**

Create `app/clients/p115.py` by moving these definitions from `bridge.py`:

```python
as_float
media_type_for_category
extract_primary_chinese_title
candidate_tokens
p115_file_id
p115_parent_id
p115_residue_file_id
p115_residue_parent_id
category_for_115_parent_id
p115_file_name
p115_is_folder
infer_category_from_115_path
infer_category_from_115_item
select_organized_115_folder
select_recent_tmdb_115_folder
select_source_residue_115_files
P115WebClient
```

Use these imports at the top:

```python
from __future__ import annotations

import re
import time
from typing import Any

from app.clients.http import FormHttp, load_cookie_value
from app.config import default_library_roots
```

Also move or import `normalize_text`, `extract_tmdb_id_from_name`, and `extract_year_from_name`. During this task, prefer importing them from `bridge` only if avoiding a circular import is verified. If a circular import appears, defer the `P115WebClient` extraction to Task 5 after classification helpers are moved.

- [ ] **Step 5: Extract Emby client**

Create `app/clients/emby.py` by moving `EmbyClient` from `bridge.py`. Imports must include:

```python
from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from app.clients.http import HttpJson
from app.config import is_relative_to, safe_resolve
```

Also move or import `item_tmdb_id` after Task 5. If `item_tmdb_id` is not yet moved, import it from `bridge` only if no circular import occurs; otherwise leave `EmbyClient` in `bridge.py` until classification extraction.

- [ ] **Step 6: Update `bridge.py` compatibility imports**

Replace moved class/function definitions in `bridge.py` with imports:

```python
from app.clients.cms import CmsClient
from app.clients.emby import EmbyClient
from app.clients.http import FormHttp, HttpJson, load_cookie_value
from app.clients.p115 import P115WebClient
```

If helper functions are still used by existing bridge code, import them explicitly from `app.clients.p115`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_self_share_workflow tests.test_quality_checks -v
```

Expected: client import tests pass; existing client behavior tests remain green.

- [ ] **Step 8: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add app/clients bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract service clients"
```

---

### Task 4: Extract STRM File Operations and Safety Gate

**Files:**
- Create: `app/media/__init__.py`
- Create: `app/media/strm.py`
- Modify: `bridge.py`
- Modify: `tests/test_self_share_workflow.py`
- Test: `tests/test_self_share_workflow.py`, `tests/test_refactor_imports.py`

- [ ] **Step 1: Add explicit regression test for direct STRM rejection**

Append this test to `tests/test_self_share_workflow.py` near the existing `merge_self_share_strm_folder` tests:

```python
    def test_merge_self_share_strm_folder_rejects_direct_strm_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "J-杰克・莱恩-2018-[tmdb=73375]"
            dest = root / "library" / "J-杰克・莱恩-2018-[tmdb=73375]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "episode.strm").write_text(
                "http://cms/d/file.mkv?/杰克・莱恩.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1212"),
                "https://115cdn.com/s/abc?password=1212",
                "submitted",
                title="杰克・莱恩 (2018) {tmdb-73375}",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
                own_share_receive_code="1212",
                own_share_file_name=source.name,
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("发现直链 STRM", updated["move_error"])
            self.assertTrue((source / "episode.strm").exists())
```

- [ ] **Step 2: Run the regression test before extraction**

Run:

```bash
python3 -m unittest tests.test_self_share_workflow.SelfShareWorkflowTests.test_merge_self_share_strm_folder_rejects_direct_strm_source -v
```

Expected: PASS if current safety behavior already exists. If the test class name differs, run `python3 -m unittest tests.test_self_share_workflow -v` and confirm the new test passes.

- [ ] **Step 3: Create media package**

Create `app/media/__init__.py`:

```python
"""Media classification and STRM file operations."""
```

- [ ] **Step 4: Extract STRM operations**

Create `app/media/strm.py` by moving these definitions from `bridge.py`:

```python
has_strm_file
newest_mtime
is_directory_stable
destination_for_category
library_category_for_path
library_media_root_for_path
find_strm_source_dir
find_recent_library_strm_source_dir
category_from_existing_library_match
plan_strm_move
execute_strm_move
validate_self_share_strm_source
remove_direct_strm_files
category_from_existing_library_folder
cleanup_direct_strm_for_organized_folder
merge_self_share_strm_folder
find_self_share_strm_source_dir
select_move_source_for_workflow
move_config_for_workflow_source
prepare_self_share_move_inputs
repair_stranded_self_share_moves
restore_missing_self_share_library_folder
restore_missing_self_share_library_folders
cleanup_pending_self_share_sources
```

Use imports:

```python
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from app.config import MoveConfig, MovePlan, SelfShareConfig, is_relative_to, is_under_any_root, safe_resolve
```

If a moved function needs classification helpers still in `bridge.py`, import those helpers from `bridge` only temporarily and remove those imports in Task 5. Keep behavior unchanged.

- [ ] **Step 5: Update `bridge.py` compatibility imports**

Replace moved definitions in `bridge.py` with:

```python
from app.media.strm import (
    category_from_existing_library_folder,
    category_from_existing_library_match,
    cleanup_direct_strm_for_organized_folder,
    cleanup_pending_self_share_sources,
    destination_for_category,
    execute_strm_move,
    find_recent_library_strm_source_dir,
    find_self_share_strm_source_dir,
    find_strm_source_dir,
    has_strm_file,
    library_category_for_path,
    library_media_root_for_path,
    merge_self_share_strm_folder,
    move_config_for_workflow_source,
    newest_mtime,
    plan_strm_move,
    prepare_self_share_move_inputs,
    remove_direct_strm_files,
    repair_stranded_self_share_moves,
    restore_missing_self_share_library_folder,
    restore_missing_self_share_library_folders,
    select_move_source_for_workflow,
    validate_self_share_strm_source,
)
```

- [ ] **Step 6: Run focused STRM tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_self_share_workflow -v
```

Expected: all tests pass, including direct STRM rejection and existing merge behavior.

- [ ] **Step 7: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add app/media bridge.py tests/test_self_share_workflow.py tests/test_refactor_imports.py
git commit -m "refactor: extract strm safety operations"
```

---

### Task 5: Extract Classification and TMDB/OpenAI Helpers

**Files:**
- Create: `app/media/classify.py`
- Modify: `bridge.py`
- Modify: `app/clients/p115.py`
- Modify: `app/clients/emby.py`
- Modify: `app/media/strm.py`
- Test: `tests/test_openai_fallback.py`, `tests/test_quality_checks.py`, `tests/test_refactor_imports.py`

- [ ] **Step 1: Extract classification helpers**

Create `app/media/classify.py` by moving these definitions from `bridge.py`:

```python
normalize_text
clean_share_title
CHINESE_LANGUAGE_MARKERS
CHINESE_COUNTRY_MARKERS
ASIAN_MOVIE_COUNTRY_MARKERS
ASIAN_MOVIE_LANGUAGE_MARKERS
INDIAN_MOVIE_MARKERS
normalized_tmdb_language
language_matches
has_indian_movie_hint
user_movie_category_bucket
infer_region_category
extract_tmdb_id_from_name
extract_year_from_name
media_type_for_category
extract_primary_chinese_title
candidate_tokens
map_category_label
final_category_for_move
parse_recognition_json
expected_task_tmdb_id
item_tmdb_id
TmdbWebResolver
TmdbApiResolver
extract_tmdb_search_query
extract_tmdb_page_title
extract_tmdb_default_language
tmdb_match_score
apply_tmdb_hint_resolution
apply_tmdb_search_resolution
```

Use imports:

```python
from __future__ import annotations

import html as html_lib
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any

from app.clients.http import HttpJson
from app.config import default_library_roots

LOG = logging.getLogger("cms-tg-ingest")
```

- [ ] **Step 2: Update dependent modules to import classification helpers**

In `app/clients/p115.py`, replace temporary `bridge` imports with:

```python
from app.media.classify import candidate_tokens, extract_tmdb_id_from_name, extract_year_from_name, media_type_for_category, normalize_text
```

In `app/clients/emby.py`, import:

```python
from app.media.classify import item_tmdb_id
```

In `app/media/strm.py`, import:

```python
from app.media.classify import candidate_tokens, expected_task_tmdb_id, extract_tmdb_id_from_name, final_category_for_move, media_type_for_category, parse_recognition_json
```

- [ ] **Step 3: Update `bridge.py` compatibility imports**

Replace moved classification definitions in `bridge.py` with:

```python
from app.media.classify import (
    TmdbApiResolver,
    TmdbWebResolver,
    apply_tmdb_hint_resolution,
    apply_tmdb_search_resolution,
    candidate_tokens,
    clean_share_title,
    expected_task_tmdb_id,
    extract_primary_chinese_title,
    extract_tmdb_default_language,
    extract_tmdb_id_from_name,
    extract_tmdb_page_title,
    extract_tmdb_search_query,
    extract_year_from_name,
    final_category_for_move,
    infer_region_category,
    item_tmdb_id,
    map_category_label,
    media_type_for_category,
    normalize_text,
    parse_recognition_json,
    tmdb_match_score,
    user_movie_category_bucket,
)
```

- [ ] **Step 4: Run focused classification tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_openai_fallback tests.test_quality_checks -v
```

Expected: all classification, TMDB, OpenAI fallback, and quality tests pass.

- [ ] **Step 5: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add app/media/classify.py app/clients/p115.py app/clients/emby.py app/media/strm.py bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract media classification helpers"
```

---

### Task 6: Extract Self-Share Workflows

**Files:**
- Create: `app/workflows/__init__.py`
- Create: `app/workflows/self_share.py`
- Modify: `bridge.py`
- Test: `tests/test_bridge_task_engine.py`, `tests/test_self_share_workflow.py`, `tests/test_bridge_v02_integration.py`, `tests/test_refactor_imports.py`

- [ ] **Step 1: Create workflow package**

Create `app/workflows/__init__.py`:

```python
"""Task workflows used by TaskRunner."""
```

- [ ] **Step 2: Extract self-share workflow classes**

Create `app/workflows/self_share.py` by moving these definitions from `bridge.py`:

```python
SelfShareWorkflow
BridgeSelfShareTaskWorkflow
enrich_recognition_from_self_share_folder
resolve_self_share_recognition_before_prepare
cleanup_self_share_source_residue
cleanup_own_share_source
is_115_receive_restricted_error
should_attempt_strm_move
should_defer_for_probing
is_move_plan_retryable
```

Use imports from the new modules instead of `bridge.py`:

```python
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.models import TaskStage
from app.task_runner import StageResult
from app.config import MoveConfig, MovePlan, SelfShareConfig, safe_resolve, is_relative_to
from app.clients.cms import CmsClient
from app.clients.p115 import P115WebClient, category_for_115_parent_id
from app.media.classify import apply_tmdb_hint_resolution, apply_tmdb_search_resolution, expected_task_tmdb_id, extract_tmdb_id_from_name, final_category_for_move, media_type_for_category, parse_recognition_json
from app.media.strm import category_from_existing_library_folder, cleanup_direct_strm_for_organized_folder, find_self_share_strm_source_dir, merge_self_share_strm_folder, move_config_for_workflow_source, plan_strm_move

LOG = logging.getLogger("cms-tg-ingest")
```

If a helper still lives in `bridge.py`, either move it into this module with the workflow or import from a new small module. Avoid introducing `from bridge import ...` in workflow code.

- [ ] **Step 3: Update `bridge.py` compatibility imports**

Replace moved workflow definitions in `bridge.py` with:

```python
from app.workflows.self_share import (
    BridgeSelfShareTaskWorkflow,
    SelfShareWorkflow,
    cleanup_own_share_source,
    cleanup_self_share_source_residue,
    enrich_recognition_from_self_share_folder,
    is_115_receive_restricted_error,
    is_move_plan_retryable,
    resolve_self_share_recognition_before_prepare,
    should_attempt_strm_move,
    should_defer_for_probing,
)
```

- [ ] **Step 4: Run workflow tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_bridge_task_engine tests.test_self_share_workflow tests.test_bridge_v02_integration -v
```

Expected: workflow behavior remains unchanged.

- [ ] **Step 5: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add app/workflows bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract self-share workflows"
```

---

### Task 7: Enforce TaskRunner Ownership for New Self-Share Links

**Files:**
- Modify: `bridge.py`
- Modify: `tests/test_bridge_v02_integration.py`
- Modify: `tests/test_bridge_task_engine.py`
- Test: `tests/test_bridge_v02_integration.py`, `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Add regression test that TaskEngine does not start legacy polling**

In `tests/test_bridge_v02_integration.py`, add a test near existing `handle_update` TaskStore tests:

```python
    def test_task_engine_self_share_link_does_not_start_legacy_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()
            cms = object()
            update = {
                "message": {
                    "chat": {"id": 464100862},
                    "text": "https://115cdn.com/s/abc?password=1234",
                }
            }
            calls = []

            def fake_start_status_poll(*args, **kwargs):
                calls.append((args, kwargs))

            with patch.object(bridge, "start_status_poll", fake_start_status_poll):
                bridge.handle_update(
                    update,
                    cms,
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=True,
                    task_store=task_store,
                    task_engine_enabled=True,
                    workflow_mode="self_share_sync",
                )

            self.assertEqual(calls, [])
            tasks = task_store.recent_tasks(limit=5)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].share_code, "abc")
```

Adjust helper names only if the existing test file uses different fake class names. Keep assertion intent unchanged.

- [ ] **Step 2: Run the new test and verify current behavior**

Run:

```bash
python3 -m unittest tests.test_bridge_v02_integration -v
```

Expected: If this already passes, keep it as regression coverage. If it fails because legacy polling starts, proceed to Step 3.

- [ ] **Step 3: Route new self-share links only into TaskStore when TaskEngine is enabled**

In `bridge.py` `handle_update`, ensure the self-share + TaskEngine path returns immediately after TaskStore task creation. The relevant logic should follow this shape:

```python
if task_engine_enabled and workflow_mode == "self_share_sync" and task_store is not None:
    task = ensure_task_for_link(task_store, link, chat_id=str(chat_id), title="")
    telegram.send_message(chat_id, f"已创建任务 #{task.id}：{stage_display_name(task.current_stage)}")
    continue
```

Do not call `cms.add_share_down`, `start_status_poll`, or other legacy execution paths in this branch.

- [ ] **Step 4: Add regression test that final STRM source must be self-share root**

In `tests/test_bridge_task_engine.py`, add a test near `_stage_strm_ready` tests:

```python
    def test_strm_ready_rejects_direct_library_source_for_self_share_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            direct_root = root / "direct"
            share_root = root / "share"
            library_root = root / "library"
            direct_source = direct_root / "Movie"
            direct_source.mkdir(parents=True)
            (direct_source / "movie.strm").write_text("http://cms/d/file.mkv?/movie.mkv", encoding="utf-8")
            workflow = self.make_workflow(
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(source_roots=[direct_root], library_roots={"欧美电影": library_root}, stable_seconds=0),
            )
            row = self.create_submission(title="Movie (2026) {tmdb-123}")
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="Movie",
                own_share_code="ownshare",
                own_share_receive_code="1212",
            ) or row
            task = self.make_task(stage=TaskStage.STRM_READY, submission_id=int(row["id"]))

            result = workflow._stage_strm_ready(task)

            self.assertEqual(result.outcome.value, "defer")
            self.assertIn("等待", result.message)
```

Adjust helper method names to the exact existing test fixture names in `tests/test_bridge_task_engine.py`.

- [ ] **Step 5: Run focused TaskRunner tests**

Run:

```bash
python3 -m unittest tests.test_bridge_v02_integration tests.test_bridge_task_engine -v
```

Expected: new TaskEngine ownership and STRM source tests pass.

- [ ] **Step 6: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add bridge.py tests/test_bridge_v02_integration.py tests/test_bridge_task_engine.py
git commit -m "fix: keep new self-share links on task runner path"
```

---

### Task 8: Thin `bridge.py` and Remove Temporary Circular Imports

**Files:**
- Modify: `bridge.py`
- Modify: `app/clients/p115.py`
- Modify: `app/clients/emby.py`
- Modify: `app/media/strm.py`
- Modify: `app/workflows/self_share.py`
- Test: full suite

- [ ] **Step 1: Search for temporary imports from `bridge`**

Run:

```bash
rg -n "from bridge import|import bridge" app bridge.py
```

Expected before cleanup: no app module should import `bridge`. If any app module imports `bridge`, move the referenced function into `app/config.py`, `app/media/classify.py`, `app/media/strm.py`, or `app/workflows/self_share.py` based on responsibility.

- [ ] **Step 2: Search for duplicate definitions in `bridge.py`**

Run:

```bash
rg -n "^(class Config|class MoveConfig|class SelfShareConfig|class P115WebClient|class CmsClient|class EmbyClient|class BridgeSelfShareTaskWorkflow|def validate_self_share_strm_source|def merge_self_share_strm_folder|def normalize_text|def infer_region_category)" bridge.py
```

Expected: no class/function definitions for moved symbols remain in `bridge.py`; only imports should remain.

- [ ] **Step 3: Keep compatibility exports explicit**

At the bottom or import section of `bridge.py`, ensure moved public symbols are imported into module scope so existing tests and user scripts can still use `bridge.P115WebClient`, `bridge.MoveConfig`, etc.

No code block is needed if the imports already exist from previous tasks.

- [ ] **Step 4: Compile all modules**

Run:

```bash
python3 -m py_compile bridge.py doctor.py app/*.py app/clients/*.py app/media/*.py app/workflows/*.py
```

Expected: no syntax errors.

- [ ] **Step 5: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add bridge.py app/clients app/media app/workflows
git commit -m "refactor: thin bridge entrypoint"
```

---

### Task 9: Update Documentation and Deployment Notes

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_docs_v02.py`, `tests/test_docs_task_engine.py`

- [ ] **Step 1: Update README architecture section**

In `README.md`, add a short section after `v0.2 Alpha.2：TaskStore 接管新链接`:

```markdown
## 后端结构

后端按职责拆分为配置、外部客户端、媒体文件操作、分类识别和自分享工作流几个模块。`bridge.py` 只作为启动入口和兼容层；新 self-share 链接的真实执行状态由 TaskRunner 推进。

自分享工作流的最终 STRM 必须来自自己的 115 永久分享：程序在移动入库前会校验 `.strm` 内容包含 `/s/<own_share_code>_<receive_code>_`，并拒绝 `/d/` 直链 STRM。CMS 普通同步产生的直链 STRM 最多作为分类参考，不会作为最终入库来源。
```

- [ ] **Step 2: Update CHANGELOG**

At the top of `CHANGELOG.md`, add:

```markdown
## Unreleased

- Refactored backend internals into focused modules for config, service clients, media STRM operations, classification, and self-share workflows.
- Kept `bridge.py` as the executable entrypoint and compatibility facade.
- Strengthened self-share STRM safety checks so direct `/d/` STRM files cannot be moved as final library output.
```

- [ ] **Step 3: Run documentation tests**

Run:

```bash
python3 -m unittest tests.test_docs_v02 tests.test_docs_task_engine -v
```

Expected: documentation tests pass.

- [ ] **Step 4: Run full tests and commit**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
git add README.md CHANGELOG.md
git commit -m "docs: document backend refactor structure"
```

---

### Task 10: Final Verification and Deployment Dry Run

**Files:**
- Modify: none unless verification exposes a defect
- Test: full local suite, Docker build

- [ ] **Step 1: Check git status**

Run:

```bash
git status --short
```

Expected: clean worktree before final verification. If not clean, inspect every changed file and either commit intentional changes or stop and ask the user.

- [ ] **Step 2: Run compile check**

Run:

```bash
python3 -m py_compile bridge.py doctor.py app/*.py app/clients/*.py app/media/*.py app/workflows/*.py
```

Expected: no output and exit code 0.

- [ ] **Step 3: Run full test suite**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 4: Build Docker image locally**

Run:

```bash
docker build -t cms-tg-ingest:refactor-check .
```

Expected: build succeeds.

- [ ] **Step 5: Run doctor inside image without secrets**

Run:

```bash
docker run --rm cms-tg-ingest:refactor-check python /app/doctor.py || true
```

Expected: command starts and reports missing configuration rather than crashing with import or syntax errors.

- [ ] **Step 6: Commit final verification note if any docs changed**

If no files changed, do not create a commit. If a verification defect required a fix, commit it with:

```bash
git add <fixed files>
git commit -m "fix: address backend refactor verification issue"
```

---

## Self-Review

Spec coverage:

- Module split: Tasks 2 through 6.
- TaskRunner ownership: Task 7.
- STRM safety gate: Task 4 and Task 7.
- Web de-emphasis: no Web UI task added.
- Avoid broad 115 scans: no task adds scans; P115 extraction keeps existing target search behavior.
- Documentation: Task 9.
- Verification: Task 10.

Placeholder scan: reviewed for unfinished-marker words and unspecified implementation steps; none are intentionally present.

Type consistency: `Config`, `MoveConfig`, `MovePlan`, `SelfShareConfig`, `P115WebClient`, `CmsClient`, `EmbyClient`, `SelfShareWorkflow`, and `BridgeSelfShareTaskWorkflow` names remain unchanged and are compatibility-exported by `bridge.py`.
