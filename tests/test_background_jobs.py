import threading
import time
import unittest

from app.background_jobs import BackgroundJobCoordinator


class BackgroundJobCoordinatorTests(unittest.TestCase):
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
