import unittest
from types import SimpleNamespace

from app.quality import QualityIssue, format_task_quality_report
from app.quality_automation import QualityRepairPlan, QualityRunSummary
from app.telegram_rich import RichDocument, bold, details, heading, paragraph, table
from app.telegram_ui import (
    format_hdhive_candidates,
    format_hdhive_unlock_result,
    format_history,
    format_metrics,
    format_quality_manual_report,
    format_quality_report,
    format_quality_scan_summary,
    format_status,
    format_taskstore_history,
    format_taskstore_status,
)
from app.workflows.self_share import format_task_label
from app.clients.hdhive import HdhiveUnlockItem
from app.models import TaskStage, TaskStatus
from bridge import _quality_attention_message


class TelegramRichTests(unittest.TestCase):
    def test_empty_document_is_false(self):
        self.assertFalse(RichDocument())
        self.assertTrue(RichDocument((heading("最近任务"),)))

    def test_heading_and_table_to_blocks(self):
        doc = RichDocument(
            (
                heading("最近任务"),
                table(("任务", "状态"), (("HK1", bold("OK")),)),
            )
        )
        blocks = doc.to_blocks()
        self.assertEqual(blocks[0]["type"], "heading")
        self.assertEqual(blocks[0]["size"], 3)
        self.assertEqual(blocks[0]["text"], "最近任务")
        self.assertEqual(blocks[1]["type"], "table")
        self.assertTrue(blocks[1]["is_bordered"])
        self.assertTrue(blocks[1]["is_striped"])
        header = blocks[1]["cells"][0][0]
        self.assertTrue(header["is_header"])
        self.assertEqual(header["align"], "left")
        self.assertEqual(header["valign"], "top")
        self.assertEqual(blocks[1]["cells"][1][1]["text"]["type"], "bold")
        self.assertEqual(blocks[1]["cells"][1][1]["text"]["text"], "OK")

    def test_to_plain_joins_tables_and_details(self):
        doc = RichDocument(
            (
                heading("最近任务"),
                table(("任务", "状态"), (("A", "ok"),)),
                details("等待", (paragraph("还在搬"),)),
            )
        )
        text = doc.to_plain()
        self.assertIn("最近任务", text)
        self.assertIn("任务 | 状态", text)
        self.assertIn("A | ok", text)
        self.assertIn("等待", text)
        self.assertIn("  还在搬", text)
        self.assertNotIn("**", text)

    def test_table_overflow_moves_extra_rows_to_details(self):
        rows = [(f"r{i}", "ok") for i in range(21)]
        doc = RichDocument((table(("任务", "状态"), rows),))
        blocks = doc.to_blocks()
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual(len(blocks[0]["cells"]), 21)
        self.assertEqual(blocks[1]["type"], "details")
        self.assertEqual(blocks[1]["summary"], "还有 1 条")
        self.assertFalse(blocks[1].get("is_open"))
        self.assertIn("还有 1 条", doc.to_plain())
        self.assertIn("r20 | ok", doc.to_plain())

    def test_with_leading_paragraph(self):
        doc = RichDocument((heading("订阅"),)).with_leading_paragraph("已设置集数过滤：S01")
        self.assertEqual(doc.to_blocks()[0]["type"], "paragraph")
        self.assertIn("已设置集数过滤：S01", doc.to_plain())
        self.assertEqual(RichDocument((heading("订阅"),)).with_leading_paragraph("").to_blocks()[0]["type"], "heading")


