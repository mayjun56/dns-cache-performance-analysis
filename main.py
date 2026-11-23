"""DNS cache performance project — midterm prototype and integration entrypoint.

Three modes:
  demo: deterministic synthetic data, no network calls
  live: real system resolver (socket.getaddrinfo), local TTL cache
  team: call the benchmark function supplied by the assigned teammate

The live resolver and cache are temporary integration fallbacks. They do not
replace dns_lookup.py or dns_cache.py supplied by the team's first member.
"""
from __future__ import annotations

import argparse
import importlib
import json
import socket
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from llm_analyzer import AnalysisError, analyze_performance, normalize_metrics


class TTLCache:
    """Minimal TTL cache for standalone integration tests only."""
    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._entries: dict[str, tuple[float, list[str]]] = {}

    def get(self, domain: str) -> list[str] | None:
        entry = self._entries.get(domain)
        if entry is None:
            return None
        expires, addresses = entry
        if self.clock() >= expires:
            del self._entries[domain]
            return None
        return addresses

    def put(self, domain: str, addresses: list[str]) -> None:
        self._entries[domain] = (self.clock() + self.ttl_seconds, addresses)


def system_resolve(domain: str) -> list[str]:
    """Resolve via OS, possibly affected by OS/recursive DNS caching."""
    answers = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    ips = sorted({entry[4][0] for entry in answers})
    if not ips:
        raise OSError(f"Resolver returned no addresses for {domain}")
    return ips


def lookup_with_cache(domain: str, cache: TTLCache,
                      resolver: Callable[[str], list[str]] | None = None) -> tuple[list[str], bool]:
    if resolver is None:
        resolver = system_resolve
    found = cache.get(domain)
    if found is not None:
        return found, True
    addresses = resolver(domain)
    cache.put(domain, addresses)
    return addresses, False


def benchmark_live(domain: str, iterations: int, ttl: float) -> dict[str, Any]:
    """Measure uncached OS resolver calls and then application-cache lookups.

    Includes the first cache miss in the cached mean. Not a controlled DNS-server
    benchmark because other caching layers and network conditions may interfere.
    """
    if iterations < 2:
        raise ValueError("iterations must be >= 2")
    raw_times: list[float] = []
    cached_times: list[float] = []
    # Application-level cache disabled in this baseline, not necessarily OS cache.
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        system_resolve(domain)
        raw_times.append((time.perf_counter_ns() - t0) / 1_000_000)
    cache = TTLCache(ttl)
    hits = misses = 0
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        _, hit = lookup_with_cache(domain, cache)
        cached_times.append((time.perf_counter_ns() - t0) / 1_000_000)
        hits += int(hit)
        misses += int(not hit)
    return {
        "domain": domain,
        "iterations": iterations,
        "uncached_avg_ms": statistics.mean(raw_times),
        "cached_avg_ms": statistics.mean(cached_times),
        "uncached_samples_ms": [round(v, 6) for v in raw_times],
        "cached_samples_ms": [round(v, 6) for v in cached_times],
        "hit_count": hits, "miss_count": misses, "dns_calls": misses,
        "cache_hit_rate": hits / iterations,
        "data_type": "measured_system_resolver",
        "notes": ("No application cache in baseline, but OS/recursive resolver may cache. "
                  "Cached mean includes cold miss; test not randomized and TTL may expire during loop."),
    }


def benchmark_demo(domain: str = "example.com") -> dict[str, Any]:
    """Illustration only. Samples are invented, NOT empirical measurements."""
    no_cache = [31.2, 28.9, 35.1, 30.8, 33.4, 29.5, 32.0, 34.0]
    with_cache = [32.4, 0.018, 0.013, 0.016, 0.015, 0.012, 0.019, 0.014]
    return {
        "domain": domain, "iterations": len(no_cache),
        "uncached_avg_ms": statistics.mean(no_cache),
        "cached_avg_ms": statistics.mean(with_cache),
        "uncached_samples_ms": no_cache, "cached_samples_ms": with_cache,
        "hit_count": 7, "miss_count": 1, "dns_calls": 1,
        "cache_hit_rate": 7 / 8,
        "data_type": "synthetic",
        "notes": "SYNTHETIC illustration for midterm development; NOT observed experimental data.",
    }


