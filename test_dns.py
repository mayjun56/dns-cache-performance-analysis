"""Offline, deterministic unit tests: python -m unittest -v"""
import threading
import unittest
from unittest.mock import Mock, patch

import dns.exception
import dns.resolver

from dns_lookup import DNSLookupError, DNSResult, lookup_domain, normalize_domain, query_dns
from dns_cache import DNSCache


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeAnswer:
    def __init__(self, addresses=("203.0.113.8",), ttl=3):
        self.rrset = Mock(ttl=ttl)
        self.items = [Mock(address=address) for address in addresses]

    def __iter__(self):
        return iter(self.items)


class LookupTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_domain(" EXAMPLE.COM. "), "example.com")
        self.assertEqual(normalize_domain("bücher.de"), "xn--bcher-kva.de")

    def test_invalid_names(self):
        for domain in ("", "  ", "a..com", "-a.com", "a-.com", "a_b.com",
                       "a" * 64 + ".com", "a" * 254, None, 5):
            with self.subTest(domain=domain), self.assertRaises(DNSLookupError):
                normalize_domain(domain)

    def test_answer_ttl_and_deduplication(self):
        resolver = Mock()
        resolver.resolve.return_value = FakeAnswer(("203.0.113.8", "203.0.113.8", "192.0.2.1"), 15)
        result = query_dns("EXAMPLE.COM", resolver=resolver)
        self.assertEqual(result.addresses, ("192.0.2.1", "203.0.113.8"))
        self.assertEqual(result.ttl, 15)
        self.assertGreaterEqual(result.elapsed_ms, 0)
        self.assertIsNone(resolver.cache)
        resolver.resolve.assert_called_once_with("example.com", "A", lifetime=3.0, search=False)

    def test_error_types(self):
        for error in (dns.resolver.NXDOMAIN(), dns.resolver.NoAnswer(),
                      dns.resolver.NoNameservers(), dns.exception.Timeout()):
            resolver = Mock()
            resolver.resolve.side_effect = error
            with self.subTest(error=type(error)), self.assertRaises(DNSLookupError):
                query_dns("example.com", resolver=resolver)

    def test_no_rrset(self):
        resolver = Mock()
        resolver.resolve.return_value = Mock(rrset=None)
        with self.assertRaises(DNSLookupError):
            query_dns("example.com", resolver=resolver)

    def test_timeout_validation(self):
        with self.assertRaises(ValueError):
            query_dns("example.com", timeout=0)

    def test_simple_lookup_api(self):
        with patch("dns_lookup.query_dns", return_value=DNSResult("example.com", ("192.0.2.1",), 30, 1.0)):
            self.assertEqual(lookup_domain("example.com"), ["192.0.2.1"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.queries = []

        def lookup(name):
            self.queries.append(name)
            return DNSResult(name, ("192.0.2.1",), 5, 1.0)

        self.cache = DNSCache(lookup, self.clock)

    def test_hit_and_stats(self):
        first = self.cache.resolve_with_metadata("EXAMPLE.COM.")
        second = self.cache.resolve_with_metadata("example.com")
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.addresses, second.addresses)
        self.assertEqual(self.queries, ["example.com"])
        stats = self.cache.get_stats()
        self.assertEqual((stats.requests, stats.hits, stats.misses, stats.dns_queries), (2, 1, 1, 1))
        self.assertEqual(stats.hit_rate, 0.5)

    def test_ttl_boundary_and_refresh(self):
        self.cache.resolve("example.com")
        self.clock.now = 104.999
        self.assertTrue(self.cache.resolve_with_metadata("example.com").cache_hit)
        self.clock.now = 105.0
        self.assertFalse(self.cache.resolve_with_metadata("example.com").cache_hit)
        self.assertEqual(self.cache.get_stats().expirations, 1)
        self.assertEqual(len(self.queries), 2)

    def test_zero_ttl_never_cached(self):
        cache = DNSCache(lambda name: DNSResult(name, ("192.0.2.2",), 0, 0.0), self.clock)
        cache.resolve("example.com")
        cache.resolve("example.com")
        self.assertEqual((cache.get_stats().hits, cache.get_stats().dns_queries), (0, 2))
        self.assertEqual(cache.get_stats().entries, 0)

    def test_cleanup_and_clear(self):
        self.cache.resolve("example.com")
        self.clock.now += 6
        self.assertEqual(self.cache.cleanup_expired(), 1)
        self.assertEqual(self.cache.cleanup_expired(), 0)
        self.cache.clear(reset_stats=True)
        self.assertEqual(self.cache.get_stats().requests, 0)

    def test_failure_not_cached(self):
        def failing(name):
            raise DNSLookupError("simulated timeout")
        cache = DNSCache(failing, self.clock)
        for _ in range(2):
            with self.assertRaises(DNSLookupError):
                cache.resolve("example.com")
        stats = cache.get_stats()
        self.assertEqual((stats.requests, stats.misses, stats.errors, stats.entries), (2, 2, 2, 0))

    def test_invalid_domain_no_request_count(self):
        with self.assertRaises(DNSLookupError):
            self.cache.resolve("a..com")
        self.assertEqual(self.cache.get_stats().requests, 0)

    def test_independent_keys(self):
        self.cache.resolve("example.com")
        self.cache.resolve("other.example.com")
        self.assertEqual(self.cache.get_stats().entries, 2)

    def test_mutating_return_list_does_not_change_cache(self):
        result = self.cache.resolve("example.com")
        result.append("8.8.8.8")
        self.assertEqual(self.cache.resolve("example.com"), ["192.0.2.1"])

    def test_threaded_access_avoids_duplicate_queries(self):
        threads = [threading.Thread(target=self.cache.resolve, args=("example.com",)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stats = self.cache.get_stats()
        self.assertEqual((stats.requests, stats.hits, stats.dns_queries), (20, 19, 1))


if __name__ == "__main__":
    unittest.main()
