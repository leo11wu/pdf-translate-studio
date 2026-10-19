"use client";

import { useCallback, useEffect, useState } from "react";

type Assets = { state: string; message: string; completed: number; total: number; bytes: number; total_bytes: number };

export default function AssetStatus({ onReady }: { onReady: (value: boolean) => void }) {
  const [assets, setAssets] = useState<Assets | null>(null);
  const [error, setError] = useState("");
  const prepare = useCallback(async () => {
    setError("");
    try {
      const response = await fetch("/api/desktop/assets", { method: "POST" });
      if (!response.ok) throw new Error("无法连接资源准备服务。");
      setAssets(await response.json() as Assets);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "准备失败，请重试。"); }
  }, []);
  useEffect(() => {
    let alive = true;
    const start = setTimeout(() => { void prepare(); }, 0);
    const poll = async () => {
      try {
        const response = await fetch("/api/desktop/assets");
        if (!response.ok) throw new Error("无法读取资源状态。");
        const next = await response.json() as Assets;
        if (alive) {
          setAssets(next); onReady(next.state === "ready"); setError("");
          if (next.state === "ready") clearInterval(interval);
        }
      } catch { if (alive) setError("本地服务暂时无法连接，请重试或重新打开 App。"); }
    };
    const interval = setInterval(() => { void poll(); }, 1500);
    return () => { alive = false; clearTimeout(start); clearInterval(interval); };
  }, [onReady, prepare]);
  const progress = assets ? Math.min(100, assets.bytes / Math.max(1, assets.total_bytes) * 100) : 0;
  return <div className={`asset-status content-width ${assets?.state === "ready" ? "is-ready" : ""}`} role="status">
    <div><strong>{error || assets?.message || "正在检查版式模型与字体…"}</strong>
      {assets && assets.state !== "ready" && <small>{assets.completed} / {assets.total} 个文件 · {(assets.bytes / 1048576).toFixed(1)} / {(assets.total_bytes / 1048576).toFixed(1)} MB</small>}
    </div>
    {assets?.state !== "ready" && <progress value={progress} max={100} aria-label="模型与字体准备进度" />}
    {(error || assets?.state === "error") && <button type="button" className="save-config" onClick={() => void prepare()}>重试</button>}
  </div>;
}
