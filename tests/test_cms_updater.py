import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.cms_updater import (
    CmsVersionChecker,
    _split_image,
    docker_container_started_at,
    docker_create_container,
    docker_pull_image,
    fetch_remote_latest_tag,
)
from app.clients.cms import CmsClient
from app.task_store import TaskStore
from app.web_api import api_cms_version


class FakeCms:
    def __init__(self, version):
        self.version = version

    def get_version(self):
        return self.version


class CmsUpdaterTests(unittest.TestCase):
    def make_store(self, tmp):
        return TaskStore(Path(tmp) / "tasks.db")

    def test_initial_check_sets_baseline_without_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(store, FakeCms("1.0.0"), enabled=True)
            notified = []

            payload = checker.check(notify=lambda version, pull: notified.append(version))

            self.assertEqual(payload["current_version"], "1.0.0")
            self.assertFalse(payload["update_ready"])
            self.assertEqual(notified, [])

    def test_new_version_detects_update_and_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(store, FakeCms("1.0.0"), enabled=True)
            checker.check()
            checker.cms.version = "1.1.0"
            notified = []

            payload = checker.check(notify=lambda version, pull: notified.append(version))

            self.assertTrue(payload["update_ready"])
            self.assertEqual(notified, ["1.1.0"])
            state = json.loads(store.get_runtime_state("cms_version_state")["value"])
            self.assertEqual(state["current_version"], "1.1.0")

    def test_update_ready_clears_after_container_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("1.0.0"),
                enabled=True,
                docker_socket="/tmp/fake.sock",
                container="cms",
            )
            with patch(
                "app.cms_updater.docker_container_started_at", return_value="2026-08-06T10:00:00Z"
            ):
                checker.check()
                checker.cms.version = "1.1.0"
                self.assertTrue(checker.check()["update_ready"])
                # Admin updates the container; it restarts with a new StartedAt
                # and now reports the previously seen version.
                with patch(
                    "app.cms_updater.docker_container_started_at",
                    return_value="2026-08-06T11:00:00Z",
                ):
                    payload = checker.check()
                self.assertFalse(payload["update_ready"])
                self.assertEqual(payload["last_container_started_at"], "2026-08-06T11:00:00Z")

    def test_update_ready_survives_maintenance_restart_of_old_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("1.0.0"),
                enabled=True,
                docker_socket="/tmp/fake.sock",
                container="cms",
            )
            with patch(
                "app.cms_updater.docker_container_started_at", return_value="2026-08-06T10:00:00Z"
            ):
                checker.check()
                checker.cms.version = "1.1.0"
                self.assertTrue(checker.check()["update_ready"])
                # A restart that does NOT adopt the new version re-arms the flag.
                checker.cms.version = "1.0.0"
                with patch(
                    "app.cms_updater.docker_container_started_at",
                    return_value="2026-08-06T11:00:00Z",
                ):
                    payload = checker.check()
                self.assertTrue(payload["update_ready"])

    def test_docker_container_started_at_returns_empty_on_error(self):
        self.assertEqual(docker_container_started_at("/tmp/does-not-exist.sock", "cms"), "")
        self.assertEqual(docker_container_started_at("", "cms"), "")
        self.assertEqual(docker_container_started_at("/tmp/fake.sock", ""), "")

    def test_auto_pull_is_attempted_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("1.0.0"),
                enabled=True,
                image="icekale/cms:latest",
                auto_pull=True,
                docker_socket="/tmp/does-not-exist.sock",
            )
            checker.check()
            checker.cms.version = "1.1.0"
            with patch("app.cms_updater.docker_pull_image", return_value="pulled") as pull:
                payload = checker.check()

            pull.assert_called_once()
            self.assertEqual(payload["pull_result"], "pulled")

    def test_docker_pull_handles_missing_socket_gracefully(self):
        result = docker_pull_image("/tmp/does-not-exist.sock", "icekale/cms", "latest")
        self.assertTrue(result.startswith("pull error") or "no docker socket" in result)

    def test_docker_pull_parses_tag_from_image_ref(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, size):
                return b""

            def close(self):
                pass

        class FakeConn:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, url):
                captured["url"] = url

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with patch("app.cms_updater._UnixHTTPConnection", FakeConn):
            result = docker_pull_image(
                "/tmp/fake.sock", "imaliang/cloud-media-sync:latest"
            )

        self.assertEqual(result, "pulled")
        self.assertNotIn("%3Alatest", captured["url"])
        self.assertIn("/images/create?", captured["url"])
        self.assertIn("fromImage=imaliang%2Fcloud-media-sync", captured["url"])
        self.assertIn("tag=latest", captured["url"])

    def test_docker_pull_omits_tag_for_digest(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, size):
                return b""

            def close(self):
                pass

        class FakeConn:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, url):
                captured["url"] = url

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with patch("app.cms_updater._UnixHTTPConnection", FakeConn):
            result = docker_pull_image("/tmp/fake.sock", "repo/app@sha256:abcd")

        self.assertEqual(result, "pulled")
        self.assertIn("fromImage=repo%2Fapp%40sha256%3Aabcd", captured["url"])
        self.assertNotIn("tag=", captured["url"])

    def test_docker_pull_consumes_full_stream(self):
        reads = []

        class FakeResponse:
            status = 200

            def __init__(self):
                self.remaining = 65536 * 3

            def read(self, size):
                if self.remaining <= 0:
                    return b""
                amount = min(size, self.remaining)
                self.remaining -= amount
                reads.append(amount)
                return b"x" * amount

            def close(self):
                pass

        class FakeConn:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, url):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with patch("app.cms_updater._UnixHTTPConnection", FakeConn):
            result = docker_pull_image("/tmp/fake.sock", "nginx:1.25")

        self.assertEqual(result, "pulled")
        self.assertGreater(len(reads), 2)

    def test_api_cms_version_reports_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(store, FakeCms("2.0.0"), enabled=True)
            checker.check()

            payload = api_cms_version(checker)

            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["current_version"], "2.0.0")
            self.assertFalse(payload["update_ready"])

    def test_api_cms_version_disabled_without_checker(self):
        payload = api_cms_version(None)
        self.assertFalse(payload["enabled"])

    def test_api_cms_version_reports_disabled_when_checker_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(store, FakeCms("1.0.0"))

            payload = api_cms_version(checker)

            self.assertFalse(payload["enabled"])

    def test_runtime_overrides_change_effective_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(store, FakeCms("1.0.0"))

            updated = checker.update_settings(
                {
                    "enabled": True,
                    "interval_seconds": 86400,
                    "image": "imaliang/cloud-media-sync:latest",
                    "container": "cloud-media-sync",
                    "auto_pull": True,
                }
            )

            self.assertTrue(updated["enabled"])
            self.assertEqual(updated["interval_seconds"], 86400)
            self.assertEqual(updated["image"], "imaliang/cloud-media-sync:latest")
            self.assertTrue(updated["auto_pull"])
            self.assertEqual(checker.effective_interval(), 86400)
            checker.reset_settings()
            self.assertFalse(checker._effective()["enabled"])

    def test_remote_lookup_reports_update_available_without_flipping_update_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("0.4.9.1"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                remote_lookup=lambda image: "0.4.9.2",
            )
            checker.check()  # establish baseline, remote == current

            payload = checker.check()

            self.assertEqual(payload["remote_version"], "0.4.9.2")
            self.assertTrue(payload["update_available"])
            self.assertFalse(payload["update_ready"])  # local running version unchanged
            self.assertIn("远程新版本 0.4.9.2", payload["message"])

    def test_remote_lookup_up_to_date_sets_no_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("0.4.9.2"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                remote_lookup=lambda image: "0.4.9.2",
            )

            payload = checker.check()

            self.assertEqual(payload["remote_version"], "0.4.9.2")
            self.assertFalse(payload["update_available"])
            self.assertFalse(payload["update_ready"])

    def test_remote_lookup_failure_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            checker = CmsVersionChecker(
                store,
                FakeCms("1.0.0"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                remote_lookup=lambda image: (_ for _ in ()).throw(RuntimeError("network down")),
            )

            payload = checker.check()

            self.assertEqual(payload["remote_version"], "")
            self.assertFalse(payload["update_available"])

    def test_split_image_accepts_only_docker_hub_refs(self):
        self.assertEqual(_split_image("imaliang/cloud-media-sync:latest"), ("imaliang/cloud-media-sync", "latest"))
        self.assertEqual(_split_image("icekale/cms-tg-ingest:0.2.92"), ("icekale/cms-tg-ingest", "0.2.92"))
        self.assertEqual(_split_image(""), ("", ""))
        self.assertEqual(_split_image("nginx"), ("", ""))
        self.assertEqual(_split_image("nginx:1.25"), ("", ""))
        self.assertEqual(_split_image("ghcr.io/owner/app:v1"), ("", ""))
        self.assertEqual(_split_image("repo/app@sha256:abcd"), ("", ""))

    def test_fetch_remote_latest_tag_skips_latest(self):
        self.assertEqual(fetch_remote_latest_tag(""), "")
        self.assertEqual(fetch_remote_latest_tag("nginx"), "")

    def test_fetch_remote_latest_tag_builds_slash_preserving_url(self):
        # Regression: the repo slash is a path separator and must NOT be
        # escaped to %2F (Docker Hub answers 400 on an escaped repo path).
        captured = {}

        class FakeResponse:
            def __init__(self):
                self.payload = json.dumps(
                    {"results": [{"name": "latest"}, {"name": "0.4.9.2"}]}
                ).encode()

            def read(self, _size=None):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeUrlOpen:
            def __init__(self, request, timeout=0):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.headers.items())

            def __enter__(self):
                return FakeResponse()

            def __exit__(self, *args):
                return False

        with patch("app.cms_updater.urllib.request.urlopen", FakeUrlOpen):
            result = fetch_remote_latest_tag("imaliang/cloud-media-sync:0.4.9.1")

        self.assertEqual(result, "0.4.9.2")
        self.assertIn("/repositories/imaliang/cloud-media-sync/tags", captured["url"])
        self.assertNotIn("%2F", captured["url"])
        self.assertIn("page_size=25", captured["url"])
        self.assertIn("ordering=last_updated", captured["url"])


