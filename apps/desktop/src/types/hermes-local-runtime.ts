// ── Managed local runtime (llama.cpp) ──────────────────────────
// Moved out of ./hermes.ts unchanged and re-exported there. [daveey]

export interface LocalModelPlacement {
  window?: number
  window_label?: string
  spilled?: boolean
  granted_window?: number
  granted_window_label?: string
}

export interface LocalModelLoadProgress {
  stage: string
  value: number
  percent: number
}

export interface LocalModelsStatus {
  enabled: boolean
  tag: string
  configured_tag: string
  update_available: boolean
  runtime_installed: boolean
  runtime_backend: string | null
  server_running: boolean
  server_base_url: string | null
  active_model_id: string | null
  loaded_models: Record<string, string>
  /** Models loading into memory right now: real per-tensor load percent. */
  loading?: Record<string, LocalModelLoadProgress>
  placement?: Record<string, LocalModelPlacement>
  models: { id: string; size_bytes: number; size_label: string }[]
  models_dir: string
}

export interface LocalHardware {
  uma: boolean
  vram_total_bytes: number
  vram_usable_bytes: number
  ram_total_bytes: number
  ram_available_bytes: number
  vram_label: string
  gpu_name: string | null
  gpu_util_percent: number | null
  vram_used_bytes: number | null
}

export interface LocalCatalogModel {
  id: string
  display_name: string
  description: string
  size_bytes: number
  size_label: string
  native_context: number
  native_context_label: string
  recommended: boolean
  /** Why the resolver picked this entry (recommended rows only):
   *  product-default | best-quality-resident | speed-gated-quality.
   *  Renders as the Recommended badge's tooltip. */
  recommended_reason?: string | null
  downloaded: boolean
  downloaded_model_id?: string | null
  downloaded_quant?: string | null
  mtp: boolean
  vision?: boolean
  fits: boolean
  fit_summary: string
  fit_detail?: string
  model_id?: string
  quant?: string
  quant_reason?: string
  quant_validated?: boolean
  variant_count?: number
  start_window?: number
  start_window_label?: string
  spilled?: boolean
}

export interface LocalRuntimeJob {
  job_id: string
  kind: 'model-activate' | 'model-download' | 'quickstart' | 'runtime-install'
  target: string
  model_id: string | null
  status: 'done' | 'error' | 'paused' | 'running'
  phase: string
  detail: string
  total_bytes: number | null
  done_bytes: number
  percent?: number
  /** Smoothed transfer rate (bytes/sec) and remaining seconds. Present only
   * while a download is actually moving — absent when parked or settled, so a
   * stale speed never reads as the live one. */
  bytes_per_sec?: number | null
  eta_seconds?: number | null
  error: string | null
  /** Which file ranges of the source plan are fetched vs banked — present
   * while a plan (multi-file or cached+fresh mix) is in flight. */
  ranges?: Record<string, [number, number][]>
  /** Control flags: false while the job is in a phase that cannot park
   * (server start, default assignment, non-download quickstart legs). */
  can_pause?: boolean
  can_resume?: boolean
  /** The backend has accepted a pause request; the status flips when the
   * downloader actually parks. */
  pause_requested?: boolean
}
