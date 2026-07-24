import unittest

from app.clients.hdhive import HdhiveAccount, HdhiveResource, HdhiveUnlockItem
from app.hdhive import HdhiveSelectionError, HdhiveSessionStore, HdhiveWorkflow


def resource(slug, pan_type="115", points=8, status="valid", owned=False):
    return HdhiveResource(
        slug=slug,
        title=f"Title {slug}",
        pan_type=pan_type,
        share_size="10GB",
        video_resolution=("1080P",),
        source=("WEB-DL",),
        subtitle_language=("简中",),
        subtitle_type=("内封",),
        unlock_points=points,
        validate_status=status,
        validate_message="链接有效" if status == "valid" else "链接无效",
        is_unlocked=owned,
    )


class FakeCms:
    def search_movie(self, keyword, page=1, page_size=8):
        return {"code": 200, "data": {"results": [{"id": 550, "title": "Example", "release_date": "2026-01-01"}]}}

    def search_tv(self, keyword, page=1, page_size=8):
        return {"code": 200, "data": {"results": [{"id": 1399, "name": "Example TV", "first_air_date": "2026-01-01"}]}}


class FakeProxy:
    def __init__(self, resources, account=None, unlock_items=None):
        self.resource_items = resources
        self.account_value = account or HdhiveAccount("Kale", 100, 0, False, "user", False, False)
        self.unlock_items = unlock_items or []
        self.unlock_calls = []

    def account(self):
        return self.account_value

    def resources(self, media_type, tmdb_id):
        return self.resource_items

    def unlock(self, slugs):
        self.unlock_calls.append(list(slugs))
        return self.unlock_items


class HdhiveWorkflowTests(unittest.TestCase):
    def workflow(self, items, account=None, unlock_items=None, now=None):
        clock = (lambda: now[0]) if now is not None else None
        sessions = HdhiveSessionStore(ttl_seconds=900, clock=clock)
        proxy = FakeProxy(items, account=account, unlock_items=unlock_items)
        workflow = HdhiveWorkflow(FakeCms(), proxy, sessions, auto_unlock_max_points=20)
        return workflow, sessions, proxy

    def test_default_filter_is_115_and_invalid_resources_are_not_selectable(self):
        workflow, sessions, _proxy = self.workflow(
            [resource("good-115"), resource("bad-115", status="invalid"), resource("good-quark", "quark")]
        )
        session_id = sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")

        self.assertEqual(workflow.available_pan_types(session_id), ["115", "quark"])
        self.assertEqual(workflow.visible_resource_indexes(session_id), [0, 1])
        self.assertEqual(workflow.selectable_resource_indexes(session_id), [0])

    def test_session_expires_after_configured_ttl(self):
        now = [1000.0]
        workflow, sessions, _proxy = self.workflow([resource("good")], now=now)
        session_id = sessions.begin("464100862", "Example")
        now[0] = 1901.0

        self.assertIsNone(sessions.get(session_id))
        with self.assertRaisesRegex(HdhiveSelectionError, "会话已过期"):
            workflow.visible_resources(session_id)

    def test_single_unlock_at_20_points_is_immediate(self):
        items = [resource("at-20", points=20)]
        unlock_items = [HdhiveUnlockItem("at-20", True, "https://115cdn.com/s/a?password=x", "", "", False)]
        workflow, sessions, proxy = self.workflow(items, unlock_items=unlock_items)
        session_id = sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.toggle_selection(session_id, 0)

        preview = workflow.unlock_preview(session_id)
        result = workflow.unlock(session_id)

        self.assertFalse(preview.requires_confirmation)
        self.assertEqual(proxy.unlock_calls, [["at-20"]])
        self.assertTrue(result[0].success)

    def test_single_unlock_above_20_points_requires_confirmation(self):
        workflow, sessions, proxy = self.workflow([resource("high", points=21)])
        session_id = sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.toggle_selection(session_id, 0)

        self.assertTrue(workflow.unlock_preview(session_id).requires_confirmation)
        with self.assertRaisesRegex(HdhiveSelectionError, "确认"):
            workflow.unlock(session_id)
        self.assertEqual(proxy.unlock_calls, [])

    def test_batch_limit_uses_account_level(self):
        workflow, sessions, proxy = self.workflow([resource("one"), resource("two")])
        session_id = sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.toggle_selection(session_id, 0)
        workflow.set_filter(session_id, "115")
        workflow.toggle_selection(session_id, 1)

        with self.assertRaisesRegex(HdhiveSelectionError, "最多解锁 1"):
            workflow.unlock_preview(session_id)
        self.assertEqual(proxy.unlock_calls, [])

    def test_batch_result_preserves_partial_failures(self):
        account = HdhiveAccount("Kale", 100, 0, False, "vip", False, False)
        result_items = [
            HdhiveUnlockItem("one", True, "https://115cdn.com/s/a?password=x", "", "", False),
            HdhiveUnlockItem("two", False, "", "积分不足", "INSUFFICIENT_POINTS", False),
        ]
        workflow, sessions, proxy = self.workflow(
            [resource("one"), resource("two")], account=account, unlock_items=result_items
        )
        session_id = sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.toggle_selection(session_id, 0)
        workflow.toggle_selection(session_id, 1)

        result = workflow.unlock(session_id)

        self.assertEqual(proxy.unlock_calls, [["one", "two"]])
        self.assertTrue(result[0].success)
        self.assertEqual(result[1].error_code, "INSUFFICIENT_POINTS")

    def test_search_candidates_merges_movie_and_tv_results(self):
        workflow, _sessions, _proxy = self.workflow([])

        candidates = workflow.search_candidates("Example")

        self.assertEqual([(item["media_type"], item["tmdb_id"]) for item in candidates], [("movie", "550"), ("tv", "1399")])


if __name__ == "__main__":
    unittest.main()
