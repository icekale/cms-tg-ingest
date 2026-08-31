from app.task_runner import TaskRunner


def drain_task_commands(store) -> None:
    class IdleWorkflow:
        def run_stage(self, task):
            raise AssertionError("stage should not run while draining commands")

    runner = TaskRunner(store, IdleWorkflow(), worker_id="test-runner")
    while True:
        command = store.claim_next_command(runner.worker_id)
        if not command:
            return
        runner._run_command(command)
