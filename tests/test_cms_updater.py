import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.cms_updater import CmsVersionChecker, docker_pull_image
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


class CmsVersionClientTests(unittest.TestCase):
    def test_get_version_probes_common_endpoints(self):
        cms = CmsClient.__new__(CmsClient)
        calls = []

        def fake_authorized(path, method="POST", params=None, safe_get_attempts=None):
            calls.append(path)
            if path == "/api/version":
                return {"data": {"version": "1.2.3"}}
            return {"data": {}}

        cms._authorized = fake_authorized

        self.assertEqual(cms.get_version(), "1.2.3")
        self.assertIn("/api/version", calls)


if __name__ == "__main__":
    unittest.main()