def benchmark_team(domain: str, iterations: int, ttl: float, entrypoint: str) -> dict[str, Any]:
    """Invoke teammate's benchmark adapter, requiring a documented contract.

    Entrypoint format: 'module_name:function_name'. Function must accept keyword
    arguments domain, iterations, ttl and return a dictionary with at least
    domain, uncached_avg_ms, cached_avg_ms. See README for adapter example.
    """
    if ":" not in entrypoint:
        raise ValueError("--team-benchmark must have format 'module:function'")
    mod_name, func_name = entrypoint.split(":", 1)
    if not mod_name or not func_name or func_name.startswith("_"):
        raise ValueError("Invalid team benchmark entrypoint")
    module = importlib.import_module(mod_name)
    func = getattr(module, func_name)
    if not callable(func):
        raise TypeError(f"{entrypoint} is not callable")
    result = func(domain=domain, iterations=iterations, ttl=ttl)
    if not isinstance(result, dict):
        raise ValueError("Teammate benchmark must return a dict")
    normalize_metrics(result)
    result = dict(result)
    result.setdefault("data_type", "team_supplied_unverified")
    return result


def make_markdown(metrics: dict[str, Any], analysis: str, source: str,
                  model: str | None, warning: str | None) -> str:
    normalized = normalize_metrics(metrics)
    meta = ["# DNS Cache Performance Analysis — Midterm Prototype", "",
            f"**Data type:** {normalized['data_type']}",
            f"**Analysis source:** {source}" + (f" ({model})" if model else ""),
            "**Important:** Rule-based reports are not LLM output; synthetic measurements are not real experiments."]
    if warning:
        meta.append(f"**Warning:** {warning}")
    return "\n".join(meta + ["", analysis, ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DNS cache benchmark midterm prototype")
    parser.add_argument("--mode", choices=["demo", "live", "team"], default="demo")
    parser.add_argument("--domain", default="example.com")
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--ttl", type=float, default=60.0, help="Local cache TTL in seconds")
    parser.add_argument("--llm", choices=["auto", "api", "local"], default="auto")
    parser.add_argument("--lang", choices=["ko", "zh", "en"], default="ko")
    parser.add_argument("--model", help="GLM model ID; or use GLM_MODEL")
    parser.add_argument("--team-benchmark", default="benchmark:run_benchmark",
                        help="Callable as module:function, used only with --mode team")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args(argv)
    if args.iterations < 2 or args.ttl <= 0:
        parser.error("--iterations must be >= 2 and --ttl must be > 0")
    try:
        if args.mode == "demo":
            metrics = benchmark_demo(args.domain)
        elif args.mode == "live":
            metrics = benchmark_live(args.domain, args.iterations, args.ttl)
        else:
            metrics = benchmark_team(args.domain, args.iterations, args.ttl, args.team_benchmark)
        result = analyze_performance(metrics, mode=args.llm, lang=args.lang, model=args.model)
        normalized = normalize_metrics(metrics)
        directory = Path(args.output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"metadata": {"created_utc": datetime.now(timezone.utc).isoformat(),
                                "mode": args.mode, "analysis_source": result.source,
                                "model": result.model, "warning": result.warning},
                   "metrics": metrics, "derived_metrics": normalized,
                   "analysis": result.report}
        (directory / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        (directory / "report.md").write_text(
            make_markdown(metrics, result.report, result.source, result.model, result.warning), encoding="utf-8")
        print(f"Mode: {args.mode} | Data: {normalized['data_type']} | Analysis: {result.source}")
        print(f"No app cache: {normalized['uncached_avg_ms']:.3f} ms | With app cache: {normalized['cached_avg_ms']:.3f} ms")
        if result.warning:
            print(f"NOTE: {result.warning}")
        print(f"Saved: {directory / 'results.json'} and {directory / 'report.md'}")
        return 0
    except (OSError, ValueError, TypeError, ImportError, AttributeError, AnalysisError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
