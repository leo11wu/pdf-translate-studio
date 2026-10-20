"""Per-job OpenAI-compatible transport: one budget, limiter and capability policy.

Only request metadata is persisted. Credentials, prompts and response text never
enter telemetry or the capability cache.
"""
from __future__ import annotations

import contextlib
import email.utils
import hashlib
import json
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


PROTOCOL_VERSION = "paperstudio-0.4-json-v1"


@dataclass
class RequestBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + 180)
    attempts: int = 0

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0 or self.attempts >= 5:
            raise RuntimeError("此批翻译已达到 5 次请求或 180 秒上限，请稍后重试或调整接口。")
        return value


def normalize_options(values=None, *, legacy_qps=8):
    values = values or {}
    mode = values.get("concurrency_mode", "auto")
    policy = values.get("reasoning_policy", "lowest")
    if mode not in {"auto", "manual"} or policy not in {"lowest", "provider_default"}:
        raise ValueError("无效的并发模式或思考策略。")
    rate = float(values.get("request_rate", 2))
    if not math.isfinite(rate):
        raise ValueError("请求速率必须是有限数值。")
    return {
        "concurrency_mode": mode,
        "max_in_flight": max(1, min(int(values.get("max_in_flight", 16)), 16 if mode == 'auto' else 64)),
        "request_rate": max(0.1, min(rate, 64)),
        "batch_tokens": max(128, min(int(values.get("batch_tokens", 1536)), 8192)),
        "batch_paragraphs": max(1, min(int(values.get("batch_paragraphs", 16)), 32)),
        "reasoning_policy": policy,
        "profile_id": str(values.get("profile_id", "session"))[:100],
        "protocol_version": PROTOCOL_VERSION,
    }


def capability_candidates(model, metadata=None):
    metadata = metadata or {}
    effort = metadata.get("effort", {})
    levels = effort.get("supported_levels", []) if isinstance(effort, dict) else []
    name = model.lower()
    if name.startswith("deepseek"):
        # The model list's effort levels describe enabled thinking. Its
        # separate on/off switch can still disable thinking when 'none' is
        # absent from that list (as on the official DeepSeek endpoint).
        minimum = next((v for v in ("minimal", "low", "medium", "high", "max") if v in levels), "low")
        return [{"reasoning_effort": "none", "extra_body": {"thinking": {"type": "disabled"}}},
                {"extra_body": {"thinking": {"type": "disabled"}}},
                {"reasoning_effort": "none"}, {"reasoning_effort": minimum}, {}]
    if levels:
        supported = [v for v in ("none", "minimal", "low", "medium", "high", "xhigh", "max") if v in levels]
        if supported:
            return [{"reasoning_effort": supported[0]}, {}]
    if name.startswith(("gpt-6.1-sol", "gpt-6-astra")):
        return [{"reasoning_effort": "low"}, {}]
    if name.startswith("gemini"):
        minimum = "low" if "pro" in name or "argon" in name else "minimal"
        return [{"reasoning_effort": minimum}, {"reasoning_effort": "low"}, {}]
    return [{"reasoning_effort": "none"}, {"reasoning_effort": "minimal"},
            {"reasoning_effort": "low"}, {}]


def parameter_rejection(exc, params):
    if getattr(exc, "status_code", None) not in {400, 422}:
        return False
    message = str(exc).lower()
    parameters = (["thinking"] if "extra_body" in params else []) + (["reasoning_effort"] if "reasoning_effort" in params else [])
    return bool(params and any(parameter in message for parameter in parameters) and re.search(
        r"unsupported|not supported|not permitted|not allowed|unknown|unrecognized|invalid.{0,30}(value|parameter)|allowed|supported values|extra inputs|不支持|无效", message))


