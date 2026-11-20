import copy
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

import book_translator as bt


class BookTranslatorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(bt.load_config(Path(__file__).with_name("config.yaml")))

    def memory_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        bt.init_schema(conn)
        return conn

    def test_gutenberg_wrapper_requires_both_markers(self):
        source = "header\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\nBOOK\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\nlicense"
        text, notes = bt.preprocess_text(source, self.cfg)
        self.assertEqual(text, "BOOK")
        self.assertTrue(notes)

        incomplete, notes = bt.preprocess_text(
            "header\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\nBOOK", self.cfg
        )
        self.assertIn("header", incomplete)
        self.assertFalse(notes)

    def test_heading_starts_a_new_chunk(self):
        self.cfg["chunking"].update(target_tokens=80, hard_max_tokens=100, chars_per_token=3)
        source = ("Preface prose. " * 18) + "\n\nCHAPTER 1. Start.\n\n" + ("Chapter prose. " * 18)
        chunks = bt.build_chunks(source, self.cfg)
        self.assertEqual(
            len([c for c in chunks if c.source_text.startswith("CHAPTER 1. Start.")]), 1
        )

    def test_validation_is_limited_to_empty_and_gross_length(self):
        self.assertTrue(bt.validate_translation("source", "", self.cfg))
        self.assertTrue(bt.validate_translation("x" * 1000, "коротко", self.cfg))
        self.assertEqual(bt.validate_translation("English 1851", "Русский текст", self.cfg), [])

    def test_chunks_keep_prepared_paragraphs_and_prefer_sentence_boundaries(self):
        self.cfg["chunking"].update(target_tokens=80, hard_max_tokens=100, chars_per_token=3)
        sentence = "The sailor waited in the darkness beside the old stone pier. " * 3
        paragraph = sentence + "She followed the road towards the village in the moonlight. " * 4
        source = "CHAPTER 1\n\n" + paragraph.rstrip() + "\n\n\n    A line of verse.\n    A second line."
        chunks = bt.build_chunks(source, self.cfg)
        self.assertEqual("".join(c.source_text + c.joiner_after for c in chunks), source)
        self.assertTrue(all(len(c.source_text) <= 300 for c in chunks))
        first_prose = next(c.source_text for c in chunks if c.source_text.startswith("The sailor"))
        self.assertTrue(first_prose.endswith("."))

    def test_translategemma_uses_structured_language_request(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "Перевод"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }

        class Session:
            def post(self, _url, json, timeout):
                self.payload = json
                return Response()

        client = bt.LMStudioClient(self.cfg, logging.getLogger("test"))
        client.session = Session()
        result = client.translate("translategemma-12b-it", "Source text")
        item = client.session.payload["messages"][0]["content"][0]
        self.assertEqual(result, ("Перевод", 5, 2))
        self.assertEqual(item["source_lang_code"], "en")
        self.assertEqual(item["target_lang_code"], "ru")
        self.assertEqual(item["text"], "Source text")
        self.assertEqual(client.session.payload["temperature"], 0.0)
        self.assertEqual(len(client.session.payload["messages"]), 1)

    def test_truncated_response_is_non_retryable(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "Обрезано"}, "finish_reason": "length"}]
                }

        class Session:
            def post(self, _url, json, timeout):
                return Response()

        client = bt.LMStudioClient(self.cfg, logging.getLogger("test"))
        client.session = Session()
        with self.assertRaises(bt.NonRetryableTranslationError):
            client.translate("translategemma-12b-it", "Source")

    def test_failed_chunk_blocks_final_assembly(self):
        conn = self.memory_db()
        conn.execute("INSERT INTO chunks(position, source_text, status) VALUES(1, 'source', 'failed')")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                bt.write_final(conn, Path(td) / "final.txt")

    def test_retry_preserves_previous_result_and_attempt_count(self):
        conn = self.memory_db()
        conn.execute(
            """INSERT INTO chunks(
                position, source_text, translated_text, status, attempts, model,
                prompt_tokens, completion_tokens, duration_seconds, translated_at
            ) VALUES(1, 'source', 'old', 'failed', 3, 'old-model', 10, 20, 2.0, CURRENT_TIMESTAMP)"""
        )
        self.assertEqual(bt.reset_for_retry(conn), 1)
        row = conn.execute("SELECT * FROM chunks WHERE position=1").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["translated_text"], 'old')
        self.assertEqual(row["model"], 'old-model')
        self.assertEqual(row['attempts'], 3)

    def test_profile_change_is_blocked_after_work_started(self):
        conn = self.memory_db()
        conn.execute("INSERT INTO chunks(position, source_text) VALUES(1, 'source')")
        bt.ensure_translation_profile(conn, self.cfg, "model-a")
        conn.execute("UPDATE chunks SET status='done', attempts=1 WHERE position=1")
        conn.commit()
        with self.assertRaises(RuntimeError):
            bt.ensure_translation_profile(conn, self.cfg, "model-b")

    def test_non_retryable_failure_is_not_repeated(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def translate(self, *_args):
                self.calls += 1
                raise bt.NonRetryableTranslationError("truncated")

        conn = self.memory_db()
        conn.execute("INSERT INTO chunks(position, source_text) VALUES(1, ?)", ("source " * 50,))
        row = conn.execute("SELECT * FROM chunks WHERE position=1").fetchone()
        client = Client()
        status, _ = bt.translate_one_chunk(
            conn, row, client, "model", self.cfg, logging.getLogger("test")
        )
        self.assertEqual(status, "failed")
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
