export type Artifact = { kind: string; filename: string; route: string };

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
  options: { ocr_assist?: boolean; generate_dual?: boolean; export_texts?: boolean; use_cache?: boolean };
  error_message?: string;
};

export function elapsed(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "耗时未记录";
  if (value < 60) return `${value.toFixed(1)} 秒`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
}
