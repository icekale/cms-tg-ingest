import unittest

import bridge


class UnifiedRuntimeGateTests(unittest.TestCase):
    def test_bridge_does_not_export_legacy_executor_types(self):
        self.assertFalse(hasattr(bridge, "SubmissionStore"))
        self.assertFalse(hasattr(bridge, "best_effort_task_sync"))
