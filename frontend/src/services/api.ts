const baseURL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export interface ResearchRequest {
  topic: string;
  search_api?: string;
  parent_run_id?: string;
  use_history_memory?: boolean;
  memory_scope?: string;
}

export interface ContinueRequest {
  topic: string;
  parent_run_id: string;
  search_api?: string;
  use_history_memory?: boolean;
  memory_scope?: string;
}

export interface RecoveryRequest {
  run_id: string;
}

export interface UserMemory {
  memory_id: string;
  scope: string;
  kind: "preference" | "fact";
  text: string;
  status: "pending" | "confirmed";
  source: string;
  created_at: string;
  updated_at: string;
  confirmed_at: string | null;
}

export interface UserMemoryPage {
  items: UserMemory[];
  scope: string;
  limit: number;
}

export interface HistoryItem {
  run_id: string;
  topic: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  parent_run_id: string | null;
  task_count: number;
  report_excerpt: string;
  resumable?: boolean;
  recovery_resumable?: boolean;
  last_resumable_parent?: string | null;
}

export interface HistoryPage {
  items: HistoryItem[];
  next_cursor: string | null;
}

export interface RunRecord {
  run_id: string;
  topic: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  parent_run_id: string | null;
  failure_reason?: string | null;
  checkpoint?: string | null;
  resumable?: boolean;
  recovery_resumable?: boolean;
  last_resumable_parent?: string | null;
  metrics?: Record<string, unknown>;
  output?: {
    running_summary?: string;
    report_markdown?: string;
    todo_items?: unknown[];
  };
  events?: ResearchStreamEvent[];
  [key: string]: unknown;
}

export interface StreamTelemetry {
  duration_ms: number;
  event_count: number;
  bytes_sent: number;
  first_event_latency_ms: number | null;
  stream_completed: boolean;
  terminal_type: string;
}

export interface ResearchStreamEvent {
  type: string;
  run_id: string;
  schema_version?: number;
  sequence?: number;
  status?: string;
  detail?: string;
  code?: string;
  resumable?: boolean;
  recovery_resumable?: boolean;
  last_resumable_parent?: string | null;
  checkpoint?: string | null;
  stream_telemetry?: StreamTelemetry;
  [key: string]: unknown;
}

export interface StreamOptions {
  signal?: AbortSignal;
}

/**
 * Shared SSE stream consumer — reads a fetch Response body line by line
 * parsing ``data: {json}\n\n`` frames and calling ``onEvent`` for each.
 */
export async function consumeSSE(
  response: Response,
  onEvent: (event: ResearchStreamEvent) => void,
): Promise<void> {
  const body = response.body;
  if (!body) {
    throw new Error("浏览器不支持流式响应，无法获取研究进度");
  }

  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let terminalSeen = false;

  const processFrame = (rawEvent: string): void => {
    if (!rawEvent.startsWith("data:")) {
      return;
    }
    const dataPayload = rawEvent.slice(5).trim();
    if (!dataPayload) {
      return;
    }

    let event: ResearchStreamEvent;
    try {
      event = JSON.parse(dataPayload) as ResearchStreamEvent;
    } catch {
      console.error("Failed to parse a research stream event.");
      return;
    }
    if (
      !event ||
      typeof event.type !== "string" ||
      typeof event.run_id !== "string"
    ) {
      console.error("Failed to parse a research stream event.");
      return;
    }

    onEvent(event);
    terminalSeen = event.type === "error" || event.type === "done";
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);

      processFrame(rawEvent);
      if (terminalSeen) {
        return;
      }

      boundary = buffer.indexOf("\n\n");
    }

    if (done) {
      // 处理可能的尾巴事件
      if (buffer.trim()) {
        processFrame(buffer.trim());
      }
      if (terminalSeen) {
        return;
      }
      break;
    }
  }

  throw new Error("Research stream ended before a terminal event.");
}

export async function runResearchStream(
  payload: ResearchRequest,
  onEvent: (event: ResearchStreamEvent) => void,
  options: StreamOptions = {}
): Promise<void> {
  const response = await fetch(`${baseURL}/research/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream"
    },
    body: JSON.stringify(payload),
    signal: options.signal
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(
      errorText || `研究请求失败，状态码：${response.status}`
    );
  }

  return consumeSSE(response, onEvent);
}

export async function runContinueStream(
  payload: ContinueRequest,
  onEvent: (event: ResearchStreamEvent) => void,
  options: StreamOptions = {}
): Promise<void> {
  const response = await fetch(`${baseURL}/research/continue/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream"
    },
    body: JSON.stringify(payload),
    signal: options.signal
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(
      errorText || `继续研究请求失败，状态码：${response.status}`
    );
  }

  return consumeSSE(response, onEvent);
}

export async function runRecoveryStream(
  payload: RecoveryRequest,
  onEvent: (event: ResearchStreamEvent) => void,
  options: StreamOptions = {},
): Promise<void> {
  const response = await fetch(`${baseURL}/research/recover/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(payload),
    signal: options.signal,
  });

  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || `Recovery request failed (${response.status})`);
  }

  return consumeSSE(response, onEvent);
}

export async function listHistory(
  limit = 20,
  cursor?: string,
): Promise<HistoryPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  const response = await fetch(`${baseURL}/runs?${params.toString()}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法加载研究历史");
  }
  return (await response.json()) as HistoryPage;
}

export async function getRunRecord(runId: string): Promise<RunRecord> {
  const response = await fetch(
    `${baseURL}/runs/${encodeURIComponent(runId)}`,
    { headers: { Accept: "application/json" } },
  );
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法加载研究记录");
  }
  return (await response.json()) as RunRecord;
}

export async function listMemories(
  scope = "default",
  includePending = true,
  limit = 50,
): Promise<UserMemoryPage> {
  const params = new URLSearchParams({
    scope,
    include_pending: String(includePending),
    limit: String(limit),
  });
  const response = await fetch(`${baseURL}/memories?${params.toString()}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法加载用户记忆");
  }
  return (await response.json()) as UserMemoryPage;
}

export async function createMemoryCandidate(
  text: string,
  kind: "preference" | "fact" = "preference",
  scope = "default",
): Promise<UserMemory> {
  const response = await fetch(`${baseURL}/memories/candidates`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({ text, kind, scope }),
  });
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法保存记忆候选");
  }
  return (await response.json()) as UserMemory;
}

export async function confirmMemory(
  memoryId: string,
  scope = "default",
): Promise<UserMemory> {
  const params = new URLSearchParams({ scope });
  const response = await fetch(
    `${baseURL}/memories/${encodeURIComponent(memoryId)}/confirm?${params.toString()}`,
    {
      method: "POST",
      headers: { Accept: "application/json" },
    },
  );
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法确认记忆");
  }
  return (await response.json()) as UserMemory;
}

export async function deleteMemory(
  memoryId: string,
  scope = "default",
): Promise<void> {
  const params = new URLSearchParams({ scope });
  const response = await fetch(
    `${baseURL}/memories/${encodeURIComponent(memoryId)}?${params.toString()}`,
    {
      method: "DELETE",
      headers: { Accept: "application/json" },
    },
  );
  if (!response.ok) {
    const errorText = await response.text().catch(() => "");
    throw new Error(errorText || "无法删除记忆");
  }
}
