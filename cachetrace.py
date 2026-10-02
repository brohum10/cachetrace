"""Deterministic, fixed-size cache trace replay. Runtime dependencies: none."""

from __future__ import annotations

import argparse
from collections import OrderedDict, defaultdict, deque
import csv
from dataclasses import asdict, dataclass
import heapq
import json
import math
from pathlib import Path
import sys


@dataclass(frozen=True)
class Access:
    key: str
    miss_cost: float = 1.0


@dataclass(frozen=True)
class Result:
    policy: str
    capacity: int
    requests: int
    hits: int
    misses: int
    hit_rate: float
    miss_cost: float
    compulsory_misses: int


class LRU:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.items: OrderedDict[str, None] = OrderedDict()

    def access(self, key: str, index: int) -> bool:
        if key in self.items:
            self.items.move_to_end(key)
            return True
        if self.capacity:
            if len(self.items) == self.capacity:
                self.items.popitem(last=False)
            self.items[key] = None
        return False


class LFU:
    """Resident-only frequency; least-recent access breaks equal-frequency ties."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.frequency: dict[str, int] = {}
        self.buckets: dict[int, OrderedDict[str, None]] = defaultdict(OrderedDict)
        self.minimum = 0

    def access(self, key: str, index: int) -> bool:
        if key in self.frequency:
            old = self.frequency[key]
            del self.buckets[old][key]
            if not self.buckets[old]:
                del self.buckets[old]
                if old == self.minimum:
                    self.minimum += 1
            self.frequency[key] = old + 1
            self.buckets[old + 1][key] = None
            return True
        if self.capacity:
            if len(self.frequency) == self.capacity:
                victim, _ = self.buckets[self.minimum].popitem(last=False)
                del self.frequency[victim]
                if not self.buckets[self.minimum]:
                    del self.buckets[self.minimum]
            self.frequency[key] = 1
            self.buckets[1][key] = None
            self.minimum = 1
        return False


class Optimal:
    """Belady's offline minimum-miss policy, with bounded lazy-heap storage."""

    def __init__(self, capacity: int, trace: list[Access]):
        self.capacity = capacity
        self.future: dict[str, deque[int]] = defaultdict(deque)
        for index, access in enumerate(trace):
            self.future[access.key].append(index)
        self.resident: dict[str, float] = {}
        self.heap: list[tuple[float, str]] = []

    def access(self, key: str, index: int) -> bool:
        self.future[key].popleft()
        following = self.future[key][0] if self.future[key] else math.inf
        hit = key in self.resident
        if not self.capacity:
            return False
        if not hit and len(self.resident) == self.capacity:
            while self.heap:
                negative_next, victim = heapq.heappop(self.heap)
                if victim in self.resident and self.resident[victim] == -negative_next:
                    del self.resident[victim]
                    break
        self.resident[key] = following
        heapq.heappush(self.heap, (-following, key))
        # Hot keys generate stale heap entries; rebuilding bounds storage by capacity.
        if len(self.heap) > 4 * max(1, self.capacity):
            self.heap = [(-next_use, item) for item, next_use in self.resident.items()]
            heapq.heapify(self.heap)
        return hit


def replay(trace: list[Access], capacity: int, policy: str) -> Result:
    if type(capacity) is not int or capacity < 0:
        raise ValueError("capacity must be a nonnegative integer")
    if policy not in ("lru", "lfu", "optimal"):
        raise ValueError("policy must be lru, lfu, or optimal")
    for access in trace:
        if not isinstance(access.key, str) or not access.key.strip():
            raise ValueError("keys must be nonblank strings")
        if isinstance(access.miss_cost, bool) or not isinstance(access.miss_cost, (int, float)):
            raise ValueError("miss costs must be finite nonnegative numbers")
        if not math.isfinite(access.miss_cost) or access.miss_cost < 0:
            raise ValueError("miss costs must be finite nonnegative numbers")
    engine = Optimal(capacity, trace) if policy == "optimal" else {"lru": LRU, "lfu": LFU}[policy](capacity)
    hits = 0
    costs = []
    for index, access in enumerate(trace):
        if engine.access(access.key, index):
            hits += 1
        else:
            costs.append(access.miss_cost)
    try:
        total_cost = math.fsum(costs)
    except OverflowError as error:
        raise ValueError("total miss cost exceeds numeric range") from error
    return Result(policy, capacity, len(trace), hits, len(trace) - hits,
                  hits / len(trace) if trace else 0.0, total_cost,
                  len({a.key for a in trace}))


def load_csv(path: Path) -> list[Access]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, strict=True)
        fields = reader.fieldnames
        if not fields or "key" not in fields or len(set(fields)) != len(fields):
            raise ValueError("CSV needs unique headers including 'key'")
        if set(fields) - {"key", "miss_cost"}:
            raise ValueError("CSV supports only 'key' and optional 'miss_cost' columns")
        result = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"malformed CSV row ending at line {reader.line_num}")
            try:
                cost = float(row.get("miss_cost", "1"))
            except ValueError as error:
                raise ValueError(f"invalid miss_cost at line {reader.line_num}") from error
            if not row["key"].strip() or not math.isfinite(cost) or cost < 0:
                raise ValueError(f"invalid key or miss_cost at line {reader.line_num}")
            result.append(Access(row["key"], cost))
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="CSV with key and optional miss_cost")
    parser.add_argument("--capacities", nargs="+", type=int, default=[16, 64, 256])
    parser.add_argument("--json", action="store_true", help="machine-readable results")
    args = parser.parse_args(argv)
    try:
        trace = load_csv(args.trace)
        results = [replay(trace, size, policy) for size in sorted(set(args.capacities))
                   for policy in ("lru", "lfu", "optimal")]
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"cachetrace: {error}\n")
    if args.json:
        print(json.dumps({"model": "cold-start, equal-size, mandatory admission; optimal minimizes misses, not cost",
                          "results": [asdict(r) for r in results]}, indent=2, allow_nan=False))
    else:
        print("Cold-start, equal-size entries; OPTIMAL knows the future (not deployable).")
        print(f"{'Policy':<9} {'Capacity':>8} {'Hits':>8} {'Misses':>8} {'Hit rate':>9} {'Miss cost':>12}")
        for r in results:
            print(f"{r.policy:<9} {r.capacity:>8} {r.hits:>8} {r.misses:>8} {r.hit_rate:>8.1%} {r.miss_cost:>12.2f}")
        print("Miss cost is user-supplied additive weight, not measured application latency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
