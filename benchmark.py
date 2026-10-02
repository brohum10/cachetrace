"""Seeded synthetic replay benchmark; never represents application speedup."""
import argparse
import json
from pathlib import Path
import platform
import random
import statistics
import time

from cachetrace import Access, replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=int, default=100000)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.requests < 1 or args.runs < 1:
        parser.error('requests and runs must be positive')
    rng = random.Random(2026)
    # Hot-set access, periodic scans, then a completely new hot set halfway through.
    trace = []
    for i in range(args.requests):
        phase = int(i >= args.requests // 2)
        key = f'scan:{i}' if i % 100 < 15 else f'hot:{phase}:{rng.randrange(128)}'
        trace.append(Access(key))
    records = []
    for policy in ('lru', 'lfu', 'optimal'):
        durations = []
        for _ in range(args.runs):
            start = time.perf_counter()
            result = replay(trace, 256, policy)
            durations.append((time.perf_counter() - start) * 1000)
        records.append({'policy': policy, 'capacity': 256, 'hits': result.hits,
                        'hit_rate': result.hit_rate, 'median_ms': statistics.median(durations),
                        'samples_ms': durations})
    report = {'python': platform.python_version(), 'platform': platform.platform(),
              'requests': args.requests, 'seed': 2026,
              'scope': 'Synthetic in-process replay including validation and optimal preprocessing; excludes CSV I/O. Not production performance.',
              'results': records}
    payload = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(payload + '\n', encoding='utf-8')
    print(payload)


if __name__ == '__main__':
    main()
