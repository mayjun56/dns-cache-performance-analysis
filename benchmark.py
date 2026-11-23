"""
benchmark.py — DNS 캐시 성능 측정 및 시각화 (담당: 이수민)

캐시 사용 전/후의 DNS 조회 시간, 캐시 적중률, 외부 DNS 조회 횟수를 측정하고
결과를 CSV / JSON / PNG 그래프로 저장한다.

사용법
    python benchmark.py               # 실제 DNS로 측정
    python benchmark.py --quick       # 빠른 동작 확인 (반복 1회, 조회 수 축소)
    python benchmark.py --offline     # 네트워크 없이 가짜 지연으로 동작 확인

3번(main.py)에서 호출
    from benchmark import run_benchmark
    summary = run_benchmark(output_dir="results")

출력
    results/raw.csv        조회 1건 = 1줄 (원본 기록)
    results/summary.json   시나리오별 요약 (LLM 분석 입력용)
    results/graphs/*.png   PPT용 그래프
"""

import argparse
import csv
import json
import os
import random
import socket
import statistics
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

TEST_DOMAINS = [
    "google.com", "youtube.com", "naver.com", "daum.net", "kakao.com",
    "github.com", "stackoverflow.com", "wikipedia.org", "amazon.com", "apple.com",
    "microsoft.com", "netflix.com", "instagram.com", "facebook.com", "x.com",
    "reddit.com", "linkedin.com", "openai.com", "anthropic.com", "cloudflare.com",
    "python.org", "pypi.org", "npmjs.com", "docker.com", "aws.amazon.com",
    "coupang.com", "11st.co.kr", "gmarket.co.kr", "korea.ac.kr", "nate.com",
    "tistory.com", "velog.io", "notion.so", "slack.com", "zoom.us",
    "spotify.com", "twitch.tv", "discord.com", "steampowered.com", "nvidia.com",
]

DNS_SERVER = "8.8.8.8"   # 외부 DNS 서버 (OS 캐시를 거치지 않도록 직접 질의)
DNS_TIMEOUT = 3.0        # 초

DEFAULT_RUNS = 5         # 시나리오별 반복 횟수
DEFAULT_QUERIES = 100    # 1회 실행당 조회 수
DEFAULT_TTL = 300        # 기본 캐시 TTL(초)
TTL_VALUES = [0.5, 1, 2, 5, 30]      # 시나리오 3: TTL별 실험값(초, 실험 시간 때문에 축소)
TTL_QUERY_INTERVAL = 0.03            # 시나리오 3: 조회 사이 간격(초)
POPULAR_RATIOS = [0.2, 0.5, 0.8]     # 시나리오 4: 인기 도메인 조회 비율
POPULAR_COUNT = 5                    # 인기 도메인 개수

OFFLINE = False          # --offline 이면 True (가짜 지연 사용)


# ---------------------------------------------------------------------------
# 외부 DNS 조회 (캐시 없음)
# ---------------------------------------------------------------------------

def resolve_external(domain):
    """외부 DNS 서버에 직접 질의한다. 반환: (ip, ttl). 실패하면 (None, 0)."""
    if OFFLINE:
        time.sleep(random.uniform(0.02, 0.06))   # 가짜 네트워크 지연 20~60ms
        return "0.0.0.0", DEFAULT_TTL

    try:
        import dns.resolver
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [DNS_SERVER]
        resolver.lifetime = DNS_TIMEOUT
        answer = resolver.resolve(domain, "A")
        return answer[0].to_text(), answer.rrset.ttl
    except ImportError:
        # dnspython이 없으면 OS 함수 사용 (OS 캐시 영향을 받으므로 비추천)
        try:
            return socket.gethostbyname(domain), DEFAULT_TTL
        except OSError:
            return None, 0
    except Exception:
        return None, 0


# ---------------------------------------------------------------------------
# 임시 캐시 (1번 코드 오기 전까지 사용)
# ---------------------------------------------------------------------------

class _MockCache:
    """딕셔너리 + TTL로 만든 임시 캐시. 1번의 dns_cache.py가 오면 교체한다."""

    def __init__(self, ttl):
        self.ttl = ttl
        self.store = {}   # domain -> (ip, 만료시각)

    def lookup(self, domain):
        """반환: (ip, hit)"""
        now = time.monotonic()
        entry = self.store.get(domain)
        if entry and entry[1] > now:
            return entry[0], True
        ip, _record_ttl = resolve_external(domain)
        if ip is not None:
            self.store[domain] = (ip, now + self.ttl)
        return ip, False


# ---------------------------------------------------------------------------
# 1번 코드 연결부 — 1번 코드가 오면 이 두 함수만 수정한다
# ---------------------------------------------------------------------------

BACKEND = "mock"

