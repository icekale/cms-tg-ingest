import unittest

from app.quality import QualityIssue, format_task_quality_report
from app.quality_automation import QualityRepairPlan, QualityRunSummary
from app.telegram_rich import RichDocument, bold, details, heading, paragraph, table
from app.telegram_ui import (
    format_history,
    format_metrics,
    format_quality_manual_report,
    format_quality_report,
    format_quality_scan_summary,
    format_status,
    format_taskstore_history,
    format_taskstore_status,
)
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
