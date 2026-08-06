const baseURL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export interface ResearchRequest {
  topic: string;
  search_api?: string;
  parent_run_id?: string;
}

export interface ContinueRequest {
  topic: string;
  parent_run_id: string;
  search_api?: string;
}

export interface ResearchStreamEvent {
  type: string;
  run_id: string;
  schema_version?: number;
  sequence?: number;
  status?: string;
  detail?: string;
  code?: string;
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
