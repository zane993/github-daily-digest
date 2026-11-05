import unittest

import report


class InlineLinkSecurityTests(unittest.TestCase):
    def test_https_link_is_rendered_and_ampersands_are_escaped_once(self):
        rendered = report._inline("[docs](https://example.com/search?a=1&b=2)")

        self.assertIn('href="https://example.com/search?a=1&amp;b=2"', rendered)
        self.assertIn('rel="noopener noreferrer"', rendered)

    def test_unsafe_link_schemes_are_rendered_as_plain_text(self):
        for url in (
            "javascript:alert",
            "data:text/html,example",
            "file:///private/report.html",
            "//example.com/path",
        ):
            with self.subTest(url=url):
                rendered = report._inline("[click](%s)" % url)
                self.assertNotIn("<a ", rendered)
                self.assertNotIn(url, rendered)
                self.assertEqual(rendered, "click")

    def test_credentials_in_link_url_are_rejected(self):
        rendered = report._inline("[site](https://user:password@example.com)")

        self.assertNotIn("<a ", rendered)
        self.assertNotIn("password", rendered)
        self.assertEqual(rendered, "site")

    def test_link_label_is_html_escaped(self):
        rendered = report._inline("[<img src=x>](https://example.com)")

        self.assertIn("&lt;img src=x&gt;", rendered)
        self.assertNotIn("<img", rendered)


class ReportPrivacyTests(unittest.TestCase):
    def test_report_does_not_include_configured_output_path(self):
        private_path = r"C:\Users\private-user\reports"
        data = {
            "meta": {
                "window_start": "2026-11-01T00:00:00+08:00",
                "window_end": "2026-11-05T00:00:00+08:00",
                "timezone": "北京时间",
                "since_utc": "2026-10-31T16:00:00Z",
                "run_date": "2026-11-05",
                "query_time": "12:00:00",
                "days": 7,
                "rss": {},
                "created_search_count": 0,
                "official_trending_count": 0,
            },
            "records": [],
            "limitations": [],
            "budget": {
                "core_remaining": 0,
                "search_remaining": 0,
                "has_token": False,
                "reset_local": "未知",
            },
        }

        rendered = report.build_markdown(data, {"output_dir": private_path})

        self.assertNotIn(private_path, rendered)
        self.assertIn("实际写入目录由本地配置决定，不写入报告", rendered)


if __name__ == "__main__":
    unittest.main()
