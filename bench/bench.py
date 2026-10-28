"""Time dimsheet on the three examples.

    python bench/bench.py        # measure + draw (opens Chrome once per example), then draw-only from the saved JSON
"""
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dimsheet  # noqa: E402

for name in ("card", "player", "dialog"):
    with tempfile.TemporaryDirectory() as d:
        runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            subprocess.run([sys.executable, str(ROOT / "dimsheet.py"), str(ROOT / "examples" / f"{name}.html"), "-o", d],
                           check=True, capture_output=True)
            runs.append((time.perf_counter() - t0) * 1000)
    tree = json.loads((ROOT / "tests" / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    draws = []
    for _ in range(20):
        t0 = time.perf_counter()
        sh = dimsheet.draw(tree, name.title(), f"{name}.html", "2026-10-28")
        sh.svg(), sh.svg(motion=True)
        draws.append((time.perf_counter() - t0) * 1000)
    print(f"{name:7s} measure + draw (CLI, median of 3): {statistics.median(runs):7.0f} ms   "
          f"draw both SVGs from JSON (median of 20): {statistics.median(draws):5.1f} ms")