def make_cache(ttl):
    """TTL을 지정해 새 캐시를 만든다."""
    # TODO: 1번 코드 도착 후 아래처럼 교체 (실제 클래스/인자 이름에 맞출 것)
    #   from dns_cache import DNSCache
    #   return DNSCache(ttl=ttl)
    return _MockCache(ttl)


def do_lookup(cache, domain):
    """캐시를 통해 조회한다. 반환: (ip, hit)"""
    # TODO: 1번 코드의 조회 함수/반환 형식에 맞출 것
    return cache.lookup(domain)


# ---------------------------------------------------------------------------
# 측정
# ---------------------------------------------------------------------------

def measure(scenario, run, domain, cache=None):
    """조회 1건을 측정해 기록 1줄(dict)을 반환한다. cache=None 이면 캐시 없이 조회."""
    start = time.perf_counter()
    if cache is None:
        ip, _ = resolve_external(domain)
        hit = False
    else:
        ip, hit = do_lookup(cache, domain)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return {
        "scenario": scenario,
        "run": run,
        "domain": domain,
        "time_ms": round(elapsed_ms, 4),
        "hit": hit,
        "external_query": not hit,   # 캐시 miss = 외부 DNS 질의 1회
        "success": ip is not None,
    }


def make_workload(n, popular_ratio=0.5, seed=None):
    """조회할 도메인 순서를 만든다. popular_ratio 비율만큼 인기 도메인에서 뽑는다."""
    rng = random.Random(seed)
    popular = TEST_DOMAINS[:POPULAR_COUNT]
    return [
        rng.choice(popular) if rng.random() < popular_ratio else rng.choice(TEST_DOMAINS)
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# 시나리오
# ---------------------------------------------------------------------------

def scenario_cache_vs_nocache(runs, n):
    """시나리오 1: 같은 조회 목록을 캐시 없이 / 캐시로 조회한다."""
    records = []
    for run in range(1, runs + 1):
        workload = make_workload(n, popular_ratio=0.5, seed=run)
        for d in workload:
            records.append(measure("no_cache", run, d))
        cache = make_cache(DEFAULT_TTL)
        for d in workload:
            records.append(measure("with_cache", run, d, cache))
    return records


def scenario_cold_vs_warm(runs):
    """시나리오 2: 빈 캐시에서 전체 도메인 1회(cold) → 같은 목록 다시(warm)."""
    records = []
    for run in range(1, runs + 1):
        cache = make_cache(DEFAULT_TTL)
        for d in TEST_DOMAINS:
            records.append(measure("cold", run, d, cache))
        for d in TEST_DOMAINS:
            records.append(measure("warm", run, d, cache))
    return records


def scenario_ttl(runs, n):
    """시나리오 3: TTL 값을 바꿔가며 적중률 변화를 본다."""
    records = []
    for ttl in TTL_VALUES:
        for run in range(1, runs + 1):
            cache = make_cache(ttl)
            for d in make_workload(n, popular_ratio=0.5, seed=run):
                records.append(measure(f"ttl_{ttl}", run, d, cache))
                time.sleep(TTL_QUERY_INTERVAL)
    return records


def scenario_popularity(runs, n):
    """시나리오 4: 인기 도메인 조회 비율을 바꿔가며 적중률 변화를 본다."""
    records = []
    for ratio in POPULAR_RATIOS:
        for run in range(1, runs + 1):
            cache = make_cache(DEFAULT_TTL)
            for d in make_workload(n, popular_ratio=ratio, seed=run):
                records.append(measure(f"popular_{ratio}", run, d, cache))
    return records


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------

def summarize(records, scenario):
    rows = [r for r in records if r["scenario"] == scenario]
    if not rows:
        return None
    times = [r["time_ms"] for r in rows]
    hits = sum(r["hit"] for r in rows)
    runs = len({r["run"] for r in rows})
    return {
        "total_queries": len(rows),
        "runs": runs,
        "cache_hits": hits,
        "cache_misses": len(rows) - hits,
        "hit_rate": round(hits / len(rows), 4),
        "external_queries": sum(r["external_query"] for r in rows),
        "avg_time_ms": round(statistics.mean(times), 3),
        "median_time_ms": round(statistics.median(times), 3),
        "stdev_time_ms": round(statistics.stdev(times), 3) if len(times) > 1 else 0.0,
        "failed_queries": sum(not r["success"] for r in rows),
    }


def build_summary(records, runs, n):
    s = {}
    for name in sorted({r["scenario"] for r in records}):
        s[name] = summarize(records, name)

    no_c, with_c = s.get("no_cache"), s.get("with_cache")
    comparison = {}
    if no_c and with_c:
        comparison = {
            "avg_time_no_cache_ms": no_c["avg_time_ms"],
            "avg_time_with_cache_ms": with_c["avg_time_ms"],
            "speedup": round(no_c["avg_time_ms"] / with_c["avg_time_ms"], 2)
                       if with_c["avg_time_ms"] > 0 else None,
            "external_queries_no_cache": no_c["external_queries"],
            "external_queries_with_cache": with_c["external_queries"],
            "external_query_reduction": round(
                1 - with_c["external_queries"] / no_c["external_queries"], 4)
                if no_c["external_queries"] else None,
        }

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "backend": BACKEND,          # "mock" 이면 최종 결과로 쓰면 안 됨
            "offline": OFFLINE,          # True 면 가짜 지연(동작 확인용)
            "dns_server": DNS_SERVER,
            "runs_per_scenario": runs,
            "queries_per_run": n,
            "domain_count": len(TEST_DOMAINS),
            "default_ttl_s": DEFAULT_TTL,
        },
        "comparison": comparison,
        "scenarios": s,
    }


