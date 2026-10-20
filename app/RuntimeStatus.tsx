"use client";

import { useEffect, useState } from "react";
import { elapsed, type TranslationJob } from "./job-types";

export default function RuntimeStatus({ job }: { job: TranslationJob }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const active = ["queued", "running", "cancelling"].includes(job.status);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const stats = job.runtime_stats;
  if (!stats || !Object.keys(stats).length) return null;
  const waiting = stats.status === "rate_limited" ? "上游限流，等待重试" : stats.status === "retrying" ? "上游暂不可用，等待重试" : "正在翻译";
  const stage = /typeset|font|drawing|save|merge|排版|保存|合并/i.test(job.stage) ? "正在排版与保存" : waiting;
  const delay = Math.max(0, (active ? now : job.finished_at ?? now) - (stats.last_response_at ?? job.created_at));
  return <div className="runtime-status" aria-live="polite">
    <strong>{active ? stage : job.status === "completed" ? "请求处理已完成" : "请求处理已停止"}</strong>
    <p>成功 {stats.successful_requests ?? 0} / 请求 {stats.requests ?? 0} 次 · 在途 {active ? stats.in_flight ?? 0 : 0} / {stats.effective_concurrency ?? 0} · 限流重试 {stats.rate_limits ?? 0} 次</p>
    {active && <p>{stats.last_response_at ? "距离最近响应" : "等待首个响应"} {elapsed(delay)}{stats.retry_after_seconds ? ` · 本轮退避约 ${stats.retry_after_seconds} 秒` : ""}</p>}
    <p>{stats.reasoning_status}</p>
    {stats.usage && Object.keys(stats.usage).length > 0 && <details><summary>服务商返回的用量与请求耗时</summary>
      <p>输入 {stats.usage.prompt_tokens ?? "未提供"} · 输出 {stats.usage.completion_tokens ?? "未提供"} · 思考 {stats.usage.reasoning_tokens ?? "未提供"} token</p>
      <p>平均请求耗时 {elapsed(stats.requests ? (stats.request_seconds ?? 0) / stats.requests : null)} · 协议重试 {stats.protocol_retries ?? 0} 次 · 上游错误 {stats.upstream_errors ?? 0} 次</p>
    </details>}
  </div>;
}
