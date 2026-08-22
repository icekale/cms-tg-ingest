import unittest

from app.telegram_rich import RichDocument, bold, details, heading, paragraph, table


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


if __name__ == "__main__":
    unittest.main()
