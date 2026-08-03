import type { AgentHostConfig, WebSearchMode } from "./config.js";

type FetchLike = typeof fetch;

export interface WebSearchRequest {
  query: string;
}

export interface WebSearchSource {
  url: string;
  title?: string;
}

export interface WebSearchResponse {
  provider: string;
  model: string;
  answer: string;
  sources: WebSearchSource[];
  /** True only when the provider returned structured URL citations. */
  citationsProvided: boolean;
  searchCallIds: string[];
}

/** Provider-neutral search boundary shared by Pi and future MCP adapters. */
export interface WebSearchProvider {
  readonly provider: string;
  readonly model: string;
  search(
    request: WebSearchRequest,
    signal?: AbortSignal,
  ): Promise<WebSearchResponse>;
}

export class DeepSeekResponsesWebSearchProvider implements WebSearchProvider {
  readonly provider = "deepseek_responses";

  constructor(
    private readonly baseUrl: string,
    private readonly apiKey: string,
    readonly model = "deepseek-v4-flash",
    private readonly fetchImpl: FetchLike = fetch,
    private readonly timeoutMs = 90_000,
  ) {}

  async search(
    request: WebSearchRequest,
    signal?: AbortSignal,
  ): Promise<WebSearchResponse> {
    const query = request.query.trim();
    if (!query) throw new Error("web search query is required");
    if (query.length > 4_000) {
      throw new Error("web search query exceeds 4000 characters");
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const abort = () => controller.abort(signal?.reason);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    try {
      const response = await this.fetchImpl(responsesEndpoint(this.baseUrl), {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: this.model,
          tools: [{ type: "web_search" }],
          tool_choice: { type: "web_search" },
          input: query,
        }),
        signal: controller.signal,
      });
      const payload = (await response.json()) as unknown;
      if (!response.ok) {
        throw new Error(
          `DeepSeek web search failed (${response.status}): ${responseError(payload)}`,
        );
      }
      return parseDeepSeekSearchResponse(payload, this.model);
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }
  }
}

export function createWebSearchProvider(
  config: Pick<
    AgentHostConfig,
    | "webSearchMode"
    | "webSearchModel"
    | "remoteBaseUrl"
    | "remoteApiKey"
  >,
  fetchImpl: FetchLike = fetch,
): WebSearchProvider | undefined {
  if (config.webSearchMode === "disabled") return undefined;
  const officialDeepSeek = isOfficialDeepSeek(config.remoteBaseUrl);
  if (config.webSearchMode === "auto" && !officialDeepSeek) return undefined;
  if (!config.remoteBaseUrl || !config.remoteApiKey) {
    if (config.webSearchMode === "auto") return undefined;
    throw new Error(
      "DeepSeek web search requires AGENT_REMOTE_BASE_URL and the remote model credential",
    );
  }
  return new DeepSeekResponsesWebSearchProvider(
    config.remoteBaseUrl,
    config.remoteApiKey,
    config.webSearchModel,
    fetchImpl,
  );
}

export function webSearchStatus(
  mode: WebSearchMode,
  provider: WebSearchProvider | undefined,
): string {
  if (provider) return `${provider.provider}/${provider.model}`;
  return mode === "disabled" ? "disabled" : "unavailable for this model endpoint";
}

function responsesEndpoint(baseUrl: string): string {
  const url = new URL(baseUrl);
  if (
    url.hostname.toLowerCase() === "api.deepseek.com" &&
    /^\/v1\/?$/u.test(url.pathname)
  ) {
    url.pathname = "/";
  }
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return new URL("responses", url).toString();
}

function isOfficialDeepSeek(baseUrl: string | undefined): boolean {
  if (!baseUrl) return false;
  try {
    return new URL(baseUrl).hostname.toLowerCase() === "api.deepseek.com";
  } catch {
    return false;
  }
}

function parseDeepSeekSearchResponse(
  payload: unknown,
  model: string,
): WebSearchResponse {
  if (!isRecord(payload)) throw new Error("DeepSeek web search returned invalid JSON");
  const outputs = Array.isArray(payload.output) ? payload.output : [];
  const answerParts: string[] = [];
  const sources = new Map<string, WebSearchSource>();
  const searchCallIds: string[] = [];

  for (const output of outputs) {
    if (!isRecord(output)) continue;
    if (output.type === "web_search_call") {
      if (typeof output.id === "string") searchCallIds.push(output.id);
      continue;
    }
    if (output.type !== "message" || !Array.isArray(output.content)) continue;
    for (const content of output.content) {
      if (!isRecord(content) || content.type !== "output_text") continue;
      if (typeof content.text === "string" && content.text.trim()) {
        answerParts.push(content.text.trim());
      }
      if (!Array.isArray(content.annotations)) continue;
      for (const annotation of content.annotations) {
        if (!isRecord(annotation) || annotation.type !== "url_citation") continue;
        const url = typeof annotation.url === "string" ? annotation.url.trim() : "";
        if (!url || sources.has(url)) continue;
        const title =
          typeof annotation.title === "string" && annotation.title.trim()
            ? annotation.title.trim()
            : undefined;
        sources.set(url, { url, ...(title ? { title } : {}) });
      }
    }
  }

  const fallback =
    typeof payload.output_text === "string" ? payload.output_text.trim() : "";
  const answer = answerParts.join("\n\n").trim() || fallback;
  if (!searchCallIds.length) {
    throw new Error("DeepSeek response did not contain a web_search_call");
  }
  if (!answer) throw new Error("DeepSeek web search returned no answer text");
  return {
    provider: "deepseek_responses",
    model,
    answer,
    sources: [...sources.values()],
    citationsProvided: sources.size > 0,
    searchCallIds,
  };
}

function responseError(payload: unknown): string {
  if (!isRecord(payload)) return "invalid error response";
  if (typeof payload.error === "string") return payload.error.slice(0, 500);
  if (isRecord(payload.error) && typeof payload.error.message === "string") {
    return payload.error.message.slice(0, 500);
  }
  return "request rejected";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
