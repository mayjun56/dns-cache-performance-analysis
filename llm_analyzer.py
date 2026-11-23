"""LLM-based DNS cache benchmark analysis.

Uses a GLM/OpenAI-compatible Chat Completions endpoint with the Python standard
library. A deterministic local summary is available for offline demonstrations;
it must NOT be presented as an LLM-generated report.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib import error, request

DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4-flash"


class AnalysisError(RuntimeError):
    """A benchmark or model request could not be analyzed."""


@dataclass(frozen=True)
class AnalysisResult:
    report: str
    source: str  # "llm_api", "local", or "local_fallback"
    model: str | None = None
    warning: str | None = None


def _number(data: Mapping[str, Any], name: str, *, required: bool = True) -> float | None:
    value = data.get(name)
    if value is None:
        if required:
            raise ValueError(f"Missing metric: {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    import math
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def normalize_metrics(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the shared benchmark format and add derived metrics.

    Required keys: domain, uncached_avg_ms, cached_avg_ms.
    Optional: iterations, hit_count, miss_count, dns_calls, cache_hit_rate,
    data_type, notes. cache_hit_rate is a fraction from 0 to 1.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("Benchmark result must be a dictionary")
    domain = raw.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain must be a nonempty string")
    uncached = _number(raw, "uncached_avg_ms")
    cached = _number(raw, "cached_avg_ms")
    hits = _number(raw, "hit_count", required=False)
    misses = _number(raw, "miss_count", required=False)
    iterations = _number(raw, "iterations", required=False)
    dns_calls = _number(raw, "dns_calls", required=False)
    hit_rate = _number(raw, "cache_hit_rate", required=False)
    for k, v in (("hit_count", hits), ("miss_count", misses), ("iterations", iterations), ("dns_calls", dns_calls)):
        if v is not None and not v.is_integer():
            raise ValueError(f"{k} must be a whole number")
    if iterations is not None and iterations < 1:
        raise ValueError("iterations must be positive")
    if hits is not None and misses is not None:
        if iterations is not None and hits + misses != iterations:
            raise ValueError("hit_count + miss_count must equal iterations")
        computed = hits / (hits + misses) if (hits + misses) else 0.0
        if hit_rate is not None and abs(hit_rate - computed) > 0.001:
            raise ValueError("cache_hit_rate conflicts with hit_count/miss_count")
        hit_rate = computed
    if hit_rate is not None and hit_rate > 1:
        raise ValueError("cache_hit_rate must be a fraction between 0 and 1")
    output: dict[str, Any] = {
        "domain": domain.strip(),
        "uncached_avg_ms": uncached,
        "cached_avg_ms": cached,
        "iterations": int(iterations) if iterations is not None else None,
        "hit_count": int(hits) if hits is not None else None,
        "miss_count": int(misses) if misses is not None else None,
        "dns_calls": int(dns_calls) if dns_calls is not None else None,
        "cache_hit_rate": hit_rate,
        "data_type": str(raw.get("data_type", "unspecified")),
        "notes": str(raw.get("notes", "")),
    }
    output["speedup"] = uncached / cached if cached > 0 else None
    output["reduction_percent"] = (uncached - cached) / uncached * 100 if uncached > 0 else None
    return output


def _metric_lines(data: Mapping[str, Any], lang: str) -> list[str]:
    avg1, avg2 = data["uncached_avg_ms"], data["cached_avg_ms"]
    hit = data.get("cache_hit_rate")
    pct = data.get("reduction_percent")
    speed = data.get("speedup")
    if lang == "ko":
        rows = [
            f"- 도메인: {data['domain']}",
            f"- 데이터 종류: {data['data_type']}",
            f"- 캐시 미사용 평균 시간: {avg1:.3f} ms",
            f"- 캐시 사용 평균 시간: {avg2:.3f} ms",
        ]
        if pct is not None:
            rows.append(f"- 평균 시간 감소율: {pct:.2f}%" if pct >= 0 else f"- 평균 시간 증가율: {-pct:.2f}%")
        if speed is not None:
            rows.append(f"- 속도 비율(미사용/사용): {speed:.2f}배")
        if hit is not None:
            rows.append(f"- 캐시 적중률: {hit * 100:.2f}%")
        if data.get("dns_calls") is not None:
            rows.append(f"- 캐시 사용 측정 중 실제 resolver 호출 횟수: {data['dns_calls']}")
    elif lang == "zh":
        rows = [f"- 域名：{data['domain']}", f"- 数据类型：{data['data_type']}",
                f"- 无应用缓存平均耗时：{avg1:.3f} ms", f"- 使用应用缓存平均耗时：{avg2:.3f} ms"]
        if pct is not None:
            rows.append(f"- 平均耗时变化（降低为正）：{pct:.2f}%")
        if speed is not None:
            rows.append(f"- 耗时比（无缓存/有缓存）：{speed:.2f} 倍")
        if hit is not None:
            rows.append(f"- 缓存命中率：{hit * 100:.2f}%")
        if data.get("dns_calls") is not None:
            rows.append(f"- 缓存组 resolver 实际调用次数：{data['dns_calls']}")
    else:
        rows = [f"- Domain: {data['domain']}", f"- Data type: {data['data_type']}",
                f"- Mean latency without app cache: {avg1:.3f} ms", f"- Mean latency with app cache: {avg2:.3f} ms"]
        if pct is not None:
            rows.append(f"- Mean latency reduction (positive means faster): {pct:.2f}%")
        if speed is not None:
            rows.append(f"- Latency ratio (without/with): {speed:.2f}x")
        if hit is not None:
            rows.append(f"- Cache hit rate: {hit * 100:.2f}%")
        if data.get("dns_calls") is not None:
            rows.append(f"- Resolver calls during cached runs: {data['dns_calls']}")
    if data.get("iterations") is not None:
        rows.append(f"- Iterations: {data['iterations']}")
    if data.get("notes"):
        rows.append(f"- Notes: {data['notes']}")
    return rows


def local_summary(raw: Mapping[str, Any], *, lang: str = "ko") -> str:
    """Make an accurate offline rule-based report, not an LLM report."""
    data = normalize_metrics(raw)
    headings = {
        "ko": ("DNS 캐시 성능 분석 — 규칙 기반 요약", "측정 지표", "해석 및 한계",
               "캐시 사용 조건과 실험 환경에서 측정된 값만으로 성능을 해석해야 합니다. 운영체제 및 재귀 DNS 서버의 캐시 때문에 '캐시 미사용'도 모든 DNS 요청이 외부 네트워크에 도달했다는 뜻은 아닙니다. 합성 데이터는 실제 성능 개선을 입증하지 않습니다."),
        "zh": ("DNS 缓存性能分析——规则生成（非 LLM）", "指标", "解释与局限",
               "本结果只说明给定条件下的数据。操作系统或递归 DNS 的缓存可能影响无应用缓存测量；合成数据不能证明真实优化效果。"),
        "en": ("DNS Cache Performance — Rule-based Summary (not LLM)", "Metrics", "Interpretation and limits",
               "Results apply only to the tested conditions. OS and recursive DNS caches may influence the no-app-cache baseline. Synthetic data does not establish real-world improvements."),
    }
    if lang not in headings:
        raise ValueError("lang must be 'ko', 'zh', or 'en'")
    title, metrics, limits, caution = headings[lang]
    return "\n".join([f"# {title}", "", f"## {metrics}", *_metric_lines(data, lang), "", f"## {limits}", caution])


def _prompt(raw: Mapping[str, Any], lang: str) -> list[dict[str, str]]:
    data = normalize_metrics(raw)
    language = {"ko": "Korean", "zh": "Simplified Chinese", "en": "English"}[lang]
    system = (
        f"You are a computer-networking lab assistant. Write a concise project midterm "
        f"performance analysis in {language}. Include: measurements, interpretation, "
        "limitations, and next tests. Only use the supplied numeric values. Do not "
        "invent experiments, p-values, results, or project completion claims. "
        "If data_type is synthetic, state unambiguously that it is a simulated example, "
        "not empirical evidence. Avoid presenting LLM output as verification. "
        "Remember OS-level and recursive resolver caching can confound the 'without "
        "application cache' baseline. Never reveal credentials."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": "Analyze the following JSON benchmark data:\n" + json.dumps(data, ensure_ascii=False)}]


def analyze_performance(
    metrics: Mapping[str, Any], *, mode: str = "auto", lang: str = "ko",
    api_key: str | None = None, api_url: str | None = None,
    model: str | None = None, timeout: int = 25,
) -> AnalysisResult:
    """Analyze metrics using GLM-compatible chat completions or offline summary.

    mode='local': never call an API.
    mode='api': require an API key, propagate errors.
    mode='auto': API when a key is present, otherwise local; on API failure,
    clearly flag rule-based fallback to avoid misrepresenting results.
    """
    if mode not in ("auto", "local", "api"):
        raise ValueError("mode must be 'auto', 'local', or 'api'")
    if lang not in ("ko", "zh", "en"):
        raise ValueError("lang must be 'ko', 'zh', or 'en'")
    validated = normalize_metrics(metrics)
    key = api_key or os.environ.get("GLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if mode == "local" or (mode == "auto" and not key):
        reason = "No API key: rule-based summary, NOT LLM output" if mode == "auto" else None
        return AnalysisResult(local_summary(validated, lang=lang), "local", warning=reason)
    if not key:
        raise AnalysisError("GLM_API_KEY (or LLM_API_KEY) is missing")
    model_name = model or os.environ.get("GLM_MODEL", DEFAULT_MODEL)
    url = api_url or os.environ.get("LLM_API_URL", DEFAULT_API_URL)
    if not url.startswith("https://"):
        raise AnalysisError("LLM_API_URL must use HTTPS")
    payload = {"model": model_name, "messages": _prompt(validated, lang), "stream": False, "max_tokens": 800}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            output = json.loads(resp.read().decode("utf-8"))
        text = output["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise AnalysisError("LLM response is empty or not text")
        return AnalysisResult(text.strip(), "llm_api", model=model_name)
    except (error.HTTPError, error.URLError, TimeoutError, KeyError, IndexError,
            ValueError, json.JSONDecodeError, AnalysisError) as exc:
        # Avoid logging request headers or keys.
        details = f"LLM request failed ({type(exc).__name__})"
        if mode == "api":
            raise AnalysisError(details) from exc
        return AnalysisResult(local_summary(validated, lang=lang), "local_fallback", warning=details + "; used rule-based summary instead")
