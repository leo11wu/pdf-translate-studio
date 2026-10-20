export type Artifact = { kind: string; filename: string; route: string };
export type RuntimeStats = {
  status?: string; requests?: number; successful_requests?: number; in_flight?: number;
  effective_concurrency?: number; peak_in_flight?: number; rate_limits?: number; upstream_errors?: number;
  protocol_retries?: number; request_seconds?: number; retry_after_seconds?: number;
  last_response_at?: number | null; reasoning_status?: string;
  usage?: Record<string, number>;
};

export type TranslationJob = {
  id: string; filename: string; kind: "translation" | "extraction";
  status: string; stage: string; progress: number;
  page_count: number; part_pages: number | null; planned_parts: number;
  current_part: number; total_parts: number; dual_requested?: boolean;
  download_ready: boolean; dual_download_ready: boolean;
  link_stats?: Record<string, unknown>; warnings?: string[];
  artifacts: Artifact[]; model: string; provider: string; qps: number;
  created_at: number; finished_at: number | null; elapsed_seconds: number | null;
  stage_seconds: Record<string, number>; cache_hit: boolean;
  options: { ocr_assist?: boolean; generate_dual?: boolean; export_texts?: boolean; use_cache?: boolean;
    runtime?: { concurrency_mode?: string; max_in_flight?: number; request_rate?: number } };
  error_message?: string;
  runtime_stats?: RuntimeStats;
};

export function elapsed(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "耗时未记录";
  if (value < 60) return `${value.toFixed(1)} 秒`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
}
