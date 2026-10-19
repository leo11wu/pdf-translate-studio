"use client";

import { ChangeEvent, DragEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import ProviderSettings from "./ProviderSettings";
import AssetStatus from "./AssetStatus";
import OutputLibrary from "./OutputLibrary";
import { elapsed, type TranslationJob } from "./job-types";

const workflow = [
  ["解析原稿", "识别页面、文字块、图表、公式与脚注"],
  ["逐段译制", "自动判断每段语言，统一译为简体中文"],
  ["原位回填", "尽量保留原有坐标、字体和版面关系"],
  ["汇总输出", "长文自动分段处理后合并为一份 PDF"],
];

type ConfigStatus = {
  available: boolean;
  model: string | null;
  base_url: string | null;
  qps?: number | null;
  table_text_translation_supported?: boolean;
};

type JobEvent = { type: string; [key: string]: unknown };

function numberFrom(value: unknown, fallback = 0) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stageIndex(stage: string, completed: boolean) {
  if (completed) return workflow.length;
  const normalized = stage.toLowerCase();
  if (/save|merge|合并|保存|输出/.test(normalized)) return 3;
  if (/render|generate|回填|排版/.test(normalized)) return 2;
  if (/translate|translation|翻译|译制|paragraph/.test(normalized)) return 1;
  return 0;
}

function isLocalWorkerEndpoint(endpoint: string) {
  try {
    const url = new URL(endpoint);
    return ["http:", "https:"].includes(url.protocol) && ["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}

function normalizeApiBaseUrl(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return trimmed;
  try {
    const url = new URL(trimmed);
    let pathname = url.pathname.replace(/\/+$/, "");
    if (pathname.endsWith("/chat/completions")) pathname = pathname.slice(0, -"/chat/completions".length).replace(/\/+$/, "");
    url.pathname = pathname || "/v1";
    return url.toString().replace(/\/$/, "");
  } catch {
    return trimmed.replace(/\/+$/, "").replace(/\/chat\/completions$/, "");
  }
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

export default function Home({ desktop = false }: { desktop?: boolean } = {}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const streamRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [apiBaseUrl, setApiBaseUrl] = useState("https://api.openai.com/v1");
  const [model, setModel] = useState("gpt-4o-mini");
  const [apiKey, setApiKey] = useState("");
  const [workerUrl, setWorkerUrl] = useState(() => desktop ? window.location.origin : "http://127.0.0.1:8789");
  const [partPages, setPartPages] = useState(50);
  const [qps, setQps] = useState(desktop ? 8 : 32);
  const [tableText, setTableText] = useState(false);
  const [tableTextSupported, setTableTextSupported] = useState(false);
  const [ocrAssist, setOcrAssist] = useState(false);
  const [generateDual, setGenerateDual] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [notice, setNotice] = useState("");
  const [configLoaded, setConfigLoaded] = useState(false);
  const [configModel, setConfigModel] = useState<string | null>(null);
  const [job, setJob] = useState<TranslationJob | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [hasSessionKey, setHasSessionKey] = useState(false);
  const [assetsReady, setAssetsReady] = useState(!desktop);
  const [exportTexts, setExportTexts] = useState(true);
  const [useCache, setUseCache] = useState(true);
  const [autoSaveJobId, setAutoSaveJobId] = useState<string | null>(null);
  const ready = Boolean(
    file && assetsReady &&
    (configLoaded || (apiBaseUrl.trim() && model.trim() && (apiKey.trim() || hasSessionKey)))
  );

  useEffect(() => {
    if (desktop) return;
    const endpoint = workerUrl.trim().replace(/\/$/, "");
    if (!isLocalWorkerEndpoint(endpoint)) return;
    const timer = setTimeout(() => {
      fetch(`${endpoint}/api/config-status`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data: ConfigStatus | null) => {
          setTableTextSupported(Boolean(data?.table_text_translation_supported));
          if (data?.available) {
            if (data.base_url) setApiBaseUrl(data.base_url);
            if (data.model) {
              setModel(data.model);
              setConfigModel(data.model);
            }
            if (typeof data.qps === "number" && Number.isFinite(data.qps)) setQps(Math.max(1, Math.min(64, data.qps)));
          }
          setConfigLoaded(Boolean(data?.available));
        })
        .catch(() => setConfigLoaded(false));
    }, 250);
    return () => clearTimeout(timer);
  }, [workerUrl, desktop]);
  const jobIsActive = Boolean(job && ["queued", "running", "cancelling"].includes(job.status));
  const jobIsComplete = job?.status === "completed";
  const activeStep = stageIndex(job?.stage ?? "", Boolean(jobIsComplete));

  useEffect(() => () => {
    eventSourceRef.current?.close();
    if (streamRetryRef.current) clearTimeout(streamRetryRef.current);
  }, []);

  const acceptFile = (candidate?: File) => {
    if (jobIsActive) return;
    if (!candidate) return;
    const isPdf = candidate.type === "application/pdf" || candidate.name.toLowerCase().endsWith(".pdf");
    if (!isPdf) {
      setNotice("请选择 PDF 文件。其余格式不会送入译制流程。");
      return;
    }
    setFile(candidate);
    setJob(null);
    setAutoSaveJobId(null);
    setNotice("");
  };

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => acceptFile(event.target.files?.[0]);
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (jobIsActive) return;
    acceptFile(event.dataTransfer.files?.[0]);
  };
  const onUploadKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (jobIsActive) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInputRef.current?.click();
    }
  };
  const workerEndpoint = () => workerUrl.trim().replace(/\/$/, "");

  const applyStreamEvent = (event: JobEvent) => {
    setJob((current) => {
      if (!current) return current;
      const next = { ...current };
      if (event.type === "started") {
        next.status = "running";
        next.stage = String(event.stage ?? "正在解析 PDF");
      }
      if (event.type === "progress") {
        next.status = "running";
        next.stage = String(event.stage ?? next.stage);
        next.progress = numberFrom(event.progress, next.progress);
        next.current_part = numberFrom(event.part_index, next.current_part);
        next.total_parts = numberFrom(event.total_parts, next.total_parts);
      }
      if (event.type === "stage_summary") {
        next.total_parts = numberFrom(event.total_parts, next.total_parts);
      }
      if (event.type === "completed") {
        Object.assign(next, event);
        next.status = "completed";
        next.stage = String(event.stage ?? "已合并为一份中文 PDF");
        next.progress = 100;
        if (typeof event.download_ready === "boolean") next.download_ready = event.download_ready;
        if (typeof event.dual_download_ready === "boolean") next.dual_download_ready = event.dual_download_ready;
        if (event.link_stats && typeof event.link_stats === "object") next.link_stats = event.link_stats as Record<string, unknown>;
        if (Array.isArray(event.warnings)) next.warnings = event.warnings.filter((warning): warning is string => typeof warning === "string");
      }
      if (event.type === "cancelled") {
        next.status = "cancelled";
        next.stage = String(event.stage ?? "任务已取消");
      }
      if (event.type === "error") {
        next.status = "failed";
        next.stage = "译制失败";
      }
      return next;
    });
    if (event.type === "completed") {
      const warnings = Array.isArray(event.warnings) ? event.warnings.filter((value): value is string => typeof value === "string") : [];
      const result = event.kind === "extraction" ? "原文 TXT 已提取，未调用 API。" : event.cache_hit ? "已复用相同文件、模型与设置的验证结果。" : "译制完成，PDF 与所选 TXT 已准备好。";
      setNotice(`${result}${warnings.join("；")}`);
    }
    if (event.type === "cancelled") {
      setNotice("任务已取消；已生成的临时文件会由本地服务自动清理。");
    }
    if (event.type === "error") {
      setNotice(String(event.message ?? "译制服务返回了错误。请检查 PDF 与接口设置后重试。"));
    }
  };

  const openProgressStream = (jobId: string, endpoint: string) => {
    if (streamRetryRef.current) {
      clearTimeout(streamRetryRef.current);
      streamRetryRef.current = null;
    }
    eventSourceRef.current?.close();
    const source = new EventSource(`${endpoint}/api/jobs/${encodeURIComponent(jobId)}/events`);
    eventSourceRef.current = source;
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as JobEvent;
        applyStreamEvent(event);
        if (["completed", "cancelled", "error"].includes(event.type)) {
          source.close();
          eventSourceRef.current = null;
        }
      } catch {
        setNotice("进度信息无法读取，但译制服务仍可能在继续运行。");
      }
    };
    source.onerror = () => {
      if (eventSourceRef.current !== source) return;
      // EventSource automatically retries transient failures.  Only schedule
      // an explicit retry after the browser reports a terminal close.
      setNotice("进度连接暂时中断，正在自动重连；译制任务仍在本地继续。" );
      if (source.readyState !== EventSource.CLOSED || streamRetryRef.current) return;
      source.close();
      eventSourceRef.current = null;
      const retry = () => {
        streamRetryRef.current = null;
        fetch(`${endpoint}/api/jobs/${encodeURIComponent(jobId)}`)
          .then((response) => response.ok ? response.json() as Promise<TranslationJob> : Promise.reject(new Error("status")))
          .then((snapshot) => {
            setJob(snapshot);
            if (["completed", "cancelled", "failed"].includes(snapshot.status)) return;
            openProgressStream(jobId, endpoint);
          })
          .catch(() => {
            streamRetryRef.current = setTimeout(retry, 1500);
          });
      };
      streamRetryRef.current = setTimeout(retry, 1000);
    };
  };

  const startTranslation = async (extractOnly = false) => {
    if (extractOnly ? !file : !ready) {
      setNotice("请先选择 PDF，并完整填写接口地址、模型名称和 API 密钥。");
      return;
    }
    const endpoint = workerEndpoint();
    if (!endpoint) {
      setNotice("请填写本地译制服务地址。");
      return;
    }
    if (!isLocalWorkerEndpoint(endpoint)) {
      setNotice("为保护 API 密钥，译制服务地址只能是本机 localhost、127.0.0.1 或 ::1。");
      return;
    }
    setIsStarting(true);
    setNotice(extractOnly ? "正在读取 PDF 文本层，不调用 API…" : "正在上传原稿并进行页数预检…");
    try {
      const form = new FormData();
      form.append("file", file as File);
      if (!extractOnly) {
        if (apiBaseUrl.trim()) form.append("api_base_url", normalizeApiBaseUrl(apiBaseUrl));
        if (apiKey.trim()) form.append("api_key", apiKey.trim());
        if (model.trim()) form.append("model", model.trim());
      }
      form.append("qps", String(qps));
      form.append("part_pages", String(partPages));
      form.append("translate_table_text", String(tableText && tableTextSupported));
      form.append("ocr_assist", String(ocrAssist));
      form.append("generate_dual", String(generateDual));
      form.append("export_texts", String(exportTexts));
      form.append("use_cache", String(useCache));
      form.append("extract_only", String(extractOnly));
      const response = await fetch(`${endpoint}/api/jobs`, { method: "POST", body: form });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof body?.detail === "string" ? body.detail : "无法创建译制任务");
      }
      const created = body as TranslationJob;
      setJob(created);
      setAutoSaveJobId(created.id);
      if (!extractOnly) setApiKey("");
      setNotice(extractOnly ? `正在提取 ${created.page_count} 页原文…` : created.part_pages ? `已预检 ${created.page_count} 页，将分为约 ${created.planned_parts} 部分处理并自动合并。` : `已预检 ${created.page_count} 页，正在启动译制。`);
      openProgressStream(created.id, endpoint);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "无法连接本地译制服务";
      setNotice(`无法开始译制：${detail}`);
      setJob(null);
    } finally {
      setIsStarting(false);
    }
  };

  const cancelTranslation = async () => {
    if (!job || !jobIsActive) return;
    try {
      const response = await fetch(`${workerEndpoint()}/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
      if (!response.ok) throw new Error("cancel failed");
      setJob((current) => current ? { ...current, status: "cancelling", stage: "正在取消" } : current);
      setNotice("正在取消任务；当前页面会在服务确认后更新状态。");
    } catch {
      setNotice("取消请求未能送达本地服务。请稍后再试。" );
    }
  };

  return (
    <main className={desktop ? "studio-shell desktop-mode" : "studio-shell"} data-job-active={jobIsActive}>
      <div className="halo halo-one" aria-hidden="true" />
      <div className="halo halo-two" aria-hidden="true" />
      <header className="topbar content-width">
        <a className="brand" href="#top" aria-label="译制台首页">
          <span className="brand-mark">译</span>
          <span><strong>纸间</strong><small>PDF 译制台</small></span>
        </a>
        <div className="topbar-meta">{desktop && <a className="text-action" href="#library">输出与历史</a>}<span className={jobIsActive ? "local-badge is-active" : "local-badge"}><i /> {jobIsActive ? "正在处理" : jobIsComplete ? job?.kind === "extraction" ? "提取完成" : "译制完成" : "本地译制"}</span><span>{desktop ? "钥匙串可选 · 中文输出" : "密钥不保存 · 中文输出"}</span></div>
      </header>

      <section className="hero content-width" id="top">
        {desktop ? <><h1>PDF 译制台</h1><p>保留版式，译为中文。PDF、原文与译文 TXT 自动归档，随时从历史取回。</p></> : <>
        <div className="eyebrow">LAYOUT-PRESERVING TRANSLATION</div>
        <h1>让一本 PDF <em>用中文重新排版</em><br />而不是重新做成一本书。</h1>
        <p>针对学术书、报告与复杂版式文档。系统逐段识别原文语言，统一翻译为简体中文，并尽可能原位保留公式、图表、脚注和双栏结构。</p>
        <div className="hero-pills" aria-label="核心能力"><span>源语言自动识别</span><span>OpenAI 兼容接口</span><span>长文自动分段</span><span>单一 PDF 输出</span></div>
        </>}
      </section>

      {desktop && <AssetStatus onReady={setAssetsReady} />}
      <section className="workspace content-width" aria-label="PDF 翻译工作区">
        <aside className="setup-card card">
          <div className="card-heading">
            <div><span className="section-kicker">01 / 配置</span><h2>译制参数</h2></div>
            <span className="lock-note">{desktop ? "密钥可安全存入钥匙串" : "密钥仅在本机内存中使用"}</span>
          </div>
          {desktop ? <ProviderSettings baseUrl={apiBaseUrl} model={model} apiKey={apiKey} qps={qps}
            keyAvailable={hasSessionKey} disabled={jobIsActive || isStarting} setBaseUrl={setApiBaseUrl}
            setModel={setModel} setApiKey={setApiKey} setQps={setQps} setKeyAvailable={setHasSessionKey} /> : configLoaded ? (
            <div className="language-route" style={{gridTemplateColumns: "1fr", textAlign: "center"}}>
              <div><span>API 配置</span><strong>✓ 已从配置文件读取</strong><small>{configModel ? `模型: ${configModel}` : "已就绪"}</small></div>
            </div>
          ) : (
            <>
              <div className="language-route">
                <div><span>源语言</span><strong>自动识别</strong><small>可混合多种语言</small></div>
                <span className="route-arrow" aria-hidden="true">→</span>
                <div><span>目的语</span><strong>简体中文</strong><small>固定输出</small></div>
              </div>
              <div className="field-grid">
                <label className="field full-field"><span>API 基础地址</span><input disabled={jobIsActive} value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" spellCheck="false" /></label>
                <label className="field"><span>模型名称</span><input disabled={jobIsActive} value={model} onChange={(event) => setModel(event.target.value)} placeholder="例如 gpt-4o-mini" spellCheck="false" /></label>
                <label className="field">
                  <span>API 密钥</span>
                  <input
                    disabled={jobIsActive}
                    type="password"
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    placeholder="sk-…"
                    autoComplete="off"
                  />
                </label>
              </div>
            </>
          )}
          <button type="button" className="advanced-trigger" onClick={() => setShowAdvanced((value) => !value)} aria-expanded={showAdvanced}>
            <span>{desktop ? "高级译制设置" : "本地服务与长文策略"}</span><span className={showAdvanced ? "chevron is-open" : "chevron"}>⌄</span>
          </button>
          {showAdvanced && (
            <div className="advanced-panel">
              {!desktop && <label className="field full-field"><span>本地译制服务地址</span><input disabled={jobIsActive} value={workerUrl} onChange={(event) => setWorkerUrl(event.target.value)} placeholder="http://127.0.0.1:8789" spellCheck="false" /></label>}
              <label className="range-field"><span>请求速率 / 并发上限 <strong>{qps}</strong></span><input disabled={jobIsActive} type="range" min="1" max="64" step="1" value={qps} onChange={(event) => setQps(Number(event.target.value))} /><small>按模型端点的速率限制调节；过高可能触发限流。</small></label>
              <div className="speed-presets" aria-label="并发预设">{[[4, "稳妥 4"], [8, "标准 8"], [16, "快速 16"]].map(([value, label]) => <button className={qps === value ? "save-config is-selected" : "save-config"} type="button" disabled={jobIsActive} key={value} onClick={() => setQps(Number(value))}>{label}</button>)}</div>
              <label className="remember-control"><input type="checkbox" checked={useCache} disabled={jobIsActive} onChange={(event) => setUseCache(event.target.checked)} />复用相同文件、接口、模型与设置的已验证结果</label>
                <label className="range-field"><span>每部分最多页数 <strong>{partPages} 页</strong></span><input disabled={jobIsActive} type="range" min="50" max="150" step="10" value={partPages} onChange={(event) => setPartPages(Number(event.target.value))} /><small>超过 80 页时自动启用；完成后自动合并。</small></label>
              <div className="toggle-stack">
                {tableTextSupported ? (
                  <div className="toggle-row"><span><strong>翻译表格内文本</strong><small>实验性功能；开启后会增加表格识别时间</small></span><button type="button" className={tableText ? "toggle-control is-on" : "toggle-control"} role="switch" aria-label="翻译表格内文本" aria-checked={tableText} disabled={jobIsActive} onClick={() => setTableText((value) => !value)}><i aria-hidden="true" /></button></div>
                ) : (
                  <div className="toggle-row is-disabled"><span><strong>表格文字翻译</strong><small>当前引擎暂不支持；表格结构与位置仍会保留</small></span><span className="feature-status">待支持</span></div>
                )}
                <div className="toggle-row"><span><strong>生成中英对照版</strong><small>按需生成；会增加一次版式渲染与合并时间</small></span><button type="button" className={generateDual ? "toggle-control is-on" : "toggle-control"} role="switch" aria-label="生成中英对照版" aria-checked={generateDual} disabled={jobIsActive} onClick={() => setGenerateDual((value) => !value)}><i aria-hidden="true" /></button></div>
                <div className="toggle-row"><span><strong>扫描件版式辅助</strong><small>按需开启；会增加扫描 PDF 的预处理时间</small></span><button type="button" className={ocrAssist ? "toggle-control is-on" : "toggle-control"} role="switch" aria-label="扫描件版式辅助" aria-checked={ocrAssist} disabled={jobIsActive} onClick={() => setOcrAssist((value) => !value)}><i aria-hidden="true" /></button></div>
              </div>
            </div>
          )}
        </aside>

        <section className="job-card card">
          <div className="card-heading job-heading"><div><span className="section-kicker">02 / 原稿</span><h2>放入需要译制的 PDF</h2></div><span className="pdf-tag">.PDF</span></div>
          <div className={jobIsActive ? "upload-zone is-busy" : dragging ? "upload-zone is-dragging" : "upload-zone"} role="button" tabIndex={jobIsActive ? -1 : 0} onClick={() => { if (!jobIsActive) fileInputRef.current?.click(); }} onKeyDown={onUploadKeyDown} onDragEnter={(event) => { event.preventDefault(); if (!jobIsActive) setDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={onDrop} aria-disabled={jobIsActive} aria-label="选择或拖放 PDF 文件">
            <input ref={fileInputRef} disabled={jobIsActive} type="file" accept="application/pdf,.pdf" onChange={onFileChange} />
            <div className="paper-icon" aria-hidden="true"><span>PDF</span></div>
            <strong>{jobIsActive ? "原稿正在译制中" : file ? "已选中原稿" : "拖入 PDF，或点击选择"}</strong>
            <p>{jobIsActive ? "任务进行中，暂不能替换文件。" : file ? "可以替换为另一份 PDF；当前设置会保留。" : "支持文本型与扫描型 PDF。大文件将在本机分段处理。"}</p>
          </div>
          {file && <div className="file-row"><div className="file-chip" aria-hidden="true">PDF</div><div className="file-name"><strong>{file.name}</strong><span>{formatBytes(file.size)} · {job ? `${job.page_count} 页` : "等待预检"}</span></div><button disabled={jobIsActive} type="button" onClick={() => setFile(null)} aria-label="移除已选 PDF">移除</button></div>}
          <div className="plan-strip"><div><span>源语言</span><strong>逐段自动判断</strong></div><div><span>长文策略</span><strong>{job?.part_pages ? `约 ${job.planned_parts} 部分自动合并` : "自动切分与合并"}</strong></div><div><span>交付文件</span><strong>{generateDual ? "中文 PDF + 中英对照" : "中文 PDF"}{exportTexts ? " + TXT" : ""}</strong></div></div>
          <label className="remember-control export-texts"><input type="checkbox" checked={exportTexts} disabled={jobIsActive || isStarting} onChange={(event) => setExportTexts(event.target.checked)} />同时导出原文 TXT 和译文 TXT</label>
          <p className="provider-hint">TXT 从 PDF 文本层提取，保留分页和换行。扫描页可能无文字；用于研究前请核对读取顺序、连字符与公式。</p>
          {notice && <p className="notice" role="status">{notice}</p>}
          {job && <div className="live-progress"><div><span>{job.stage}</span><strong>{Math.round(job.progress)}%</strong></div><progress aria-label="当前任务进度" value={job.progress} max={100} /></div>}
          {jobIsComplete && job.warnings?.map((warning) => <p className="notice" role="status" key={warning}>{warning}</p>)}
          <button type="button" className="primary-action" onClick={() => void startTranslation()} disabled={!ready || jobIsActive || isStarting}><span>{isStarting ? "正在创建任务…" : jobIsActive ? "处理进行中" : jobIsComplete ? "再次开始译制" : "开始译制"}</span><span aria-hidden="true">↗</span></button>
          <button type="button" className="secondary-action extract-action" disabled={!file || jobIsActive || isStarting} onClick={() => void startTranslation(true)}>仅提取原文 TXT（不调用 API）</button>
          {jobIsActive && job?.status !== "cancelling" && <button type="button" className="secondary-action" onClick={cancelTranslation}>取消当前译制</button>}
          {job && jobIsComplete && (
            <div className="download-row">
              <p className="result-summary">{job.filename} · {job.page_count} 页 · {elapsed(job.elapsed_seconds)}{job.cache_hit ? " · 结果复用" : ""}</p>
              <p className="provider-hint">{job.kind === "extraction" ? "纯本地提取 · 未调用 API" : `${job.model || "旧版模型信息未知"} · 并发 ${job.qps}${job.options?.generate_dual ? " · 含对照版" : ""}${job.options?.ocr_assist ? " · 扫描件辅助" : ""}`}</p>
              {job.artifacts?.map((artifact) => <a className="download-action" download={artifact.filename} key={artifact.kind} href={`${workerEndpoint()}${artifact.route}`}>{({ pdf: "中文译文 PDF", dual: "中英对照 PDF", source_txt: "原文 TXT", translated_txt: "译文 TXT" } as Record<string, string>)[artifact.kind] ?? artifact.kind} <span aria-hidden="true">另存 ↓</span></a>)}
              {job.stage_seconds && Object.keys(job.stage_seconds).length > 0 && <details className="timing-details"><summary>耗时明细</summary>{Object.entries(job.stage_seconds).map(([stage, seconds]) => <p key={stage}>{stage} <strong>{elapsed(seconds)}</strong></p>)}</details>}
            </div>
          )}
          {!ready && !jobIsActive && !isStarting && (
            <p className="action-hint">
              {desktop ? (assetsReady ? "选择 PDF 并配置 API 后即可开始。" : "模型与字体准备完成后即可开始译制。") : configLoaded
                ? "已从 backend/api_config.txt 读取配置。选择 PDF 即可开始。"
                : "选择 PDF 并填写 API 后即可开始，或在 backend/api_config.txt 中预设配置。"}
            </p>
          )}
        </section>
      </section>

      {desktop && <OutputLibrary endpoint={workerEndpoint()} job={job} autoSaveJobId={autoSaveJobId} onSelect={(selected) => {
        if (jobIsActive && selected.id !== job?.id) { setNotice("当前任务仍在运行，请完成或取消后再切换历史记录。"); return; }
        setJob(selected); setFile(null); if (fileInputRef.current) fileInputRef.current.value = "";
        setNotice(`${selected.filename} · ${selected.stage}${selected.error_message ? `：${selected.error_message}` : ""}`);
        if (["queued", "running", "cancelling"].includes(selected.status)) openProgressStream(selected.id, workerEndpoint());
      }} />}

      <section className="progress-section content-width">
        <div className="progress-copy"><span className="section-kicker">03 / 过程可见</span><h2>不是黑箱等待，<br />每一步都会说明现在在做什么。</h2><p>译制服务会把解析、翻译、回填和合并进度持续发回页面。大型文件会同时显示当前分段与总体完成比例。</p></div>
        <div className="workflow-list card" aria-label="译制流程">
          {(job?.kind === "extraction" ? [["提取文本层", "本地读取原文，保留分页，不调用 API"]] : workflow).map(([title, detail], index) => {
            const rowState = jobIsComplete || index < activeStep ? "完成" : jobIsActive && index === activeStep ? "进行中" : "等待";
            return <div className={rowState === "进行中" ? "workflow-row is-current" : "workflow-row"} key={title}><span className="workflow-number">0{index + 1}</span><div><strong>{title}</strong><p>{detail}</p></div><span className="workflow-state">{rowState}</span></div>;
          })}
          <div className="progress-footer"><span><i /> {jobIsActive ? `${job?.stage ?? "正在译制"}${(job?.total_parts ?? 1) > 1 ? ` · 第 ${Math.max(1, job?.current_part ?? 1)}/${job?.total_parts} 部分` : ""}` : jobIsComplete ? "译制完成，可下载" : job?.status === "failed" ? "译制失败" : job?.status === "cancelled" ? "任务已取消" : "尚未开始"}</span><strong>{Math.round(job?.progress ?? 0)}%</strong></div>
        </div>
      </section>

      <section className="principles content-width" aria-label="工作方式">
        <article><span>01</span><h3>混合语言无需手设</h3><p>翻译提示词要求模型按段判断德语、英语或其他自然语言，再统一转为中文。</p></article>
        <article><span>02</span><h3>结构优先于重排</h3><p>公式、图形与页面对象优先保护；文字在原版面坐标内适配，而非重新生成文档。</p></article>
        <article><span>03</span><h3>大书也按需交付</h3><p>处理端自动拆分长 PDF，默认汇总纯译文；需要核对时再生成中英对照版。</p></article>
      </section>
      <footer className="footer content-width"><span>纸间 / 本地 PDF 译制台</span><span>复杂 PDF 的版式保留程度取决于原文件结构；建议先试译典型页面。</span></footer>
    </main>
  );
}
