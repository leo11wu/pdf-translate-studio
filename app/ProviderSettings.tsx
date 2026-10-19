"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { nativeProfile } from "./desktop-bridge";

type Props = {
  baseUrl: string; model: string; apiKey: string; qps: number;
  keyAvailable: boolean; disabled: boolean;
  setBaseUrl: (value: string) => void; setModel: (value: string) => void;
  setApiKey: (value: string) => void; setQps: (value: number) => void;
  setKeyAvailable: (value: boolean) => void;
};

export default function ProviderSettings(props: Props) {
  const { baseUrl, model, apiKey, qps, keyAvailable, disabled,
    setBaseUrl, setModel, setApiKey, setQps, setKeyAvailable } = props;
  const [remember, setRemember] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [modelsStatus, setModelsStatus] = useState("");
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let alive = true;
    nativeProfile("load").then((profile) => {
      if (!alive) return;
      setBaseUrl(profile.base_url);
      setModel(profile.model);
      setQps(profile.qps);
      setKeyAvailable(profile.key_available);
      setRemember(profile.remembered);
      if (profile.remembered) setStatus("已从 macOS 钥匙串读取密钥。");
    }).catch((error: Error) => { if (alive) setStatus(error.message); });
    return () => { alive = false; requestRef.current?.abort(); };
  }, [setBaseUrl, setModel, setQps, setKeyAvailable]);

  const loadModels = useCallback(async () => {
    requestRef.current?.abort();
    if (!baseUrl.trim() || (!apiKey.trim() && !keyAvailable)) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setModelsStatus("");
    try {
      const response = await fetch("/api/provider/models", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl.trim(), api_key: apiKey.trim() }), signal: controller.signal,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "获取模型列表失败，可手动填写。");
      const available = body.models as string[];
      if (controller.signal.aborted) return;
      setModels(available);
      setModelsStatus(`已获取 ${available.length} 个模型，请选择支持文本对话的模型。`);
    } catch (error) {
      if (!controller.signal.aborted) {
        setModels([]);
        setModelsStatus(error instanceof Error ? error.message : "获取失败，可手动填写模型名称。");
      }
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [baseUrl, apiKey, keyAvailable]);

  useEffect(() => {
    const timer = setTimeout(() => { void loadModels(); }, 900);
    return () => { clearTimeout(timer); requestRef.current?.abort(); };
  }, [loadModels]);

  const save = async () => {
    setSaving(true);
    try {
      const result = await nativeProfile("save", {
        base_url: baseUrl, model, api_key: apiKey, qps, remember,
      });
      setBaseUrl(result.base_url);
      setKeyAvailable(result.key_available);
      setApiKey("");
      setStatus(result.remembered ? "配置已保存，密钥安全存放在 macOS 钥匙串。" : "配置已应用；密钥仅保留在本次 App 会话中。");
    } catch (error) { setStatus(error instanceof Error ? error.message : "保存配置失败。"); }
    finally { setSaving(false); }
  };

  const forget = async () => {
    setSaving(true);
    try {
      await nativeProfile("forget");
      setKeyAvailable(false);
      setApiKey("");
      setRemember(false);
      setModels([]);
      setStatus("已删除钥匙串记录并清除本次会话的密钥。");
    } catch (error) { setStatus(error instanceof Error ? error.message : "删除失败。"); }
    finally { setSaving(false); }
  };

  const changeUrl = (value: string) => {
    setBaseUrl(value);
    setApiKey("");
    setKeyAvailable(false);
    setModels([]);
    setModelsStatus("");
    setStatus("地址已更改，请为此地址填写密钥。");
  };

  return <div className="provider-settings">
    <div className="field-grid">
      <label className="field full-field"><span>API 基础地址</span>
        <input disabled={disabled || saving} value={baseUrl} onChange={(event) => changeUrl(event.target.value)}
          placeholder="https://你的供应商/v1" spellCheck={false} autoCapitalize="none" />
      </label>
      <label className="field full-field"><span>API 密钥 {keyAvailable && <small>本次会话已就绪</small>}</span>
        <input disabled={disabled || saving} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)}
          placeholder={keyAvailable ? "留空继续使用已配置的密钥" : "填写此供应商的 API Key"} autoComplete="off" spellCheck={false} />
      </label>
      <div className="field full-field"><div className="model-field-heading"><label htmlFor="provider-models">供应商模型列表</label>
        <button type="button" className="text-action" disabled={disabled || loading || (!apiKey.trim() && !keyAvailable)} onClick={() => void loadModels()}>{loading ? "正在获取…" : "刷新列表"}</button>
      </div>
        <select id="provider-models" disabled={disabled || !models.length} value={models.includes(model) ? model : ""} onChange={(event) => setModel(event.target.value)}>
          <option value="">{models.length ? "从列表选择模型，或在下方手动输入" : "填写地址和密钥后自动获取"}</option>
          {models.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </div>
      <label className="field full-field"><span>模型名称</span>
        <input disabled={disabled || saving} value={model} onChange={(event) => setModel(event.target.value)} placeholder="选择模型后自动填写，也可手动输入" spellCheck={false} />
      </label>
    </div>
    {modelsStatus && <p className="provider-hint" aria-live="polite">{modelsStatus}</p>}
    <label className="remember-control"><input type="checkbox" checked={remember} disabled={disabled || saving} onChange={(event) => setRemember(event.target.checked)} />安全记住 API 密钥（macOS 钥匙串）</label>
    <div className="provider-actions">
      <button className="save-config" type="button" disabled={disabled || saving || !model.trim() || !baseUrl.trim() || (!apiKey.trim() && !keyAvailable)} onClick={() => void save()}>{saving ? "正在处理…" : "保存配置"}</button>
      <button className="text-action" type="button" disabled={disabled || saving} onClick={() => void forget()}>忘记密钥</button>
    </div>
    {status && <p className="provider-hint" role="status">{status}</p>}
  </div>;
}