class TelegramUiRichTests(unittest.TestCase):
    def test_format_status_empty_is_paragraph(self):
        doc = format_status([])
        self.assertIn("暂无记录", doc.to_plain())
        self.assertEqual(doc.to_blocks()[0]["type"], "paragraph")

    def test_format_status_table(self):
        doc = format_status([{"title": "海贼王", "status": "done", "last_error": ""}])
        types = [block["type"] for block in doc.to_blocks()]
        self.assertIn("heading", types)
        self.assertIn("table", types)
        self.assertIn("最近任务", doc.to_plain())
        self.assertIn("海贼王", doc.to_plain())

    def test_format_metrics_is_key_value_table(self):
        doc = format_metrics({"generated_at": "t", "total": 2, "status_counts": {"done": 2}})
        self.assertEqual(doc.to_blocks()[0]["text"], "任务统计")
        self.assertEqual(doc.to_blocks()[1]["type"], "table")
        self.assertIn("总数", doc.to_plain())

    def test_quality_scan_summary_stays_str(self):
        self.assertIsInstance(format_quality_scan_summary([]), str)

    def test_quality_report_table(self):
        rows = [
            {
                "id": 72,
                "title": "航海王 (1999) {tmdb=37854}",
                "emby_status": "confirmed",
                "emby_title": "我是余欢水",
                "emby_path": "/mnt/user/Unraid/strm/转存/TVCN/W-我是余欢水-2020-[tmdb=101588]",
                "recognition_json": "{}",
            }
        ]
        doc = format_quality_report(rows)
        self.assertIn("疑似错配", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])

    def test_format_hdhive_candidates_is_bounded_and_structured(self):
        candidates = [
            {"title": "标题" * 50, "media_type": "movie", "year": "2024", "tmdb_id": str(index)}
            for index in range(13)
        ]
        doc = format_hdhive_candidates(candidates)
        blocks = doc.to_blocks()
        self.assertIsInstance(doc, RichDocument)
        self.assertEqual(blocks[0]["type"], "heading")
        self.assertEqual(blocks[1]["type"], "table")
        self.assertEqual(blocks[1]["cells"][0][0]["text"], "#")
        self.assertEqual(len(blocks[1]["cells"]) - 1, 12)
        self.assertIn("请选择", doc.to_plain())
        self.assertIn("…", doc.to_plain())

    def test_format_hdhive_candidates_empty_is_a_paragraph(self):
        doc = format_hdhive_candidates([])
        self.assertEqual(doc.to_blocks()[0]["type"], "paragraph")
        self.assertEqual(doc.to_plain(), "没有找到匹配的 TMDB 媒体。")

    def test_format_hdhive_unlock_result_hides_full_urls_and_reports_counts(self):
        secret_url = "https://115cdn.com/s/secret?password=4321"
        results = [
            HdhiveUnlockItem(
                "resource-a",
                True,
                secret_url,
                "已解锁 https://evil.test/s/leak?password=message-secret",
                "",
                False,
            ),
            HdhiveUnlockItem("resource-b", False, "", "积分不足", "POINTS", False),
        ]
        doc = format_hdhive_unlock_result(
            results,
            {"resource-a": "115", "resource-b": "quark"},
            enqueued_count=1,
            enqueue_error="",
        )
        plain = doc.to_plain()
        self.assertIn("HDHive 解锁结果", plain)
        self.assertIn("resource-a", plain)
        self.assertIn("成功", plain)
        self.assertIn("失败", plain)
        self.assertIn("已入队 1 个", plain)
        self.assertIn("非 115 资源：0 个", plain)
        self.assertNotIn(secret_url, plain)
        self.assertNotIn("message-secret", plain)
        self.assertNotIn("evil.test", plain)
        self.assertNotIn(secret_url, repr(doc.to_blocks()))

    def test_format_hdhive_unlock_result_redacts_and_bounds_failure_reason(self):
        secret_url = "https://evil.test/s/failure?password=failure-secret"
        long_slug = "resource-" + "s" * 120
        long_reason = f"failed {secret_url} password=assignment-secret " + ("x" * 300)
        doc = format_hdhive_unlock_result(
            [HdhiveUnlockItem(long_slug, False, "", long_reason, "FAIL", False)],
            {long_slug: "115"},
        )
        row = doc.to_blocks()[1]["cells"][1]
        self.assertLessEqual(len(row[0]["text"]), 80)
        self.assertLessEqual(len(row[2]["text"]), 160)
        self.assertNotIn("failure-secret", doc.to_plain())
        self.assertNotIn("assignment-secret", doc.to_plain())
        self.assertNotIn("https://evil.test", doc.to_plain())
        self.assertNotIn("failure-secret", repr(doc.to_blocks()))

        self.assertEqual(format_task_label({"cms_task_id": 7, "share_code": "secret"}), "任务 #7")
        task = SimpleNamespace(
            id=8,
            title="",
            share_code="secret",
            metadata={},
            category="",
            current_stage=TaskStage.RECEIVED,
            status=TaskStatus.PENDING,
            error_summary="",
            next_run_at=0,
            claimed_by="",
            claimed_at=None,
            updated_at=0,
            created_at=0,
        )
        self.assertNotIn("secret", format_taskstore_history([task]).to_plain())
        self.assertNotIn("secret", format_taskstore_status([task]).to_plain())

    def test_format_hdhive_unlock_result_uses_unknown_reason_when_failure_has_no_details(self):
        doc = format_hdhive_unlock_result(
            [HdhiveUnlockItem("resource-empty", False, "", "", "", False)],
            {"resource-empty": "115"},
        )
        self.assertIn("未知原因", doc.to_plain())
        self.assertIn("未知原因", repr(doc.to_blocks()))


class BridgeRichFormatterTests(unittest.TestCase):
    def test_task_quality_report_table(self):
        doc = format_task_quality_report(
            [QualityIssue(code="unexpected_strm", message="多余 STRM", detail="", task_id=4, title="剧")]
        )
        self.assertIn("TaskStore 轻量巡检", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])

    def test_task_quality_report_empty(self):
        self.assertIn("未发现本地 STRM 问题", format_task_quality_report([]).to_plain())

    def test_quality_attention_includes_run_id(self):
        summary = QualityRunSummary(
            run_id="run-1",
            status="ok",
            scanned_count=3,
            issue_count=1,
            failed_count=1,
            plans=(
                QualityRepairPlan(
                    task_id=8,
                    action="reprocess",
                    reason="失败",
                    title="剧",
                    execution_status="failed",
                ),
            ),
        )
        doc = _quality_attention_message(summary)
        self.assertIn("run-1", doc.to_plain())
        self.assertIn("质量巡检需要关注", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])


if __name__ == "__main__":
    unittest.main()
