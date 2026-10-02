import contextlib
import csv
import functools
import io
import itertools
import json
from pathlib import Path
import random
import tempfile
import unittest

from cachetrace import Access, LFU, Optimal, load_csv, main, replay


def trace(keys):
    return [Access(k) for k in keys]


def brute_optimal(keys, capacity):
    @functools.lru_cache(None)
    def solve(index, resident):
        if index == len(keys):
            return 0
        key = keys[index]
        if key in resident:
            return solve(index + 1, resident)
        if not capacity:
            return 1 + solve(index + 1, ())
        if len(resident) < capacity:
            return 1 + solve(index + 1, tuple(sorted((*resident, key))))
        return 1 + min(solve(index + 1, tuple(sorted((set(resident) - {v}) | {key}))) for v in resident)
    return solve(0, ())


class ReplayTests(unittest.TestCase):
    def test_known_trace(self):
        data = trace("ABACAB")
        self.assertEqual(replay(data, 2, "lru").hits, 2)
        self.assertEqual(replay(data, 2, "lfu").hits, 2)
        self.assertEqual(replay(data, 2, "optimal").hits, 2)

    def test_exhaustive_optimal_matches_independent_solver(self):
        for length in range(7):
            for keys in itertools.product("ABC", repeat=length):
                for capacity in range(4):
                    result = replay(trace(keys), capacity, "optimal")
                    self.assertEqual(result.misses, brute_optimal(keys, capacity), (keys, capacity))

    def test_random_lfu_against_simple_reference(self):
        rng = random.Random(42)
        for capacity in range(1, 8):
            engine = LFU(capacity)
            reference = {}
            for index in range(1000):
                key = str(rng.randrange(12))
                hit = key in reference
                self.assertEqual(engine.access(key, index), hit)
                if hit:
                    reference[key] = (reference[key][0] + 1, index)
                else:
                    if len(reference) == capacity:
                        del reference[min(reference, key=reference.get)]
                    reference[key] = (1, index)
                self.assertEqual(engine.frequency, {k: v[0] for k, v in reference.items()})

    def test_lfu_lru_tie_break(self):
        engine = LFU(2)
        for i, k in enumerate("ABC"):
            engine.access(k, i)
        self.assertEqual(set(engine.frequency), {"B", "C"})

    def test_scan_resistance_example(self):
        data = trace("AB" * 10 + "CDEFGH" + "AB" * 10)
        self.assertGreater(replay(data, 2, "lfu").hits, replay(data, 2, "lru").hits)

    def test_phase_change_example(self):
        data = trace("AB" * 10 + "CD" * 20)
        self.assertGreater(replay(data, 2, "lru").hits, replay(data, 2, "lfu").hits)

    def test_zero_empty_and_roomy(self):
        for policy in ("lru", "lfu", "optimal"):
            self.assertEqual(replay([], 2, policy).hit_rate, 0)
            self.assertEqual(replay(trace("AAAA"), 0, policy).misses, 4)
            result = replay(trace("ABACAB"), 100, policy)
            self.assertEqual(result.misses, result.compulsory_misses)

    def test_miss_cost(self):
        data = [Access("A", 5), Access("A", 100), Access("B", 2)]
        self.assertEqual(replay(data, 1, "lru").miss_cost, 7)

    def test_invalid_inputs(self):
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                replay([], value, "lru")
        for value in (-1, float("nan"), float("inf"), True, "3"):
            with self.assertRaises(ValueError):
                replay([Access("A", value)], 1, "lru")
        with self.assertRaises(ValueError):
            replay(trace([" "]), 1, "lru")
        with self.assertRaises(ValueError):
            replay([], 1, "unknown")

    def test_cost_overflow(self):
        with self.assertRaises(ValueError):
            replay([Access("a", 1e308), Access("b", 1e308)], 1, "lru")

    def test_optimal_heap_bounded(self):
        data = trace("A" * 10000)
        engine = Optimal(3, data)
        for i, a in enumerate(data):
            engine.access(a.key, i)
            self.assertLessEqual(len(engine.heap), 12)

    def test_lru_stack_property(self):
        rng = random.Random(8)
        data = trace([str(rng.randrange(20)) for _ in range(1000)])
        misses = [replay(data, c, "lru").misses for c in range(21)]
        self.assertEqual(misses, sorted(misses, reverse=True))


class CLITests(unittest.TestCase):
    def fixture(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "trace.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_csv_quotes_and_defaults(self):
        self.assertEqual(load_csv(self.fixture('key\n"hello,world"\n')), [Access("hello,world")])

    def test_reject_malformed_csv(self):
        for content in ('', 'x\na\n', 'key,key\na,b\n', 'key\na,b\n', 'key,miss_cost\na\n',
                        'key,miss_cost\na,nan\n', 'key,miss_cost\na,-1\n', 'key\n" "\n'):
            with self.assertRaises(ValueError):
                load_csv(self.fixture(content))

    def test_json_cli(self):
        path = self.fixture('key,miss_cost\na,2\na,3\n')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([str(path), '--capacities', '2', '1', '2', '--json']), 0)
        results = json.loads(output.getvalue())["results"]
        self.assertEqual(len(results), 6)
        self.assertEqual(results[0]["miss_cost"], 2)

    def test_unclosed_quote(self):
        with self.assertRaises(csv.Error):
            load_csv(self.fixture('key\n"unterminated\n'))

    def test_bad_cli_fails_without_report(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                main([str(self.fixture('key\na\n')), '--capacities', '-1'])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
