"""DNS TTL-aware cache with a small adapter API for benchmark.py.

Owned API (DNS/cache 담당):
    DNSCache().resolve(domain) -> list[str]
    DNSCache().resolve_with_metadata(domain) -> CacheResult
    DNSCache().get_stats() -> CacheStats

Integration API (benchmark 담당):
    DNSCache(ttl=5).lookup(domain) -> (first_ip_or_None, cache_hit)

The optional `ttl` is a MAXIMUM cache lifetime, not a replacement for the
DNS record's actual TTL. Effective lifetime = min(actual DNS TTL, user cap).
For reproducible offline tests, pass `lookup_fn` and `clock` explicitly.
"""

from __future__ import annotations

import argparse
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from dns_lookup import DNSLookupError, DNSResult, normalize_domain, query_dns


@dataclass(frozen=True)
class CacheResult:
    domain: str
    addresses: tuple[str, ...]
    cache_hit: bool
    elapsed_ms: float
    ttl_remaining: float


@dataclass(frozen=True)
class CacheStats:
    requests: int
    hits: int
    misses: int
    dns_queries: int
    errors: int
    expirations: int
    entries: int
    hit_rate: float


@dataclass(frozen=True)
class _CacheEntry:
    addresses: tuple[str, ...]
    expires_at: float


class DNSCache:
    """In-memory, TTL-aware DNS A-record cache.

    Args:
        lookup_fn: function(domain) -> DNSResult. Defaults to real DNS lookup.
        clock: monotonic clock returning seconds (can inject fake clock).
        ttl: optional maximum lifetime in seconds (allows fractional seconds).
            Never extends the TTL received in the DNS response. `None` means
            honor DNS TTL without an additional cap; 0 disables caching.

    A single instance serializes requests using RLock, preventing simultaneous
    duplicate lookups. Do not use that serialization as a concurrency speed test.
    """

    def __init__(
        self,
        lookup_fn: Callable[[str], DNSResult] = query_dns,
        clock: Callable[[], float] = time.monotonic,
        *,
        ttl: float | None = None,
    ) -> None:
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
                raise ValueError("ttl must be a finite, nonnegative number or None")
            if not math.isfinite(ttl) or ttl < 0:
                raise ValueError("ttl must be a finite, nonnegative number or None")
        self.ttl = None if ttl is None else float(ttl)
        self._lookup_fn = lookup_fn
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()
        self._requests = 0
        self._hits = 0
        self._misses = 0
        self._dns_queries = 0
        self._errors = 0
        self._expirations = 0

    def resolve_with_metadata(self, domain: str) -> CacheResult:
        """Resolve an A record; return addresses, hit status, time, and TTL left.

        Invalid domains raise DNSLookupError before altering statistics.
        DNS/network failures are counted as a miss and an error, never cached.
        """
        start = time.perf_counter()
        name = normalize_domain(domain)
        with self._lock:
            self._requests += 1
            entry = self._entries.get(name)
            now = self._clock()
            if entry is not None and now < entry.expires_at:
                self._hits += 1
                return CacheResult(
                    name, entry.addresses, True,
                    (time.perf_counter() - start) * 1000,
                    max(0.0, entry.expires_at - now),
                )
            if entry is not None:
                del self._entries[name]
                self._expirations += 1
            self._misses += 1
            self._dns_queries += 1
            try:
                record = self._lookup_fn(name)
                if not isinstance(record, DNSResult):
                    raise TypeError("lookup_fn must return a DNSResult")
                if (record.domain != name or not record.addresses
                    or isinstance(record.ttl, bool)
                    or not isinstance(record.ttl, (int, float))
                    or not math.isfinite(record.ttl) or record.ttl < 0):
                    raise ValueError("lookup_fn returned an invalid DNSResult")
            except Exception:
                self._errors += 1
                raise

            # Start TTL after the DNS response is received.
            # Never keep a record longer than its authoritative/recursive TTL.
            effective_ttl = float(record.ttl)
            if self.ttl is not None:
                effective_ttl = min(effective_ttl, self.ttl)
            now = self._clock()
            if effective_ttl > 0:
                self._entries[name] = _CacheEntry(record.addresses, now + effective_ttl)
            return CacheResult(
                name, record.addresses, False,
                (time.perf_counter() - start) * 1000,
                effective_ttl,
            )

    def resolve(self, domain: str) -> list[str]:
        """Simple public API: a list of IPv4 addresses, or raise on failure."""
        return list(self.resolve_with_metadata(domain).addresses)

    def lookup(self, domain: str) -> tuple[str | None, bool]:
        """benchmark.py-compatible API: (first IPv4 or None, cache hit).

        DNS failures return (None, False), matching benchmark.resolve_external.
        Low-level .resolve() and .resolve_with_metadata() instead raise errors
        so callers that need diagnostics can handle them explicitly.
        """
        try:
            result = self.resolve_with_metadata(domain)
            return result.addresses[0], result.cache_hit
        except DNSLookupError:
            return None, False

    def cleanup_expired(self) -> int:
        """Delete expired entries and return how many were removed."""
        with self._lock:
            now = self._clock()
            expired = [key for key, value in self._entries.items()
                       if now >= value.expires_at]
            for key in expired:
                del self._entries[key]
            self._expirations += len(expired)
            return len(expired)

    def clear(self, *, reset_stats: bool = False) -> None:
        """Clear entries; optionally reset hit/miss/error counters."""
        with self._lock:
            self._entries.clear()
            if reset_stats:
                self._requests = self._hits = self._misses = 0
                self._dns_queries = self._errors = self._expirations = 0

    def get_stats(self) -> CacheStats:
        """Return an immutable snapshot of cache counters."""
        with self._lock:
            hits, requests = self._hits, self._requests
            return CacheStats(
                requests, hits, self._misses, self._dns_queries,
                self._errors, self._expirations, len(self._entries),
                hits / requests if requests else 0.0,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="TTL-aware DNS IPv4 cache demo")
    parser.add_argument("domain", help="domain, for example example.com")
    parser.add_argument("--count", type=int, default=3, help="number of queries")
    parser.add_argument("--ttl", type=float, default=None,
                        help="optional maximum cache TTL in seconds")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    try:
        cache = DNSCache(ttl=args.ttl)
        for i in range(args.count):
            result = cache.resolve_with_metadata(args.domain)
            print(f"#{i + 1}: {'HIT' if result.cache_hit else 'MISS'} | "
                  f"{', '.join(result.addresses)} | {result.elapsed_ms:.3f} ms | "
                  f"TTL left: {result.ttl_remaining:.1f}s")
    except (DNSLookupError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    stats = cache.get_stats()
    print(f"Requests={stats.requests}, Hits={stats.hits}, Misses={stats.misses}, "
          f"DNS queries={stats.dns_queries}, Hit rate={stats.hit_rate:.1%}")


if __name__ == "__main__":
    main()
