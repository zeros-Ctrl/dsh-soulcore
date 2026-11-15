"""Accounting tests with supplied HTTP responses; no sockets or paid calls."""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from soulcore.embedding import CachedEmbedder, HTTPEmbedder
from soulcore.llm import DeepSeekLLM, LLMError, MockLLM
from soulcore.usage import UsageLedger, current_purpose, normalize_usage, usage_scope


def response(usage=None, text="ok"):
    data = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        data["usage"] = usage
    return data


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class UsageLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(self.tmp.name, {"prices": {
            "deepseek:chat": {"currency": "CNY", "input_per_million": 3,
                              "cached_input_per_million": 1, "output_per_million": 10}}}, clock=lambda: 100)
        self.llm = DeepSeekLLM({"provider": "deepseek", "api_key": "fake-secret-only", "model": "chat", "extract_model": "small"})
        self.llm.bind_usage(self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_deepseek_cache_and_reasoning_are_counted_once(self):
        usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600,
                 "prompt_cache_miss_tokens": 400, "completion_tokens": 200,
                 "completion_tokens_details": {"reasoning_tokens": 120}}
        with patch.object(self.llm, "_req", return_value=response(usage)):
            with usage_scope("online_reply"):
                self.assertEqual(self.llm.chat([{"role": "user", "content": "private prompt"}]), "ok")
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["input_tokens"], 1000)
        self.assertEqual(result["totals"]["cached_input_tokens"], 600)
        self.assertEqual(result["totals"]["uncached_input_tokens"], 400)
        self.assertEqual(result["totals"]["output_tokens"], 200)
        self.assertEqual(result["totals"]["reasoning_tokens"], 120)
        self.assertEqual(result["totals"]["billable_output_tokens"], 200)
        self.assertAlmostEqual(result["known_cost_by_currency"]["CNY"], 0.0038)
        self.assertAlmostEqual(result["by_purpose"]["online_reply"]["known_cost_by_currency"]["CNY"], 0.0038)
        self.assertAlmostEqual(result["by_model"]["deepseek:chat"]["known_cost_by_currency"]["CNY"], 0.0038)
        self.assertTrue(result["cost_complete"])

    def test_openai_input_output_aliases_and_nested_cache(self):
        normalized = normalize_usage({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 25},
                                      "output_tokens": 60, "output_tokens_details": {"reasoning_tokens": 40}})
        self.assertEqual(normalized["uncached_input_tokens"], 75)
        self.assertEqual(normalized["billable_output_tokens"], 60)
        self.assertEqual(normalized["usage_status"], "reported")

    def test_missing_usage_remains_unknown(self):
        with patch.object(self.llm, "_req", return_value=response()):
            self.llm.chat([{"role": "user", "content": "abc"}])
        totals = self.ledger.snapshot()["totals"]
        self.assertIsNone(totals["input_tokens"])
        self.assertIsNone(totals["output_tokens"])
        self.assertEqual(totals["missing_usage_calls"], 1)
        self.assertFalse(self.ledger.snapshot()["cost_complete"])

    def test_missing_price_does_not_claim_free_usage(self):
        with patch.object(self.llm, "_req", return_value=response({"prompt_tokens": 20, "completion_tokens": 30}, '{"a":1}')):
            with usage_scope("extract"):
                self.assertEqual(self.llm.chat_json([]), {"a": 1})
        result = self.ledger.snapshot()
        self.assertEqual(result["by_purpose"]["extract"]["calls"], 1)
        self.assertEqual(result["by_model"]["deepseek:small"]["incompletely_priced_calls"], 1)
        self.assertEqual(result["totals"]["unconfigured_price_calls"], 1)
        self.assertFalse(result["cost_complete"])

    def test_failed_attempt_and_retry_are_both_recorded(self):
        good = response({"prompt_tokens": 10, "prompt_cache_hit_tokens": 0, "completion_tokens": 5})
        with patch.object(self.llm, "_req", side_effect=[LLMError("sensitive remote detail", 429), good]):
            with self.assertRaises(LLMError):
                self.llm.chat([])
            self.assertEqual(self.llm.chat([]), "ok")
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["calls"], 2)
        self.assertEqual(result["totals"]["failed_calls"], 1)
        self.assertEqual(result["totals"]["successful_calls"], 1)
        failed = self.ledger.db.execute("SELECT * FROM requests WHERE success=0").fetchone()
        self.assertEqual(failed["status_code"], 429)
        self.assertEqual(failed["error_type"], "LLMError")
        self.assertFalse(result["cost_complete"])

    def test_successful_usage_survives_reopen(self):
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 8, "prompt_cache_hit_tokens": 0, "completion_tokens": 2})
        self.ledger.close()
        self.ledger = UsageLedger(self.tmp.name)
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["calls"], 1)
        self.assertTrue(result["cost_complete"])
        self.assertIn("CNY", result["known_cost_by_currency"])
        self.assertEqual(self.ledger.snapshot(since=101)["totals"]["calls"], 0)

    def test_no_prompt_key_response_or_remote_error_in_database(self):
        with patch.object(self.llm, "_req", return_value=response({"prompt_tokens": 9, "completion_tokens": 4}, "private reply")):
            self.llm.chat([{"role": "user", "content": "private prompt"}])
        dump = "\n".join(self.ledger.db.iterdump())
        for secret in ("fake-secret-only", "private prompt", "private reply", "sensitive remote detail"):
            self.assertNotIn(secret, dump)

    def test_cache_differential_requires_reported_split(self):
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 100, "completion_tokens": 10})
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["unknown_cache_split_calls"], 1)
        self.assertFalse(result["cost_complete"])
        self.assertAlmostEqual(result["known_cost_by_currency"]["CNY"], 0.0001)  # only output is priced

    def test_reasoning_can_have_a_different_rate_without_double_counting(self):
        self.ledger.cfg["prices"]["deepseek:chat"]["reasoning_per_million"] = 5
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 0, "completion_tokens": 200,
                                                                    "completion_tokens_details": {"reasoning_tokens": 120}})
        self.assertAlmostEqual(self.ledger.snapshot()["known_cost_by_currency"]["CNY"], 0.0014)

    def test_explicit_separate_reasoning_billing(self):
        price = self.ledger.cfg["prices"]["deepseek:chat"]
        price["reasoning_in_output"] = False
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 0, "completion_tokens": 200,
                                                                    "reasoning_tokens": 120})
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["billable_output_tokens"], 320)
        self.assertAlmostEqual(result["known_cost_by_currency"]["CNY"], 0.0032)

    def test_invalid_provider_split_is_flagged_and_not_priced(self):
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 10, "prompt_cache_hit_tokens": 20,
                                                                    "completion_tokens": 3})
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["invalid_usage_calls"], 1)
        self.assertFalse(result["cost_complete"])
        self.assertEqual(result["known_cost_by_currency"]["CNY"], 0)

    def test_thread_scopes_do_not_cross_and_concurrent_writes_are_durable(self):
        def write(i):
            with usage_scope(f"worker-{i % 3}"):
                self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 0, "completion_tokens": 0})
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(60)))
        result = self.ledger.snapshot()
        self.assertEqual(result["totals"]["calls"], 60)
        self.assertEqual({key: value["calls"] for key, value in result["by_purpose"].items()},
                         {"worker-0": 20, "worker-1": 20, "worker-2": 20})
        self.assertEqual(current_purpose(), "online_reply")

    def test_mock_does_not_create_billable_requests(self):
        mock = MockLLM().bind_usage(self.ledger)
        mock.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(self.ledger.snapshot()["totals"]["calls"], 0)

    def test_accounting_failure_does_not_drop_reply(self):
        class BrokenLedger:
            recording_errors = 0
            def record(self, **kwargs):
                raise OSError("secret data in diagnostics")
        broken = BrokenLedger()
        self.llm.bind_usage(broken)
        with patch.object(self.llm, "_req", return_value=response()), self.assertLogs("soulcore.usage", level="WARNING") as logs:
            self.assertEqual(self.llm.chat([]), "ok")
        self.assertEqual(broken.recording_errors, 1)
        self.assertNotIn("secret data", "\n".join(logs.output))

    def test_disabled_ledger_creates_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(tmp, {"enabled": False})
            ledger.record(provider="x", model="y")
            self.assertEqual(ledger.snapshot()["totals"]["calls"], 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "usage.sqlite3")))

    def test_embedding_http_usage_and_cache_only_count_actual_requests(self):
        self.ledger.cfg["prices"]["http:bge"] = {"currency": "USD", "input_per_million": 0.5}
        embed = CachedEmbedder(HTTPEmbedder({"provider": "http", "base_url": "https://example.invalid/v1",
                                           "api_key": "fake-embed-secret", "model": "bge"}), lambda: 0).bind_usage(self.ledger)
        payload = {"data": [{"index": 0, "embedding": [1.0, 0.0]}], "usage": {"total_tokens": 3}}
        with patch("soulcore.embedding.urllib.request.urlopen", return_value=FakeResponse(payload)) as request:
            with usage_scope("retrieval"):
                self.assertEqual(embed.embed(["private embedding text"]), [[1.0, 0.0]])
                self.assertEqual(embed.embed(["private embedding text"]), [[1.0, 0.0]])
        self.assertEqual(request.call_count, 1)
        result = self.ledger.snapshot()
        self.assertEqual(result["by_purpose"]["retrieval"]["calls"], 1)
        self.assertEqual(result["totals"]["input_tokens"], 3)
        self.assertEqual(result["totals"]["output_tokens"], 0)
        self.assertAlmostEqual(result["known_cost_by_currency"]["USD"], 0.0000015)
        self.assertTrue(result["cost_complete"])
        self.assertNotIn("private embedding text", "\n".join(self.ledger.db.iterdump()))

    def test_different_currencies_are_never_added_into_one_total(self):
        self.ledger.cfg["prices"]["other:chat"] = {"currency": "USD", "input_per_million": 1, "output_per_million": 1}
        self.ledger.record(provider="deepseek", model="chat", usage={"prompt_tokens": 0, "completion_tokens": 1})
        self.ledger.record(provider="other", model="chat", usage={"prompt_tokens": 0, "completion_tokens": 1})
        costs = self.ledger.snapshot()["known_cost_by_currency"]
        self.assertEqual(set(costs), {"CNY", "USD"})
        self.assertAlmostEqual(costs["CNY"], 0.00001)
        self.assertAlmostEqual(costs["USD"], 0.000001)


if __name__ == "__main__":
    unittest.main()
