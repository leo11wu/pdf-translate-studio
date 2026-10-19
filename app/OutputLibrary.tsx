"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { nativeFiles, type OutputSettings } from "./desktop-bridge";
import { elapsed, type TranslationJob } from "./job-types";

type Props = { endpoint: string; job: TranslationJob | null; autoSaveJobId: string | null; onSelect: (job: TranslationJob) => void };
const names: Record<string, string> = { completed: "完成", failed: "失败", interrupted: "中断", cancelled: "已取消", cancelling: "取消中", queued: "等待", running: "进行中" };

export default function OutputLibrary({ endpoint, job, autoSaveJobId, onSelect }: Props) {
  const [settings, setSettings] = useState<OutputSettings | null>(null);
  const [history, setHistory] = useState<TranslationJob[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(30);
  const [message, setMessage] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [saving, setSaving] = useState<string | null>(null);
  const [textBusy, setTextBusy] = useState<string | null>(null);
  const attempted = useRef(new Set<string>());

  useEffect(() => { nativeFiles("outputSettings").then(setSettings).catch((error: Error) => setMessage(error.message)); }, []);
  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${endpoint}/api/history?limit=${Math.min(limit, 100)}&q=${encodeURIComponent(query)}`);
      if (!response.ok) throw new Error("读取历史记录失败，可点击刷新重试。");
      const result = await response.json();
      setHistory(result.jobs); setTotal(result.total); setHistoryError("");
    } catch (error) { setHistoryError(error instanceof Error ? error.message : "历史记录暂不可用。"); }
  }, [endpoint, limit, query]);

  useEffect(() => {
    const initial = setTimeout(() => { void refresh(); }, 200);
    const timer = ["queued", "running", "cancelling"].includes(job?.status ?? "") ? setInterval(() => { void refresh(); }, 3000) : null;
    return () => { clearTimeout(initial); if (timer) clearInterval(timer); };
  }, [refresh, job?.id, job?.status]);

  const saveJob = useCallback(async (id: string) => {
    setSaving(id); setMessage("正在保存结果到统一目录…");
    try {
      const result = await nativeFiles("exportJob", { job_id: id });
      setMessage(`已保存：${result.folder}`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "保存未完成；可从历史记录重试。"); }
    finally { setSaving(null); }
  }, []);

  useEffect(() => {
    if (job?.status !== "completed" || job.id !== autoSaveJobId || !settings?.auto_save || attempted.current.has(job.id)) return;
    attempted.current.add(job.id);
    void saveJob(job.id);
  }, [job?.id, job?.status, autoSaveJobId, settings?.auto_save, saveJob]);

  const invoke = async (action: "chooseOutput" | "openOutput" | "openJobFolder", id?: string) => {
    try {
      const result = await nativeFiles(action, id ? { job_id: id } : {});
      if (action === "chooseOutput") { setSettings(result); setMessage("新任务将保存到所选目录，已保存的历史文件保留原位置。"); }
    } catch (error) { setMessage(error instanceof Error ? error.message : "操作未完成。"); }
  };

  const prepareText = async (id: string) => {
    setTextBusy(id); setMessage("正在补充 TXT，不调用 API…");
    try {
      const response = await fetch(`${endpoint}/api/jobs/${id}/prepare-text`, { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "TXT 提取失败。");
      onSelect(result); await refresh();
      setMessage("原文与译文 TXT 已就绪，可点击“保存结果”或单独另存。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "TXT 提取失败。"); }
    finally { setTextBusy(null); }
  };

  return <section id="library" className="library-section content-width card" aria-label="输出目录与翻译历史">
    <div className="library-heading"><div><span className="section-kicker">结果与历史</span><h2>所有结果，有处可寻。</h2></div><button className="save-config" type="button" onClick={() => void refresh()}>刷新历史</button></div>
    <div className="output-folder"><div><strong>统一输出目录</strong><p>{settings?.directory ?? "正在读取输出目录…"}</p></div><button className="save-config" type="button" disabled={saving !== null} onClick={() => void invoke("chooseOutput")}>更改目录</button><button className="text-action" type="button" onClick={() => void invoke("openOutput")}>打开目录</button></div>
    <label className="remember-control"><input type="checkbox" checked={settings?.auto_save ?? true} disabled={!settings} onChange={(event) => { void nativeFiles("setAutoSave", { enabled: event.target.checked }).then(setSettings).catch((error: Error) => setMessage(error.message)); }} />任务完成后自动保存，每个任务使用独立文件夹</label>
    <p className="provider-hint">历史记录重启后仍保留。结果文件夹含本次勾选的 PDF / TXT；再次保存不会覆盖已有文件。</p>
    {message && <p className="library-message" role="status">{message}</p>}
    <label className="history-search"><span>查找历史</span><input type="search" placeholder="按文件名或模型查找" value={query} onChange={(event) => { setQuery(event.target.value); setLimit(30); }} /></label>
    {historyError && <p className="notice" role="alert">{historyError}</p>}
    {!history.length && !historyError && <p className="provider-hint">尚无匹配记录。翻译或提取 TXT 后会自动登记。</p>}
    <div className="history-list">{history.map((item) => <article className={item.id === job?.id ? "history-item is-selected" : "history-item"} key={item.id}>
      <div className="history-description"><strong title={item.filename}>{item.filename}</strong><p>{new Date(item.created_at * 1000).toLocaleString("zh-CN")} · {item.page_count} 页 · {item.kind === "extraction" ? "原文提取（不调用 API）" : item.model || "旧版记录"}</p><p>{names[item.status] ?? item.status} · {elapsed(item.elapsed_seconds)}{item.cache_hit ? " · 复用已验证结果" : ""}{item.warnings?.length ? ` · ${item.warnings.length} 条提示` : ""}</p></div>
      <div className="history-actions"><button className="text-action" type="button" onClick={() => onSelect(item)}>查看任务</button>{item.status === "completed" && item.artifacts.length > 0 && <><button className="save-config" type="button" disabled={saving !== null} onClick={() => void saveJob(item.id)}>{saving === item.id ? "保存中…" : "保存结果"}</button><button className="text-action" type="button" onClick={() => void invoke("openJobFolder", item.id)}>结果文件夹</button>{item.kind === "translation" && !item.artifacts.some((artifact) => artifact.kind === "translated_txt") && <button className="text-action" type="button" disabled={textBusy !== null} onClick={() => void prepareText(item.id)}>{textBusy === item.id ? "提取中…" : "补充 TXT（不调用 API）"}</button>}</>}</div>
    </article>)}</div>
    {history.length < total && limit < 100 && <button className="save-config" type="button" onClick={() => setLimit((value) => value + 30)}>显示更多记录（共 {total} 条）</button>}
    {total > 100 && limit >= 100 && <p className="provider-hint">显示最近 100 条匹配记录；可输入文件名或模型缩小范围。</p>}
  </section>;
}
