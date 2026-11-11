"""发布扫描器的识别和脱敏行为，样例凭据在运行时构造。"""

import unittest
from unittest.mock import patch

from scripts.audit_release import scan_text, scan_content, audit, PROJECT


class ReleaseScannerTests(unittest.TestCase):
    def test_recognizes_secret_without_returning_value(self):
        value = "sk-" + "Z" * 30
        result = scan_text(value)
        self.assertIn("疑似服务密钥", result)
        self.assertNotIn(value, str(result))

    def test_personal_information_and_synthetic_email(self):
        personal = "person" + "@" + "private-domain.test"
        self.assertIn("疑似个人邮箱", scan_text(personal))
        self.assertEqual(scan_text("demo@example.com"), [])
        self.assertEqual(scan_text("https://user:pass@remote.invalid/v1"), [])

    def test_png_bytes_do_not_create_text_false_positive(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"person" + b"@" + b"private-domain.test"
        categories, skipped = scan_content(raw)
        self.assertEqual(categories, [])
        self.assertIn("PNG", skipped)
        self.assertIn("人工", skipped)

    def test_text_secret_still_detected_and_not_returned(self):
        value = "sk-" + "Z" * 30
        categories, skipped = scan_content(value.encode())
        self.assertIn("疑似服务密钥", categories)
        self.assertIsNone(skipped)
        self.assertNotIn(value, str((categories, skipped)))

    def test_bom_text_still_scanned(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                categories, skipped = scan_content(("sk-" + "Z" * 30).encode(encoding))
                self.assertIn("疑似服务密钥", categories)
                self.assertIsNone(skipped)

    def test_unknown_binary_not_claimed_as_clean_text(self):
        for raw in (b"\xff\x80", b"abc\x00def"):
            with self.subTest(raw_size=len(raw)):
                categories, skipped = scan_content(raw)
                self.assertEqual(categories, [])
                self.assertTrue(skipped.startswith("未知"))

    def test_jpeg_is_classified_by_content(self):
        categories, skipped = scan_content(b"\xff\xd8\xff" + b"person" + b"@" + b"private-domain.test")
        self.assertEqual(categories, [])
        self.assertIn("JPEG", skipped)

    def test_audit_records_working_and_history_binary_skips(self):
        # 使用项目内已有只读文件；Git 返回值和扫描内容均模拟，不创建仓库或文件。
        def fake_git(*args, **kwargs):
            if args[0] == "rev-parse":
                return str(PROJECT).encode()
            if args[0] == "ls-files":
                return b"scripts/audit_release.py\0"
            if args[:2] == ("rev-list", "--objects"):
                return b"abc image.png\n"
            if args[:2] == ("cat-file", "-t"):
                return b"blob"
            if args[:2] == ("cat-file", "-s"):
                return b"12"
            if args[:2] == ("cat-file", "-p"):
                return b"mock-binary"
            if args[0] == "rev-list":
                return b""
            raise AssertionError("不应执行其他Git操作")
        with patch("scripts.audit_release.git", side_effect=fake_git), \
                patch("scripts.audit_release.scan_content", return_value=([], "PNG二进制需人工检查")):
            report = audit()
        self.assertEqual({item["scope"] for item in report["skipped_binary"]}, {"working_tree", "history"})
        self.assertEqual(report["findings"], [])
        with patch("scripts.audit_release.git", side_effect=fake_git), \
                patch("scripts.audit_release.scan_content", return_value=([], "未知二进制需人工检查")):
            report = audit()
        self.assertEqual(report["status"], "STOP_REVIEW_REQUIRED")
        self.assertEqual(len(report["incomplete"]), 2)


if __name__ == "__main__":
    unittest.main()
