import io
import json
import logging
import threading
import time
import unittest

from app.background_jobs import BackgroundJobCoordinator, LOG, redact_background_text


class BackgroundJobCoordinatorTests(unittest.TestCase):
    def test_failure_redacts_bearer_and_api_key_from_state_and_logs(self):
        class StateStore:
            def __init__(self):
                self.values = {}

            def set_runtime_state(self, key, value):
                self.values[key] = value

        state_store = StateStore()
        coordinator = BackgroundJobCoordinator(state_store=state_store)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LOG.addHandler(handler)
        try:
            coordinator.submit(
                "quality:run",
                lambda: (_ for _ in ()).throw(
                    RuntimeError("Authorization: Bearer bearer-secret X-API-Key: api-key-secret token=legacy-secret")
                ),
            )
            coordinator.shutdown(wait=True)

            values = [coordinator.snapshot("quality:run").error, state_store.values["background_job:quality:run"], stream.getvalue()]
            self.assertTrue(all("bearer-secret" not in value for value in values))
            self.assertTrue(all("api-key-secret" not in value for value in values))
            self.assertTrue(all("legacy-secret" not in value for value in values))
            self.assertIn("[redacted]", json.loads(state_store.values["background_job:quality:run"])["error"])
        finally:
            LOG.removeHandler(handler)
            coordinator.shutdown(wait=True)

    def test_failure_redacts_compound_keys_and_every_cookie_segment_everywhere(self):
        class StateStore:
            def __init__(self):
                self.values = {}

            def set_runtime_state(self, key, value):
                self.values[key] = value

        state_store = StateStore()
        coordinator = BackgroundJobCoordinator(state_store=state_store)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LOG.addHandler(handler)
        error = (
            "upstream rejected request: refresh_token=refresh-secret OPENAI_API_KEY=openai-secret "
            "TG_BOT_TOKEN=tg-secret monkey=banana Cookie: session=session-secret; csrf=csrf-secret; preference=light"
        )
        try:
            coordinator.submit("quality:run", lambda: (_ for _ in ()).throw(RuntimeError(error)))
            coordinator.shutdown(wait=True)
            from app.web_api import api_quality
            from app.task_store import TaskStore
            from pathlib import Path
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                api_payload = json.dumps(api_quality(TaskStore(Path(tmp) / "tasks.db"), background_jobs=coordinator))
            values = [
                coordinator.snapshot("quality:run").error,
                state_store.values["background_job:quality:run"],
                api_payload,
                stream.getvalue(),
            ]
            for secret in ("refresh-secret", "openai-secret", "tg-secret", "session-secret", "csrf-secret", "light"):
                self.assertTrue(all(secret not in value for value in values))
            self.assertIn("upstream rejected request", values[0])
            self.assertIn("monkey=banana", values[0])
            self.assertLessEqual(len(values[0]), 160)
        finally:
            LOG.removeHandler(handler)
            coordinator.shutdown(wait=True)

    def test_redacts_explicit_key_families_and_empty_cookie_segments_everywhere(self):
        class StateStore:
            def __init__(self):
                self.values = {}

            def set_runtime_state(self, key, value):
                self.values[key] = value

        state_store = StateStore()
        coordinator = BackgroundJobCoordinator(state_store=state_store)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LOG.addHandler(handler)
        description = "AWS_ACCESS_KEY_ID=DESCA API_ACCESS_KEY=DESCB SECRET_KEY=DESCC PRIVATE_KEY=DESCD"
        error = (
            "request failed Cookie: first=; session=ZZ1; middle=; csrf=ZZ2 "
            "AWS_ACCESS_KEY_ID=ZZ3 API_ACCESS_KEY=ZZ4 SECRET_KEY=ZZ5 PRIVATE_KEY=ZZ6"
        )
        try:
            coordinator.submit(
                "quality:run",
                lambda: (_ for _ in ()).throw(RuntimeError(error)),
                description=description,
            )
            coordinator.shutdown(wait=True)
            from app.web_api import api_quality
            from app.task_store import TaskStore
            from pathlib import Path
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                api_payload = json.dumps(api_quality(TaskStore(Path(tmp) / "tasks.db"), background_jobs=coordinator))
            snapshot = coordinator.snapshot("quality:run")
            values = [
                snapshot.description,
                snapshot.error,
                state_store.values["background_job:quality:run"],
                api_payload,
                stream.getvalue(),
            ]
            for secret in ("DESCA", "DESCB", "DESCC", "DESCD", "ZZ1", "ZZ2", "ZZ3", "ZZ4", "ZZ5", "ZZ6"):
                self.assertTrue(all(secret not in value for value in values))
            self.assertIn("request failed", snapshot.error)
            self.assertLessEqual(len(snapshot.error), 160)
        finally:
            LOG.removeHandler(handler)
            coordinator.shutdown(wait=True)

    def test_redactor_preserves_ordinary_diagnostic_key_names(self):
        text = "task_key=task operation_key=operation primary_key=primary sort_key=sort monkey=banana"

        self.assertEqual(redact_background_text(text), text)

    def test_state_persistence_exception_does_not_log_its_credential(self):
        class FailingStateStore:
            def set_runtime_state(self, _key, _value):
                raise RuntimeError("Authorization: Bearer state-store-secret")

        coordinator = BackgroundJobCoordinator(state_store=FailingStateStore())
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        LOG.addHandler(handler)
        try:
            self.assertEqual(coordinator.submit("quality:run", lambda: None).outcome, "accepted")
            coordinator.shutdown(wait=True)
            self.assertNotIn("state-store-secret", stream.getvalue())
        finally:
            LOG.removeHandler(handler)
            coordinator.shutdown(wait=True)

    def test_completion_callback_keeps_key_and_capacity_reserved_until_it_returns(self):
        coordinator = BackgroundJobCoordinator(max_in_flight=1)
        callback_started = threading.Event()
        release_callback = threading.Event()

        def on_complete(_snapshot):
            callback_started.set()
            release_callback.wait(1)

        try:
            self.assertEqual(coordinator.submit("quality:run", lambda: None, on_complete=on_complete).outcome, "accepted")
            self.assertTrue(callback_started.wait(1))
            self.assertEqual(coordinator.submit("quality:run", lambda: None).outcome, "already_running")
            self.assertEqual(coordinator.submit("hdhive:run", lambda: None).outcome, "capacity_rejected")
        finally:
            release_callback.set()
            coordinator.shutdown(wait=True)

    def test_completion_callback_error_does_not_leak_reservation_or_kill_worker(self):
        coordinator = BackgroundJobCoordinator(max_in_flight=1)
        callback_finished = threading.Event()
        second_job_finished = threading.Event()

        def broken_callback(_snapshot):
            callback_finished.set()
            raise RuntimeError("callback failure")

        try:
            coordinator.submit("quality:run", lambda: None, on_complete=broken_callback)
            self.assertTrue(callback_finished.wait(1))
            deadline = time.monotonic() + 1
            while True:
                submission = coordinator.submit("quality:run", second_job_finished.set)
                if submission.outcome == "accepted":
                    break
                self.assertEqual(submission.outcome, "already_running")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail("callback reservation did not release")
                threading.Event().wait(min(0.01, remaining))
            self.assertTrue(second_job_finished.wait(1))
        finally:
            coordinator.shutdown(wait=True)

    def test_duplicate_submissions_execute_once(self):
        coordinator = BackgroundJobCoordinator()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def job():
            calls.append(1)
            started.set()
            release.wait(1)

        try:
            submissions = [coordinator.submit("quality:run", job, description="Quality run")]
            self.assertTrue(started.wait(1))
            submissions.extend(coordinator.submit("quality:run", job, description="Quality run") for _ in range(19))
            self.assertEqual([submission.outcome for submission in submissions].count("accepted"), 1)
            self.assertEqual([submission.outcome for submission in submissions].count("already_running"), 19)
            release.set()
            coordinator.shutdown(wait=True)
            self.assertEqual(calls, [1])
        finally:
            release.set()
            coordinator.shutdown(wait=True)

    def test_capacity_limits_unique_jobs_to_eight_in_flight(self):
        coordinator = BackgroundJobCoordinator(max_in_flight=8)
        started = threading.Event()
        release = threading.Event()

        def blocking_job():
            started.set()
            release.wait(1)

        try:
            submissions = [coordinator.submit("quality:run", blocking_job)]
            self.assertTrue(started.wait(1))
            submissions.extend(coordinator.submit(f"hdhive:subscription:{index}", lambda: None) for index in range(20))
            self.assertEqual([submission.outcome for submission in submissions].count("accepted"), 8)
            self.assertEqual([submission.outcome for submission in submissions].count("capacity_rejected"), 13)
        finally:
            release.set()
            coordinator.shutdown(wait=True)

    def test_executor_uses_one_worker_thread(self):
        coordinator = BackgroundJobCoordinator(max_workers=1)
        threads = set()

        try:
            for index in range(4):
                coordinator.submit(f"hdhive:item:{index}", lambda: threads.add(threading.current_thread().name))
            coordinator.shutdown(wait=True)
            self.assertEqual(len(threads), 1)
            self.assertTrue(next(iter(threads)).startswith("background-jobs"))
        finally:
            coordinator.shutdown(wait=True)

    def test_failure_records_short_error_and_calls_completion(self):
        coordinator = BackgroundJobCoordinator()
        completed = []

        try:
            submission = coordinator.submit(
                "hdhive:run",
                lambda: (_ for _ in ()).throw(RuntimeError("x" * 200)),
                description="HDHive run",
                on_complete=completed.append,
            )
            self.assertEqual(submission.outcome, "accepted")
            coordinator.shutdown(wait=True)
            snapshot = coordinator.snapshot("hdhive:run")
            self.assertEqual(snapshot.status, "failed")
            self.assertLessEqual(len(snapshot.error), 160)
            self.assertEqual(completed, [snapshot])
        finally:
            coordinator.shutdown(wait=True)

    def test_shutdown_rejects_new_work_and_joins_worker(self):
        coordinator = BackgroundJobCoordinator()
        started = threading.Event()
        release = threading.Event()

        try:
            coordinator.submit("quality:run", lambda: (started.set(), release.wait(1)))
            self.assertTrue(started.wait(1))
            release.set()
            coordinator.shutdown(wait=True)
            self.assertEqual(coordinator.submit("quality:run", lambda: None).outcome, "closed")
            self.assertFalse(any(thread.name.startswith("background-jobs") for thread in threading.enumerate()))
        finally:
            release.set()
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