class CmsVersionClientTests(unittest.TestCase):
    def test_login_ignores_version_when_login_fails(self):
        cms = CmsClient.__new__(CmsClient)
        cms._cached_version = ""
        cms.config = Mock(cms_base_url="http://cms", cms_username="u", cms_password="p")
        cms.http = Mock(request=Mock(return_value={"code": 500, "data": {"version": "v9.9"}}))

        with self.assertRaises(RuntimeError):
            cms.login()

        self.assertEqual(cms._cached_version, "")

    def test_get_version_uses_login_response(self):
        cms = CmsClient.__new__(CmsClient)

        def fake_login():
            cms._cached_version = "v1.2.3"

        with patch.object(cms, "login", side_effect=fake_login):
            self.assertEqual(cms.get_version(), "v1.2.3")

    def test_get_version_probes_common_endpoints_as_fallback(self):
        cms = CmsClient.__new__(CmsClient)
        calls = []

        def fake_authorized(path, method="POST", params=None, safe_get_attempts=None):
            calls.append(path)
            if path == "/api/version":
                return {"data": {"version": "1.2.3"}}
            return {"data": {}}

        cms._authorized = fake_authorized
        with patch.object(cms, "login", side_effect=lambda: setattr(cms, "_cached_version", "")):
            self.assertEqual(cms.get_version(), "1.2.3")

        self.assertIn("/api/version", calls)