# ---------------------------------------------------------------------------
# 저장 / 시각화
# ---------------------------------------------------------------------------

COLOR_1 = "#2a78d6"   # 파랑
COLOR_2 = "#eb6834"   # 주황
TEXT = "#52514e"
GRID = "#e4e3df"


def save_csv(records, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)


def save_json(summary, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def _style(ax, title, ylabel):
    ax.set_title(title, loc="left", fontsize=13, color="#0b0b0b", pad=12)
    ax.set_ylabel(ylabel, color=TEXT)
    ax.tick_params(colors=TEXT)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _bar_labels(ax, bars, fmt):
    for b in bars:
        ax.annotate(fmt(b.get_height()), (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom", xytext=(0, 4), textcoords="offset points",
                    color=TEXT, fontsize=10)


def save_graphs(summary, records, graph_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = summary["scenarios"]
    tag = " (MOCK)" if summary["meta"]["backend"] == "mock" or summary["meta"]["offline"] else ""
    saved = []

    # 1) 캐시 유무별 평균 조회 시간
    if "no_cache" in s and "with_cache" in s:
        fig, ax = plt.subplots(figsize=(6, 4.5))
        names = ["No cache", "With cache"]
        vals = [s["no_cache"]["avg_time_ms"], s["with_cache"]["avg_time_ms"]]
        bars = ax.bar(names, vals, color=[COLOR_2, COLOR_1], width=0.5)
        _bar_labels(ax, bars, lambda v: f"{v:.1f} ms")
        _style(ax, "Average DNS lookup time" + tag, "ms")
        fig.tight_layout()
        p = os.path.join(graph_dir, "1_avg_time_cache_vs_nocache.png")
        fig.savefig(p, dpi=150); plt.close(fig); saved.append(p)

    # 2) 외부 DNS 조회 횟수
    if "no_cache" in s and "with_cache" in s:
        fig, ax = plt.subplots(figsize=(6, 4.5))
        vals = [s["no_cache"]["external_queries"], s["with_cache"]["external_queries"]]
        bars = ax.bar(["No cache", "With cache"], vals, color=[COLOR_2, COLOR_1], width=0.5)
        _bar_labels(ax, bars, lambda v: f"{int(v)}")
        _style(ax, "External DNS queries (total)" + tag, "queries")
        fig.tight_layout()
        p = os.path.join(graph_dir, "2_external_queries.png")
        fig.savefig(p, dpi=150); plt.close(fig); saved.append(p)

    # 3) Cold vs Warm 조회 시간 분포
    cold = [r["time_ms"] for r in records if r["scenario"] == "cold"]
    warm = [r["time_ms"] for r in records if r["scenario"] == "warm"]
    if cold and warm:
        fig, ax = plt.subplots(figsize=(6, 4.5))
        bp = ax.boxplot([cold, warm], tick_labels=["Cold (1st lookup)", "Warm (repeat)"],
                        patch_artist=True, widths=0.45, showfliers=False)
        for patch, c in zip(bp["boxes"], [COLOR_2, COLOR_1]):
            patch.set_facecolor(c); patch.set_alpha(0.85); patch.set_edgecolor(c)
        for med in bp["medians"]:
            med.set_color("#ffffff"); med.set_linewidth(2)
        ax.set_yscale("log")   # warm은 0.01ms 수준이라 로그 스케일로 표시
        lo = max(min(min(cold), min(warm)), 1e-4)
        ax.set_ylim(lo / 3, max(max(cold), max(warm)) * 3)
        for i, data in enumerate([cold, warm], start=1):
            med = max(statistics.median(data), 1e-4)
            ax.annotate(f"median {med:.3g} ms", (i, med), xytext=(42, 0),
                        textcoords="offset points", va="center", color=TEXT, fontsize=9)
        _style(ax, "Lookup time: cold vs warm cache" + tag, "ms (log scale)")
        fig.tight_layout()
        p = os.path.join(graph_dir, "3_cold_vs_warm.png")
        fig.savefig(p, dpi=150); plt.close(fig); saved.append(p)

    # 4) TTL별 적중률
    ttl_x = [t for t in TTL_VALUES if f"ttl_{t}" in s]
    if ttl_x:
        fig, ax = plt.subplots(figsize=(6, 4.5))
        y = [s[f"ttl_{t}"]["hit_rate"] * 100 for t in ttl_x]
        ax.plot([str(t) for t in ttl_x], y, color=COLOR_1, linewidth=2,
                marker="o", markersize=8)
        for xi, yi in zip([str(t) for t in ttl_x], y):
            ax.annotate(f"{yi:.0f}%", (xi, yi), xytext=(0, 8), textcoords="offset points",
                        ha="center", color=TEXT, fontsize=10)
        ax.set_ylim(0, 105)
        ax.set_xlabel("TTL (seconds)", color=TEXT)
        _style(ax, "Cache hit rate by TTL" + tag, "hit rate (%)")
        fig.tight_layout()
        p = os.path.join(graph_dir, "4_hit_rate_by_ttl.png")
        fig.savefig(p, dpi=150); plt.close(fig); saved.append(p)

    # 5) 인기 도메인 비율별 적중률
    pop_x = [r for r in POPULAR_RATIOS if f"popular_{r}" in s]
    if pop_x:
        fig, ax = plt.subplots(figsize=(6, 4.5))
        y = [s[f"popular_{r}"]["hit_rate"] * 100 for r in pop_x]
        bars = ax.bar([f"{int(r * 100)}%" for r in pop_x], y, color=COLOR_1, width=0.5)
        _bar_labels(ax, bars, lambda v: f"{v:.0f}%")
        ax.set_ylim(0, 105)
        ax.set_xlabel("Share of queries to top-5 popular domains", color=TEXT)
        _style(ax, "Cache hit rate by query popularity" + tag, "hit rate (%)")
        fig.tight_layout()
        p = os.path.join(graph_dir, "5_hit_rate_by_popularity.png")
        fig.savefig(p, dpi=150); plt.close(fig); saved.append(p)

    return saved


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def run_benchmark(output_dir="results", runs=DEFAULT_RUNS, n=DEFAULT_QUERIES,
                  offline=False, make_plots=True):
    """전체 실험을 실행하고 summary(dict)를 반환한다. 3번 main.py에서 호출용."""
    global OFFLINE
    OFFLINE = offline

    graph_dir = os.path.join(output_dir, "graphs")
    os.makedirs(graph_dir, exist_ok=True)

    print(f"[benchmark] backend={BACKEND}  offline={OFFLINE}  runs={runs}  queries/run={n}")
    if BACKEND == "mock":
        print("[benchmark] 주의: 임시(mock) 캐시 사용 중 — 동작 확인용, 최종 결과 아님")

    records = []
    steps = [
        ("1/4 캐시 유무 비교", lambda: scenario_cache_vs_nocache(runs, n)),
        ("2/4 cold vs warm", lambda: scenario_cold_vs_warm(runs)),
        ("3/4 TTL별", lambda: scenario_ttl(runs, n)),
        ("4/4 인기 도메인 비율별", lambda: scenario_popularity(runs, n)),
    ]
    for label, fn in steps:
        t0 = time.time()
        print(f"[benchmark] {label} ...", end=" ", flush=True)
        records.extend(fn())
        print(f"done ({time.time() - t0:.1f}s)")

    summary = build_summary(records, runs, n)

    save_csv(records, os.path.join(output_dir, "raw.csv"))
    save_json(summary, os.path.join(output_dir, "summary.json"))
    print(f"[benchmark] 저장: {output_dir}/raw.csv, {output_dir}/summary.json")

    if make_plots:
        for p in save_graphs(summary, records, graph_dir):
            print(f"[benchmark] 그래프: {p}")

    c = summary["comparison"]
    if c:
        print(f"[benchmark] 평균 조회시간 {c['avg_time_no_cache_ms']} ms → "
              f"{c['avg_time_with_cache_ms']} ms (x{c['speedup']}), "
              f"외부 조회 {c['external_queries_no_cache']} → {c['external_queries_with_cache']}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DNS 캐시 성능 측정")
    parser.add_argument("--quick", action="store_true", help="빠른 동작 확인 (반복 1회, 조회 30개)")
    parser.add_argument("--offline", action="store_true", help="네트워크 없이 가짜 지연 사용")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--queries", type=int, default=DEFAULT_QUERIES)
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    runs, n = (1, 30) if args.quick else (args.runs, args.queries)
    run_benchmark(output_dir=args.out, runs=runs, n=n, offline=args.offline)
