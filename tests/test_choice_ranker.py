"""Synthetic-only tests for batched Choice ranking."""

import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import choice_ranker
from jev_ranker import RankError

MODEL = "jev-1.13.0"


def fake_response(body, tokens=12):
    ids = list(body["questions"]["q0"]["criteria"])
    weights = {identifier: int(body["state"]["threads"][identifier]["rank"]) + 1 for identifier in ids}
    total = sum(weights.values())
    probabilities = {identifier: weights[identifier] / total for identifier in ids}
    chosen = max(ids, key=lambda identifier: (probabilities[identifier], identifier))
    return {"model": body["model"], "answers": {"q0": {"type": "choice", "choice": chosen,
            "probabilities": probabilities, "confidence": 0.7}},
            "usage": {"input_tokens": tokens, "output_tokens": 3}}


class ChoiceRankerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.rows = [{"id": str(i), "relevance_score": 0.1} for i in range(9)]
        self.evidence = {str(i): {"rank": i, "text": "synthetic " + str(i)} for i in range(9)}

    def test_all_candidates_top_five_and_warm_cache(self):
        seen = set()
        def post(body, key):
            self.assertEqual(key, "fake")
            self.assertEqual(body["state"]["query"], "find")
            ids = set(body["state"]["threads"])
            self.assertEqual(ids, set(body["questions"]["q0"]["criteria"]))
            self.assertTrue(all(body["questions"]["q0"]["criteria"][identifier] is None for identifier in ids))
            self.assertEqual(body["questions"]["q0"]["type"], "choice")
            self.assertLessEqual(len(choice_ranker._encoded(body)), choice_ranker.MAX_REQUEST_BYTES)
            seen.update(ids)
            return fake_response(body), 1
        ranked, stats = choice_ranker.rank_by_choice("find", self.rows, self.evidence, self.cache, "fake", MODEL, 5, 3, False, post)
        self.assertEqual([row["id"] for row in ranked], ["8", "7", "6", "5", "4"])
        self.assertEqual(seen, set(self.evidence))
        self.assertGreater(stats["choice_api_calls"], 0)
        warm, warm_stats = choice_ranker.rank_by_choice("find", self.rows, self.evidence, self.cache, None, MODEL, 5, 3, False, post)
        self.assertEqual(warm, ranked)
        self.assertEqual(warm_stats["choice_api_calls"], 0)
        self.assertGreater(warm_stats["choice_cache_hits"], 0)

    def test_changed_evidence_invalidates_cache(self):
        calls = []
        def post(body, key):
            calls.append(body)
            return fake_response(body), 1
        choice_ranker.rank_by_choice("find", self.rows, self.evidence, self.cache, "fake", MODEL, 5, 2, False, post)
        self.assertTrue(calls)
        calls.clear()
        self.evidence["0"] = {"rank": 0, "text": "changed synthetic"}
        ranked, stats = choice_ranker.rank_by_choice("find", self.rows, self.evidence, self.cache, "fake", MODEL, 5, 2, False, post)
        self.assertEqual([row["id"] for row in ranked], ["8", "7", "6", "5", "4"])
        self.assertGreater(stats["choice_api_calls"], 0)
        self.assertTrue(calls)

    def test_body_validation_and_size_failure_are_safe(self):
        body = choice_ranker._body("q", MODEL, ["0", "1"], self.evidence)
        self.assertEqual(set(body["state"]["threads"]), {"0", "1"})
        self.assertEqual(set(body["questions"]["q0"]["criteria"]), {"0", "1"})
        probabilities, tokens = choice_ranker._validate(fake_response(body), ["0", "1"], MODEL)
        self.assertEqual(set(probabilities), {"0", "1"})
        self.assertEqual(tokens, 12)
        bad = fake_response(body)
        bad["answers"]["q0"]["probabilities"]["0"] = float("nan")
        with self.assertRaises(RankError):
            choice_ranker._validate(bad, ["0", "1"], MODEL)
        huge = {"0": {"rank": 0, "text": "secret" * 10000}, "1": {"rank": 1, "text": "other" * 10000}}
        with self.assertRaises(RankError) as raised:
            choice_ranker.rank_by_choice("q", self.rows[:2], huge, self.cache, "fake", MODEL, 2, 1, False,
                                         lambda request, key: (fake_response(request), 1))
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