if __name__ == "__main__":
    unittest.main()


class CmsUpdaterPullTests(unittest.TestCase):
    def test_pull_calls_docker_pull_and_marks_state(self):
        import tempfile
        from pathlib import Path

        from app.task_store import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            checker = CmsVersionChecker(
                store,
                FakeCms("0.4.9.1"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                docker_socket="/tmp/fake.sock",
            )
            checker.check()

            with patch("app.cms_updater.docker_pull_image", return_value="pulled") as pull:
                payload = checker.pull()

            pull.assert_called_once()
            self.assertEqual(payload["pull_result"], "pulled")
            self.assertIn("镜像已拉取", payload["message"])

    def test_pull_failure_reports_result(self):
        import tempfile
        from pathlib import Path

        from app.task_store import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            checker = CmsVersionChecker(
                store,
                FakeCms("0.4.9.1"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                docker_socket="/tmp/fake.sock",
            )
            checker.check()

            with patch("app.cms_updater.docker_pull_image", return_value="pull error: connect refused"):
                payload = checker.pull()

            self.assertEqual(payload["pull_result"], "pull error: connect refused")
            self.assertIn("镜像拉取失败", payload["message"])


class CmsUpgradeTests(unittest.TestCase):
    def _checker(self, tmp, **kwargs):
        store = TaskStore(Path(tmp) / "tasks.db")
        defaults = dict(
            enabled=True,
            image="imaliang/cloud-media-sync:latest",
            container="cloud-media-sync",
            docker_socket="/tmp/fake.sock",
            remote_lookup=lambda image: "0.4.9.2",
        )
        defaults.update(kwargs)
        return CmsVersionChecker(store, FakeCms("0.4.9.1"), **defaults)

    def test_create_keeps_container_name_when_network_is_cms_default(self):
        captured = {}

        def fake_api(_socket, _method, path, body=None, timeout=30.0, headers=None):
            captured["path"] = path
            captured["body"] = json.loads(body.decode())
            return 201, b'{"Id":"abc"}'

        inspect = {
            "Config": {"Image": "imaliang/cloud-media-sync:0.4.9.2", "Env": ["A=1"]},
            "HostConfig": {"NetworkMode": "cms_default"},
            "NetworkSettings": {
                "Networks": {"cms_default": {"Aliases": ["cloud-media-sync"]}},
            },
        }
        with patch("app.cms_updater._docker_api", side_effect=fake_api):
            error = docker_create_container(
                "/var/run/docker.sock",
                "cloud-media-sync",
                inspect,
                "imaliang/cloud-media-sync:0.4.9.3",
            )

        self.assertEqual(error, "")
        self.assertIn("name=cloud-media-sync", captured["path"])
        self.assertNotIn("cms_default", captured["path"])
        self.assertEqual(captured["body"]["Image"], "imaliang/cloud-media-sync:0.4.9.3")
        self.assertIn("cms_default", captured["body"]["NetworkingConfig"]["EndpointsConfig"])

    def test_upgrade_recreates_container_and_removes_old(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            checker = self._checker(tmp)
            checker.check()
            with patch.multiple(
                "app.cms_updater",
                create=True,
                docker_pull_image=lambda socket, image, tag="latest": calls.append(("pull", image)) or "pulled",
                docker_inspect_container=lambda socket, name: {
                    "Config": {"Image": "imaliang/cloud-media-sync:0.4.9.1", "Env": ["A=1"]},
                    "HostConfig": {"NetworkMode": "bridge"},
                    "NetworkSettings": {"Networks": {}},
                },
                docker_rename_container=lambda socket, name, new: calls.append(("rename", name, new)) or "",
                docker_stop_container=lambda socket, name: calls.append(("stop", name)) or "",
                docker_create_container=lambda socket, name, inspect, image: calls.append(("create", name, image)) or "",
                docker_start_container=lambda socket, name: calls.append(("start", name)) or "",
                docker_wait_running=lambda socket, name, timeout=60: True,
                docker_remove_container=lambda socket, name: calls.append(("remove", name)) or "",
                verify_cms_guards=lambda **kwargs: (True, ""),
            ):
                payload = checker.upgrade("0.4.9.2")

        self.assertEqual(payload["upgrade_status"], "succeeded")
        self.assertIn(("pull", "imaliang/cloud-media-sync:0.4.9.2"), calls)
        self.assertIn(("rename", "cloud-media-sync", "cloud-media-sync-pre-upgrade"), calls)
        self.assertIn(("create", "cloud-media-sync", "imaliang/cloud-media-sync:0.4.9.2"), calls)
        self.assertIn(("remove", "cloud-media-sync-pre-upgrade"), calls)
        self.assertNotIn(("remove", "cloud-media-sync"), calls)

    def test_upgrade_rolls_back_when_guards_fail(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            checker = self._checker(tmp)
            with patch.multiple(
                "app.cms_updater",
                create=True,
                docker_pull_image=lambda socket, image, tag="latest": "pulled",
                docker_inspect_container=lambda socket, name: {"Config": {}, "HostConfig": {}, "NetworkSettings": {}},
                docker_rename_container=lambda socket, name, new: calls.append(("rename", name, new)) or "",
                docker_stop_container=lambda socket, name: calls.append(("stop", name)) or "",
                docker_create_container=lambda socket, name, inspect, image: "",
                docker_start_container=lambda socket, name: calls.append(("start", name)) or "",
                docker_wait_running=lambda socket, name, timeout=60: True,
                docker_remove_container=lambda socket, name: calls.append(("remove", name)) or "",
                verify_cms_guards=lambda **kwargs: (False, "守卫缺失"),
            ):
                payload = checker.upgrade("0.4.9.2")

        self.assertEqual(payload["upgrade_status"], "failed")
        self.assertIn("守卫", payload["upgrade_error"])
        self.assertIn(("remove", "cloud-media-sync"), calls)
        self.assertIn(("rename", "cloud-media-sync-pre-upgrade", "cloud-media-sync"), calls)
        self.assertIn(("start", "cloud-media-sync"), calls)

    def test_upgrade_rolls_back_when_create_fails(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            checker = self._checker(tmp)
            with patch.multiple(
                "app.cms_updater",
                create=True,
                docker_pull_image=lambda socket, image, tag="latest": "pulled",
                docker_inspect_container=lambda socket, name: {"Config": {}, "HostConfig": {}, "NetworkSettings": {}},
                docker_rename_container=lambda socket, name, new: calls.append(("rename", name, new)) or "",
                docker_stop_container=lambda socket, name: calls.append(("stop", name)) or "",
                docker_create_container=lambda socket, name, inspect, image: "create failed status=500",
                docker_start_container=lambda socket, name: calls.append(("start", name)) or "",
                docker_wait_running=lambda socket, name, timeout=60: True,
                docker_remove_container=lambda socket, name: calls.append(("remove", name)) or "",
                verify_cms_guards=lambda **kwargs: (True, ""),
            ):
                payload = checker.upgrade("0.4.9.2")

        self.assertEqual(payload["upgrade_status"], "failed")
        self.assertIn("create failed", payload["upgrade_error"])
        self.assertIn(("rename", "cloud-media-sync-pre-upgrade", "cloud-media-sync"), calls)
        self.assertIn(("start", "cloud-media-sync"), calls)
        self.assertNotIn(("remove", "cloud-media-sync"), calls)


class CmsUpgradeHintTests(unittest.TestCase):
    def test_hint_builds_host_commands_with_script_copy(self):
        from app.web_api import build_cms_upgrade_hint

        hint = build_cms_upgrade_hint("0.4.9.2")
        self.assertIn("docker cp cms-tg-ingest:/app/scripts/cms-strm-guard/", hint)
        self.assertIn("update-cms.sh", hint)
        self.assertIn("0.4.9.2", hint)
        self.assertIn("/boot/config/plugins/compose.manager/projects/CMS", hint)

    def test_hint_empty_without_version(self):
        from app.web_api import build_cms_upgrade_hint

        self.assertEqual(build_cms_upgrade_hint(""), "")

    def test_api_cms_version_includes_hint_when_update_available(self):
        import tempfile
        from pathlib import Path

        from app.task_store import TaskStore
        from app.web_api import api_cms_version

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            checker = CmsVersionChecker(
                store,
                FakeCms("0.4.9.1"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                remote_lookup=lambda image: "0.4.9.2",
            )
            checker.check()
            payload = api_cms_version(checker)
            self.assertTrue(payload["update_available"])
            self.assertIn("update-cms.sh", payload["upgrade_hint"])


if __name__ == "__main__":
    unittest.main()


class VersionCoreTests(unittest.TestCase):
    def test_compares_numeric_core_of_mixed_version_strings(self):
        from app.cms_updater import _version_core

        self.assertEqual(_version_core("v0.4.9.2 - PRO"), "0.4.9.2")
        self.assertEqual(_version_core("0.4.9.2"), "0.4.9.2")
        self.assertEqual(_version_core(""), "")
        self.assertEqual(_version_core("abc"), "abc")

    def test_update_available_clears_after_upgrade_to_same_core(self):
        import tempfile
        from pathlib import Path

        from app.task_store import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            # CMS reports a suffixed version, Docker Hub tag is plain.
            checker = CmsVersionChecker(
                store,
                FakeCms("v0.4.9.2 - PRO"),
                enabled=True,
                image="imaliang/cloud-media-sync:latest",
                remote_lookup=lambda image: "0.4.9.2",
            )
            payload = checker.check()
            self.assertEqual(payload["current_version"], "v0.4.9.2 - PRO")
            self.assertEqual(payload["remote_version"], "0.4.9.2")
            self.assertFalse(payload["update_available"])


if __name__ == "__main__":
    unittest.main()
