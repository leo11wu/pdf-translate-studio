"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { nativeProfile, defaultRuntime, type NativeProfile, type RuntimeOptions } from "./desktop-bridge";

type Props = {
  baseUrl: string; model: string; apiKey: string; qps: number;
  keyAvailable: boolean; disabled: boolean;
  runtime: RuntimeOptions; setRuntime: (value: RuntimeOptions) => void;
  setBaseUrl: (value: string) => void; setModel: (value: string) => void;
  setApiKey: (value: string) => void; setQps: (value: number) => void;
  setKeyAvailable: (value: boolean) => void;
};

export default function ProviderSettings(props: Props) {
  const { baseUrl, model, apiKey, qps, keyAvailable, disabled,
    setBaseUrl, setModel, setApiKey, setQps, setKeyAvailable, runtime, setRuntime } = props;
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [remember, setRemember] = useState(true);
  const [profiles, setProfiles] = useState<NativeProfile["profiles"]>([]);
  const [profileId, setProfileId] = useState("");
  const [profileName, setProfileName] = useState("");
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const savedRef = useRef("");
  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("正在从 macOS 钥匙串恢复配置…");
  const [restoring, setRestoring] = useState(true);
  const [modelsStatus, setModelsStatus] = useState("");
  const requestRef = useRef<AbortController | null>(null);

  const applyProfile = useCallback((profile: NativeProfile) => {
    requestRef.current?.abort();
    setBaseUrl(profile.base_url); setModel(profile.model); setQps(profile.qps);
    setKeyAvailable(profile.key_available); setApiKey(""); setModels([]);
    setProfiles(profile.profiles ?? []); setProfileId(profile.id ?? ""); setProfileName(profile.name ?? "");
    const nextRuntime = profile.runtime ?? defaultRuntime;
    setRuntime(nextRuntime);
    const nextRemember = profile.id ? profile.remembered : true;
    setRemember(nextRemember);
    setConnectionOpen(!profile.key_available);
    savedRef.current = JSON.stringify([profile.base_url, profile.model, profile.qps, nextRuntime, profile.name ?? "", nextRemember]);
    setStatus(profile.key_available ? "配置已切换，密钥已就绪。" : "请填写此配置的 API 密钥。");
  }, [setBaseUrl, setModel, setQps, setKeyAvailable, setApiKey, setRuntime]);

  useEffect(() => {
    let alive = true;
    nativeProfile("load").then((profile) => {
      if (!alive) return;
      applyProfile(profile);
      setStatus(profile.remembered ? "已从 macOS 钥匙串读取密钥。" : "请配置本次使用的 API。");
    }).catch((error: Error) => { if (alive) setStatus(error.message); })
      .finally(() => { if (alive) setRestoring(false); });
    return () => { alive = false; requestRef.current?.abort(); };
  }, [applyProfile]);

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

  const save = async (): Promise<boolean> => {
    setSaving(true);
    try {
      const result = await nativeProfile("save", {
        id: profileId, name: profileName, base_url: baseUrl, model, api_key: apiKey, qps, remember, runtime,
      });
      applyProfile(result);
      setStatus(result.remembered ? "配置已保存，密钥安全存放在 macOS 钥匙串。" : "配置已应用；密钥仅保留在本次 App 会话中。");
      return true;
    } catch (error) { setStatus(error instanceof Error ? error.message : "保存配置失败。"); return false; }
    finally { setSaving(false); }
  };

  const switchProfile = async (id: string) => {
    setSaving(true);
    try { applyProfile(await nativeProfile("select", { id })); setPendingSwitch(null); }
    catch (error) { setStatus(error instanceof Error ? error.message : "切换失败，原配置仍保留。"); }
    finally { setSaving(false); }
  };

  const chooseProfile = (id: string) => {
    setProfileMenuOpen(false);
    const current = JSON.stringify([baseUrl, model, qps, runtime, profileName, remember]);
    if (apiKey || current !== savedRef.current) setPendingSwitch(id);
    else void switchProfile(id);
  };

  const removeProfile = async () => {
    if (!window.confirm("删除选中的 API 配置及其钥匙串密钥？其他配置不会受影响。")) return;
    setSaving(true);
    try { applyProfile(await nativeProfile("delete")); }
    catch (error) { setStatus(error instanceof Error ? error.message : "删除失败。"); }
    finally { setSaving(false); }
  };

  const forget = async () => {
    setSaving(true);
    try {
      applyProfile(await nativeProfile("forget"));
      setStatus("已删除钥匙串记录并清除本次会话的密钥。");
    } catch (error) { setStatus(error instanceof Error ? error.message : "删除失败。"); }
    finally { setSaving(false); }
  };

  const changeUrl = (value: string) => {
    setBaseUrl(value);
    setProfileName("");
    setApiKey("");
    setKeyAvailable(false);
    setModels([]);
    setModelsStatus("");
    setStatus("地址已更改，请为此地址填写密钥。");
  };

  return <div className="provider-settings">
    <div className="field full-field"><span id="saved-provider-label">已保存的 API 配置</span>
      <div className="profile-dropdown" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setProfileMenuOpen(false); }}>
        <button type="button" className="profile-dropdown-trigger" aria-labelledby="saved-provider-label saved-provider-value" aria-haspopup="listbox" aria-expanded={profileMenuOpen} disabled={disabled || restoring || saving} onKeyDown={(event) => { if (event.key === "Escape") setProfileMenuOpen(false); }} onClick={() => setProfileMenuOpen((value) => !value)}>
          <span id="saved-provider-value">{(() => { const selected = profiles.find((item) => item.id === profileId); return selected ? `${selected.name} · ${selected.model}` : "新建配置 / 仅本次使用"; })()}</span><span aria-hidden="true">⌄</span>
        </button>
        {profileMenuOpen && <div className="profile-dropdown-options" role="listbox" tabIndex={-1} aria-labelledby="saved-provider-label" onKeyDown={(event) => { if (event.key === "Escape") setProfileMenuOpen(false); }}>
          <button type="button" role="option" aria-selected={!profileId} onClick={() => chooseProfile("")}>新建配置 / 仅本次使用</button>
          {profiles.map((item) => <button type="button" role="option" aria-selected={item.id === profileId} key={item.id} onClick={() => chooseProfile(item.id)}>{item.name} · {item.model}</button>)}
        </div>}
      </div>
    </div>
    <div className="provider-actions"><button type="button" className="text-action" disabled={disabled || restoring || saving} onClick={() => chooseProfile("")}>新建配置</button>
      <button type="button" className="text-action" disabled={disabled || restoring || saving || !profileId} onClick={() => void removeProfile()}>删除配置</button></div>
    {pendingSwitch !== null && <div className="profile-switch-dialog" role="alertdialog" aria-label="当前配置有未保存修改">
      <p>当前配置有未保存修改。切换前要保存吗？</p><div className="provider-actions">
        <button type="button" disabled={saving} onClick={() => { const target = pendingSwitch; void save().then((ok) => { if (ok) void switchProfile(target); }); }}>保存并切换</button>
        <button type="button" disabled={saving} onClick={() => void switchProfile(pendingSwitch)}>放弃修改并切换</button>
        <button type="button" disabled={saving} onClick={() => setPendingSwitch(null)}>取消</button>
      </div></div>}
    <div className="provider-summary"><strong>{restoring ? "正在恢复配置…" : (() => { try { return new URL(baseUrl).host; } catch { return "自定义供应商"; } })()}</strong><span>{keyAvailable ? "密钥已就绪" : "等待配置"}</span></div>
    <div className="field full-field"><div className="model-field-heading"><label htmlFor="provider-models">翻译模型</label>
      <button type="button" className="text-action" disabled={disabled || restoring || loading || (!apiKey.trim() && !keyAvailable)} onClick={() => void loadModels()}>{loading ? "正在获取…" : "刷新模型"}</button>
    </div>
      <select id="provider-models" disabled={disabled || restoring || !models.length} value={models.includes(model) ? model : ""} onChange={(event) => setModel(event.target.value)}>
        <option value="">{models.length ? "选择模型，也可在下方手动输入" : "配置 API 后自动获取"}</option>
        {models.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
    </div>
    <button className="text-action manual-model" type="button" disabled={disabled || restoring} onClick={() => setManualOpen((value) => !value)}>{manualOpen ? "收起手动输入" : "手动填写模型名称"}</button>
    {(manualOpen || (!restoring && !models.includes(model))) && <label className="field full-field manual-model"><span>模型名称（可手动输入）</span>
      <input disabled={disabled || saving || restoring} value={model} onChange={(event) => setModel(event.target.value)} placeholder="选择模型后自动填写" spellCheck={false} />
    </label>}
    {modelsStatus && <p className="provider-hint" aria-live="polite">{modelsStatus}</p>}
    <button className="advanced-trigger" type="button" aria-expanded={connectionOpen} onClick={() => setConnectionOpen((value) => !value)}><span>API 地址与钥匙串设置</span><span>{connectionOpen ? "收起" : "展开"}</span></button>
    {connectionOpen && <div className="connection-fields">
      <label className="field full-field"><span>配置名称（可选）</span><input value={profileName} disabled={disabled || saving || restoring} onChange={(event) => setProfileName(event.target.value)} placeholder="例如：论文翻译服务" maxLength={100} /></label>
      <label className="field full-field"><span>API 基础地址</span>
        <input disabled={disabled || saving || restoring} value={baseUrl} onChange={(event) => changeUrl(event.target.value)} placeholder="https://你的供应商/v1" spellCheck={false} autoCapitalize="none" />
      </label>
      <label className="field full-field"><span>API 密钥 {keyAvailable && <small>本次会话已就绪</small>}</span>
        <input disabled={disabled || saving || restoring} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={keyAvailable ? "留空继续使用已配置的密钥" : "填写此供应商的 API Key"} autoComplete="off" spellCheck={false} />
      </label>
      <label className="remember-control"><input type="checkbox" checked={remember} disabled={disabled || saving || restoring} onChange={(event) => setRemember(event.target.checked)} />安全记住 API 密钥（macOS 钥匙串）</label>
      <button className="text-action" type="button" disabled={disabled || saving || restoring} onClick={() => void forget()}>忘记密钥</button>
    </div>}
    <div className="provider-actions"><button className="save-config" type="button" disabled={disabled || saving || restoring || !model.trim() || !baseUrl.trim() || (!apiKey.trim() && !keyAvailable)} onClick={() => void save()}>{saving ? "正在处理…" : "保存配置"}</button></div>
    {status && <p className="provider-hint" role="status">{status}</p>}
  </div>;
}
