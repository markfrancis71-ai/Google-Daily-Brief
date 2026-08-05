import os
import unittest
from unittest.mock import patch

import generate_brief


class GeminiConfigTests(unittest.TestCase):
    def test_prefers_gemini_api_key_when_present(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"}, clear=True):
            self.assertEqual(generate_brief.get_gemini_api_key(), "gemini-key")

    def test_falls_back_to_google_api_key(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "google-key"}, clear=True):
            self.assertEqual(generate_brief.get_gemini_api_key(), "google-key")

    def test_uses_supported_default_model(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(generate_brief.get_gemini_model(), "gemini-3.5-flash")


if __name__ == "__main__":
    unittest.main()
