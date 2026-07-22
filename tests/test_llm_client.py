from __future__ import annotations

import io
import unittest
import urllib.error
from unittest.mock import patch

from brt5.llm.llm_client import LLMClient


class LLMClientTests(unittest.TestCase):
    def test_non_retryable_http_error_stops_after_one_request(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://example.invalid",
        )
        error = urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"context length exceeded"}'),
        )
        with patch(
            "brt5.llm.llm_client.urllib.request.urlopen",
            side_effect=error,
        ) as request, patch("brt5.llm.llm_client.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "LLM HTTP 400"):
                client.chat("system", "oversized prompt")

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
