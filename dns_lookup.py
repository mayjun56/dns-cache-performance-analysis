"""DNS A-record lookup with input validation, TTL retrieval, and clear errors.

Requires Python >= 3.10 and dnspython >= 2.4.
The resolver's built-in application cache is explicitly disabled so the
project can compare its own cache against uncached resolver calls.
"""

from __future__ import annotations

import argparse
import ipaddress
import time
from dataclasses import dataclass

import dns.exception
import dns.resolver


class DNSLookupError(Exception):
    """A predictable, human-readable failure during a DNS lookup."""


@dataclass(frozen=True)
class DNSResult:
    domain: str
    addresses: tuple[str, ...]
    ttl: int
    elapsed_ms: float


def normalize_domain(domain: str) -> str:
    """Validate a hostname and return a lowercase ASCII IDNA cache key."""
    if not isinstance(domain, str):
        raise DNSLookupError("Domain must be a string.")
    name = domain.strip().rstrip(".")
    if not name or len(name) > 253:
        raise DNSLookupError("Domain must contain 1 to 253 characters.")
    try:
        ascii_name = name.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DNSLookupError("Domain contains invalid international characters.") from exc
    if len(ascii_name) > 253:
        raise DNSLookupError("Encoded domain is too long.")
    for label in ascii_name.split("."):
        if not 1 <= len(label) <= 63:
            raise DNSLookupError("Each domain label must have 1 to 63 characters.")
        if label[0] == "-" or label[-1] == "-":
            raise DNSLookupError("Domain labels cannot start or end with '-'.")
        if not all(c.isascii() and (c.isalnum() or c == "-") for c in label):
            raise DNSLookupError("Domain contains an invalid character.")
    return ascii_name


def query_dns(domain: str, *, timeout: float = 3.0,
              resolver: dns.resolver.Resolver | None = None) -> DNSResult:
    """Query IPv4 A records and extract the DNS response TTL.

    `elapsed_ms` covers the resolver operation only, not input validation.
    This uses a stub resolver; an upstream recursive resolver/OS may cache.
    """
    normalized = normalize_domain(domain)
    if not 0 < timeout <= 60:
        raise ValueError("timeout must be greater than 0 and at most 60 seconds")
    if resolver is None:
        resolver = dns.resolver.Resolver(configure=True)
    resolver.cache = None
    start = time.perf_counter()
    try:
        answer = resolver.resolve(normalized, "A", lifetime=timeout, search=False)
        if answer.rrset is None:
            raise DNSLookupError(f"No IPv4 (A) records found for {normalized}.")
        addresses = tuple(sorted({str(ipaddress.IPv4Address(item.address))
                                  for item in answer}))
        if not addresses:
            raise DNSLookupError(f"No IPv4 (A) records found for {normalized}.")
        ttl = max(0, int(answer.rrset.ttl))
    except dns.resolver.NXDOMAIN as exc:
        raise DNSLookupError(f"Domain does not exist: {normalized}") from exc
    except dns.resolver.NoAnswer as exc:
        raise DNSLookupError(f"No IPv4 (A) records found for {normalized}.") from exc
    except dns.resolver.NoNameservers as exc:
        raise DNSLookupError(f"No available DNS nameservers for {normalized}.") from exc
    except dns.exception.Timeout as exc:
        raise DNSLookupError(f"DNS lookup timed out for {normalized}.") from exc
    except dns.exception.DNSException as exc:
        raise DNSLookupError(f"DNS lookup failed for {normalized}: {exc}") from exc
    elapsed_ms = (time.perf_counter() - start) * 1000
    return DNSResult(normalized, addresses, ttl, elapsed_ms)


def lookup_domain(domain: str, *, timeout: float = 3.0) -> list[str]:
    """Simple teammate-facing API: return IPv4 address strings."""
    return list(query_dns(domain, timeout=timeout).addresses)


def main() -> None:
    parser = argparse.ArgumentParser(description="Uncached DNS IPv4 A-record lookup")
    parser.add_argument("domain", help="domain, for example example.com")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    try:
        result = query_dns(args.domain, timeout=args.timeout)
    except (DNSLookupError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(f"Domain: {result.domain}")
    print(f"IPv4: {', '.join(result.addresses)}")
    print(f"DNS TTL: {result.ttl} seconds")
    print(f"Lookup time: {result.elapsed_ms:.3f} ms")


if __name__ == "__main__":
    main()