class RequestRuntime:
    def __init__(self, options, *, stats_path=None, capability_path=None, model="", base_url="", metadata=None):
        self.options = normalize_options(options)
        self.stats_path = Path(stats_path) if stats_path else None
        self.capability_path = Path(capability_path) if capability_path else None
        self.model = model
        self.metadata = metadata or {}
        self.cancel_check = lambda: None
        self.condition = threading.Condition(threading.RLock())
        self.negotiation = threading.Lock()
        self.local = threading.local()
        self.limit = min(4, self.options["max_in_flight"]) if self.options["concurrency_mode"] == "auto" else self.options["max_in_flight"]
        self.rate = self.options["request_rate"]
        self.next_request = 0.0
        self.cooldown_until = 0.0
        self.success_streak = 0
        self.fatal = ""
        self.last_write = 0.0
        self.selected = None
        self.stats = {"requests": 0, "successful_requests": 0, "in_flight": 0,
                      "peak_in_flight": 0, "rate_limits": 0, "upstream_errors": 0,
                      "protocol_retries": 0, "capability_retries": 0, "request_seconds": 0.0,
                      "last_response_at": None, "usage": {}, "status": "preparing",
                      "reasoning_status": "等待首批请求协商", "reasoning_confirmed": False}
        self.cache_identity = hashlib.sha256(json.dumps([PROTOCOL_VERSION, capability_candidates(model, self.metadata),
            self.options["profile_id"], base_url, model,
            self.options["reasoning_policy"], self.metadata], sort_keys=True).encode()).hexdigest()
        if self.options["reasoning_policy"] == "provider_default":
            self.selected = {}
        elif self.capability_path:
            try:
                cached = json.loads(self.capability_path.read_text())
                if cached.get("identity") == self.cache_identity and time.time() - cached.get("saved_at", 0) < 86400:
                    allowed = capability_candidates(model, self.metadata)
                    if cached.get("parameters") in allowed:
                        self.selected = cached["parameters"]
            except (OSError, ValueError, TypeError):
                pass

    def check(self):
        self.cancel_check()
        if self.fatal:
            raise RuntimeError(self.fatal)

    @contextlib.contextmanager
    def budget(self):
        previous = getattr(self.local, "budget", None)
        self.local.budget = previous or RequestBudget()
        try:
            yield self.local.budget
        finally:
            self.local.budget = previous

    def snapshot(self):
        with self.condition:
            return {**self.stats, "usage": dict(self.stats["usage"]), "effective_concurrency": self.limit,
                    "request_rate": round(self.rate, 3),
                    "retry_after_seconds": round(max(0, self.cooldown_until - time.monotonic()), 1),
                    "updated_at": time.time()}

    def emit(self, force=False):
        if not self.stats_path:
            return
        with self.condition:
            if not force and time.monotonic() - self.last_write < 0.4:
                return
            self.last_write = time.monotonic()
            try:
                self.stats_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = self.stats_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False))
                temporary.chmod(0o600)
                temporary.replace(self.stats_path)
            except OSError:
                pass

    def pause(self, seconds, budget):
        end = min(time.monotonic() + max(0, seconds), budget.deadline)
        with self.condition:
            while time.monotonic() < end:
                self.check()
                budget.remaining()
                self.condition.wait(min(0.15, end - time.monotonic()))

    @contextlib.contextmanager
    def slot(self, budget):
        with self.condition:
            while True:
                self.check()
                budget.remaining()
                now = time.monotonic()
                wait = max(self.next_request, self.cooldown_until) - now
                if self.stats["in_flight"] < self.limit and wait <= 0:
                    self.next_request = now + 1 / self.rate
                    self.stats["in_flight"] += 1
                    self.stats["peak_in_flight"] = max(self.stats["peak_in_flight"], self.stats["in_flight"])
                    self.stats["requests"] += 1
                    self.stats["status"] = "translating"
                    budget.attempts += 1
                    self.emit()
                    break
                self.condition.wait(min(0.15, max(wait, 0.02)))
        try:
            yield
        finally:
            with self.condition:
                self.stats["in_flight"] -= 1
                self.condition.notify_all()
                self.emit()

    @staticmethod
    def retry_after(exc):
        headers = getattr(getattr(exc, "response", None), "headers", {})
        value = headers.get("retry-after", "")
        try:
            return max(0, float(value))
        except (ValueError, TypeError):
            try:
                return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, AttributeError):
                return 0

    def record_success(self, response, duration, params):
        with self.condition:
            self.stats["successful_requests"] += 1
            self.stats["last_response_at"] = time.time()
            self.stats["request_seconds"] = round(self.stats["request_seconds"] + duration, 3)
            self.stats["last_request_seconds"] = round(duration, 3)
            self.stats["status"] = "translating"
            self.success_streak += 1
            if self.options["concurrency_mode"] == "auto" and self.success_streak >= 8:
                self.limit = min(self.options["max_in_flight"], self.limit + 2)
                self.success_streak = 0
            usage = getattr(response, "usage", None)
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage, field, None)
                if isinstance(value, int):
                    self.stats["usage"][field] = self.stats["usage"].get(field, 0) + value
            details = getattr(usage, "completion_tokens_details", None)
            reasoning = getattr(details, "reasoning_tokens", None)
            if isinstance(reasoning, int):
                self.stats["usage"]["reasoning_tokens"] = self.stats["usage"].get("reasoning_tokens", 0) + reasoning
            setting = params.get("reasoning_effort") or ("disabled" if params.get("extra_body") else "provider_default")
            self.stats["reasoning_setting"] = setting
            confirmed = getattr(response, "reasoning_effort", None) == setting and setting != "provider_default"
            self.stats["reasoning_confirmed"] = confirmed
            self.stats["reasoning_status"] = ("沿用服务商默认（接口未提供可用控制）" if not params and self.options["reasoning_policy"] == "lowest"
                else "服务商默认" if not params else f"已请求最低（{setting}），" + ("服务商已确认" if confirmed else "服务商未确认实际设置"))
            if setting in {'none','disabled'} and isinstance(reasoning, int) and reasoning > 0:
                self.stats['reasoning_confirmed'] = False
                self.stats['reasoning_status'] = '已请求关闭思考，但接口仍返回思考用量；服务商未确认实际设置'
            self.emit(force=True)

    def request(self, client, messages, *, extra=None):
        # One negotiation leader; other workers wait without occupying HTTP slots.
        with self.budget() as budget:
            while not self.negotiation.acquire(timeout=0.15):
                self.check()
                budget.remaining()
            negotiating = self.selected is None
            if not negotiating:
                self.negotiation.release()
            try:
                candidates = capability_candidates(self.model, self.metadata) if negotiating else [self.selected]
                index = 0
                while True:
                    params = candidates[min(index, len(candidates) - 1)]
                    self.check()
                    budget.remaining()
                    started = None
                    try:
                        with self.slot(budget):
                            started = time.monotonic()
                            response = client.chat.completions.create(model=self.model, messages=messages,
                                timeout=min(120, budget.deadline - started), **(extra or {}), **params)
                        self.record_success(response, time.monotonic() - started, params)
                        if negotiating:
                            self.selected = params
                            self._save_capability(params)
                        return response
                    except Exception as exc:
                        if started is not None:
                            duration = time.monotonic() - started
                            with self.condition:
                                self.stats["request_seconds"] = round(self.stats["request_seconds"] + duration, 3)
                                self.stats["last_request_seconds"] = round(duration, 3)
                                if getattr(exc, "status_code", None):
                                    self.stats["last_response_at"] = time.time()
                        if parameter_rejection(exc, params):
                            with self.condition:
                                self.stats["capability_retries"] += 1
                            if not negotiating:
                                # A previously accepted capability changed; renegotiate once.
                                self.selected = None
                                return self.request(client, messages, extra=extra)
                            index += 1
                            if index >= len(candidates):
                                raise RuntimeError("接口拒绝了翻译所需参数，请检查兼容接口配置。") from None
                            continue
                        status = getattr(exc, "status_code", None)
                        timeout = exc.__class__.__name__ in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "APIConnectionError"}
                        if status == 429 or status in {500, 502, 503, 504} or timeout:
                            with self.condition:
                                self.success_streak = 0
                                field = "rate_limits" if status == 429 else "upstream_errors"
                                self.stats[field] += 1
                                delay = max(self.retry_after(exc), min(30, 2 ** max(0, budget.attempts - 1)) + random.uniform(0, 0.5))
                                if status == 429:
                                    self.limit = max(1, self.limit // 2)
                                    self.rate = max(1, self.rate / 2)
                                    self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
                                self.stats["status"] = "rate_limited" if status == 429 else "retrying"
                                self.emit(force=True)
                            self.pause(delay, budget)
                            continue
                        if status in {401, 403}:
                            raise RuntimeError(f"翻译接口鉴权失败（{status}），请检查所选配置的密钥和模型权限。") from None
                        if status == 402:
                            raise RuntimeError("翻译接口余额或计费状态异常（402），请检查所选供应商的账户额度。") from None
                        if status in {400, 413, 422} and re.search(r"context|token.{0,20}(limit|exceed)|too long|maximum context|payload|上下文|长度", str(exc), re.I):
                            raise ContextLimitError("接口上下文或输出长度限制，需要缩小批次。") from None
                        if status:
                            raise RuntimeError(f"翻译接口返回 HTTP {status}，请检查供应商状态与模型配置。") from None
                        raise RuntimeError(f"翻译请求失败（{exc.__class__.__name__}）。") from None
            finally:
                if negotiating:
                    self.negotiation.release()

    def _save_capability(self, params):
        if not self.capability_path:
            return
        with contextlib.suppress(OSError):
            self.capability_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.capability_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"identity": self.cache_identity, "parameters": params, "saved_at": time.time()}))
            temporary.chmod(0o600)
            temporary.replace(self.capability_path)


class ContextLimitError(RuntimeError):
    pass
