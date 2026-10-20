export type RuntimeOptions = {
  concurrency_mode: "auto" | "manual";
  max_in_flight: number; request_rate: number; batch_tokens: number;
  reasoning_policy: "lowest" | "provider_default";
};

export const defaultRuntime: RuntimeOptions = {
  concurrency_mode: "auto", max_in_flight: 16, request_rate: 2,
  batch_tokens: 1536, reasoning_policy: "lowest",
};

export type NativeProfile = {
  id: string; name: string;
  base_url: string;
  model: string;
  qps: number;
  key_available: boolean;
  remembered: boolean;
  runtime: RuntimeOptions;
  active_profile_id: string;
  profiles: Array<Omit<NativeProfile, "profiles" | "active_profile_id">>;
};

declare global {
  interface Window {
    webkit?: { messageHandlers?: { studioNative?: {
      postMessage(message: Record<string, unknown>): Promise<unknown>;
    } } };
  }
}

export function nativeProfile(action: "load" | "list" | "select" | "save" | "forget" | "delete", profile?: Record<string, unknown>) {
  const bridge = window.webkit?.messageHandlers?.studioNative;
  if (!bridge) return Promise.reject(new Error("请从纸间 Mac App 打开此界面。"));
  return bridge.postMessage({ action, profile, ...(action === "select" ? { profile_id: profile?.id } : {}) }) as Promise<NativeProfile>;
}

export type OutputSettings = { directory: string; auto_save: boolean; folder?: string; cancelled?: boolean };

export function nativeFiles(action: "outputSettings" | "chooseOutput" | "setAutoSave" | "openOutput" | "exportJob" | "openJobFolder", values: Record<string, unknown> = {}) {
  const bridge = window.webkit?.messageHandlers?.studioNative;
  if (!bridge) return Promise.reject(new Error("请从纸间 Mac App 打开此界面。"));
  return bridge.postMessage({ action, ...values }) as Promise<OutputSettings>;
}
