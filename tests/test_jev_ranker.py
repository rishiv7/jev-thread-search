import importlib.util
import io
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.error import URLError


MODULE = Path(__file__).resolve().parents[1] / "plugins" / "jev-thread-search" / "skills" / "jev-thread-search" / "scripts" / "jev_ranker.py"
sys.path.insert(0, str(MODULE.parent))
import jev_ranker as ranker


def candidate(identifier, text):
    return {"id": identifier, "title": identifier, "profile": "synthetic", "source_file": "fixture",
            "created_at": "2020", "last_activity": "2020", "initial_model": "fixture",
            "models": [], "content_hash": identifier, "parts": [{"text": text}]}


def response(body, values=None, tokens=10):
    names = list(body["questions"])
    values = values or [0.5] * len(names)
    answers = {}
    for i, name in enumerate(names):
        question = body["questions"][name]
        if question["type"] == "noul":
            answers[name] = {"type": "noul", "noul": values[i]}
        else:
            threads = body["state"]["threads"]
            ids = list(question["criteria"])
            weights = {identifier: max(1, ord(str(threads[identifier].get("title", "0"))[0])) for identifier in ids}
            total = sum(weights.values())
            probabilities = {identifier: weights[identifier] / total for identifier in ids}
            chosen = max(ids, key=lambda identifier: (probabilities[identifier], identifier))
            answers[name] = {"type": "choice", "choice": chosen,
                             "probabilities": probabilities, "confidence": 0.8}
    return {"model": body["model"], "answers": answers,
            "usage": {"input_tokens": tokens, "output_tokens": 2}}


class RankerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def test_top_five_global_ranking_and_cache(self):
        seen = []
        def post(body, key):
            seen.append(body)
            self.assertEqual(body["state"]["query"], "find")
            self.assertEqual(body["model"], "jev-1.13.0")
            self.assertEqual(key, "fake")
            vals = ([int(question["instructions"]["thread"]["excerpt"]["text"]) / 10 for question in body["questions"].values()]
                    if next(iter(body["questions"].values()))["type"] == "noul" else None)
            return response(body, vals), 1
        candidates = [candidate(str(i), str(i)) for i in range(7)]
        with patch.object(ranker, "_post", side_effect=post):
            ranked, stats = ranker.rank_threads("find", candidates, self.cache, "fake")
            cached, cached_stats = ranker.rank_threads("find", candidates, self.cache, None)
        self.assertEqual([row["id"] for row in ranked], ["6", "5", "4", "3", "2"])
        self.assertEqual(cached, ranked)
        self.assertGreaterEqual(stats["questions"], 7)
        self.assertEqual(cached_stats["cache_hits"], 7)
        self.assertEqual(cached_stats["api_calls"], 0)
        self.assertGreaterEqual(len(seen), 1)

    def test_cache_invalidation_is_per_question(self):
        calls = []
        def post(body, key):
            calls.append(len(body["questions"]))
            return response(body), 1
        rows = [candidate("a", "one"), candidate("b", "two")]
        with patch.object(ranker, "_post", side_effect=post):
            ranker.rank_threads("q", rows, self.cache, "fake")
            rows[1] = candidate("b", "changed")
            _, stats = ranker.rank_threads("q", rows, self.cache, "fake")
            self.assertEqual(stats["cache_hits"], 1)
            self.assertIn(1, calls)
            self.assertEqual(ranker.rank_threads("other", rows, self.cache, "fake")[1]["cache_hits"], 0)
            self.assertEqual(ranker.rank_threads("q", rows, self.cache, "fake", model="jev-other")[1]["cache_hits"], 0)

    def test_byte_limits_and_oversize_split(self):
        seen = []
        def post(body, key):
            seen.append(body)
            self.assertLessEqual(len(ranker._encoded(body)), ranker.MAX_REQUEST_BYTES)
            for question in body["questions"].values():
                self.assertLessEqual(len(ranker._encoded({"state": body["state"], "question": question})), ranker.MAX_SINGLE_BYTES)
            return response(body), 1
        with patch.object(ranker, "_post", side_effect=post):
            _, stats = ranker.rank_threads("q", [candidate("a", "z" * 45000)], self.cache, "fake")
        self.assertGreater(stats["questions"], 1)
        self.assertTrue(seen)
        with self.assertRaises(ranker.RankError) as raised:
            ranker.rank_threads("q" * 30000, [candidate("a", "x")], self.cache, "fake")
        self.assertEqual(raised.exception.code, "invalid_input")

    def test_context_error_splits_batch(self):
        lengths = []
        def post(body, key):
            count = len(body["questions"])
            lengths.append(count)
            if count > 1:
                raise ranker.RankError("context_or_validation_error")
            return response(body), 1
        with patch.object(ranker, "_post", side_effect=post):
            _, stats = ranker.rank_threads("q", [candidate("a", "a"), candidate("b", "b")], self.cache, "fake")
        self.assertEqual(lengths, [2, 1, 1, 1])
        self.assertEqual(stats["api_calls"], 4)

    def test_retry_and_safe_failure(self):
        body = ranker._body("q", {"q0": ranker._question({"text": "safe"})}, "jev-1.13.0")
        error = HTTPError(ranker.URL, 429, "echo secret", {"Retry-After": "0"}, None)
        with patch.object(ranker, "urlopen", side_effect=[error, error, error]), patch.object(ranker.time, "sleep") as sleep:
            with self.assertRaises(ranker.RankError) as raised:
                ranker._post(body, "secret")
        self.assertEqual(raised.exception.code, "provider_http_error")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(sleep.call_count, 2)

    def test_invalid_response_and_partial_failure_not_cached(self):
        body = ranker._body("q", {"q0": ranker._question("x")}, "jev-1.13.0")
        for malformed in [response(body, [float("nan")]), {**response(body), "model": "jev-latest"},
                          {**response(body), "usage": {"input_tokens": 0, "output_tokens": 2}}]:
            with self.assertRaises(ranker.RankError):
                ranker._validate(malformed, {"q0"}, "jev-1.13.0")
        def fail_one(body, key):
            if any(q["type"] == "noul" and q["instructions"]["thread"]["excerpt"]["text"] == "fail" for q in body["questions"].values()):
                raise ranker.RankError("network_error")
            return response(body), 1
        with patch.object(ranker, "_post", side_effect=fail_one), patch.object(ranker, "MAX_REQUEST_BYTES", 350):
            with self.assertRaises(ranker.RankError):
                ranker.rank_threads("q", [candidate("a", "okay"), candidate("b", "fail")], self.cache, "fake")
        with patch.object(ranker, "_post", side_effect=lambda body, key: (response(body), 1)):
            _, stats = ranker.rank_threads("q", [candidate("a", "okay"), candidate("b", "fail")], self.cache, "fake")
        self.assertEqual(stats["cache_hits"], 0)

    def test_validation_error_does_not_split_and_auth_is_safe(self):
        body = ranker._body("q", {"q0": ranker._question("x")}, "jev-1.13.0")
        unknown = HTTPError(ranker.URL, 422, "secret", {}, io.BytesIO(b'{"detail":"bad instructions echoed secret"}'))
        with patch.object(ranker, "urlopen", side_effect=unknown):
            with self.assertRaises(ranker.RankError) as raised:
                ranker._post(body, "key-secret")
        self.assertEqual(raised.exception.code, "provider_validation_error")
        self.assertNotIn("secret", str(raised.exception))
        auth = HTTPError(ranker.URL, 401, "key-secret", {}, io.BytesIO(b"key-secret"))
        with patch.object(ranker, "urlopen", side_effect=auth):
            with self.assertRaises(ranker.RankError) as raised:
                ranker._post(body, "key-secret")
        self.assertEqual(raised.exception.code, "authentication_error")
        self.assertNotIn("secret", str(raised.exception))

    def test_split_metrics_count_each_attempt_and_max_single_request(self):
        sizes = []
        def post(body, key):
            sizes.append(len(ranker._encoded(body)))
            if len(body["questions"]) > 1:
                raise ranker.RankError("context_or_validation_error")
            if next(iter(body["questions"].values()))["type"] == "choice":
                return response(body), 1
            token_count = 11 if next(iter(body["questions"].values()))["instructions"]["thread"]["excerpt"]["text"] == "a" else 17
            return response(body, tokens=token_count), 2
        with patch.object(ranker, "_post", side_effect=post):
            _, stats = ranker.rank_threads("q", [candidate("a", "a"), candidate("b", "b")], self.cache, "fake")
        self.assertEqual(stats["api_calls"], 6)
        self.assertEqual(stats["validated_tokens"], 38)
        self.assertEqual(stats["max_input_tokens"], 17)
        self.assertEqual(stats["request_bytes"], sizes[0] + 2 * sizes[1] + 2 * sizes[2] + sizes[3])

    def test_nested_split_query_budget_and_permissions(self):
        nested = candidate("a", "short")
        nested["parts"] = [{"title": "tiny", "messages": [{"content": "🌿" * 9000}]}]
        parts = ranker._parts_that_fit("q", nested["parts"][0], "jev-1.13.0")
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(part["messages"][0]["content"] for part in parts), "🌿" * 9000)
        self.assertTrue(all(part["title"] == "tiny" for part in parts))
        with patch.object(ranker, "_post", side_effect=lambda body, key: (response(body), 1)):
            ranker.rank_threads("q", [nested], self.cache, "fake")
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.cache / "jev_scores.sqlite3").st_mode & 0o777, 0o600)
        with self.assertRaises(ranker.RankError) as raised:
            ranker.rank_threads("🌿" * 1000, [candidate("b", "x")], self.cache, "fake")
        self.assertEqual(raised.exception.code, "invalid_input")

    def test_transient_network_retries(self):
        body = ranker._body("q", {"q0": ranker._question("x")}, "jev-1.13.0")
        class Response:
            def __enter__(self):
                return io.BytesIO(ranker._encoded(response(body)))
            def __exit__(self, *args):
                return False
        with patch.object(ranker, "urlopen", side_effect=[URLError("secret"), HTTPError(ranker.URL, 503, "secret", {"Retry-After": "0"}, None), Response()]), patch.object(ranker.time, "sleep") as sleep:
            result, attempts = ranker._post(body, "key-secret")
        self.assertEqual(attempts, 3)
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
