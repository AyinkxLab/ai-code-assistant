/**
 * HTTP client for the AI Code Assistant public API.
 *
 * Features (Phase 12, issues #658, #659 and #661):
 *
 * - API-token authentication via `Authorization: Bearer`.
 * - Configurable per-attempt timeouts.
 * - Bounded retries that only apply to idempotent requests and transient
 *   status codes, honoring `Retry-After` when the server sends it.
 * - Typed models and structured errors that preserve the raw code and
 *   request id.
 */

import {
  APITimeoutError,
  IDEMPOTENT_METHODS,
  REQUEST_ID_HEADER,
  RETRYABLE_STATUSES,
  SDKError,
  TransportError,
  ValidationError,
  errorFromResponse,
  extractRequestId,
  isRecord,
} from "./errors.js";
import {
  parseProject,
  parseReview,
  parseUsage,
  type Project,
  type Review,
  type Usage,
} from "./models.js";

const DEFAULT_USER_AGENT = "ai-code-assistant-typescript/0.1.0";

export interface ApiResponse {
  status: number;
  headers: Headers;
  data: unknown;
  requestId?: string;
}

export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  query?: Record<string, QueryValue>;
  json?: unknown;
  headers?: Record<string, string>;
  idempotent?: boolean;
}

export interface ClientOptions {
  baseUrl: string;
  apiToken?: string;
  timeoutMs?: number;
  maxRetries?: number;
  backoffFactorMs?: number;
  maxBackoffMs?: number;
  userAgent?: string;
  jitter?: boolean;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

export interface ListQuery {
  page?: number;
  per_page?: number;
}

/** One page of a paginated list response. */
export interface Page<T> {
  items: T[];
  page: number;
  perPage: number;
  total: number | null;
  totalPages: number | null;
  nextCursor: string | null;
}

/** Options for the pagination iterators. */
export interface PageOptions extends ListQuery {
  /** Stop after this many pages. */
  maxPages?: number;
  /** Initial opaque cursor. */
  cursor?: string;
  /** Additional query parameters. */
  params?: Record<string, QueryValue>;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isAbortError(error: unknown): boolean {
  return isRecord(error) && error.name === "AbortError";
}

function parseBody(text: string, contentType: string): unknown {
  if (!text) return undefined;
  if (contentType.toLowerCase().includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function parseRetryAfter(value: string | null): number | undefined {
  if (value === null) return undefined;
  const seconds = Number(value.trim());
  if (!Number.isFinite(seconds) || seconds < 0) return undefined;
  return seconds * 1000;
}

function items(data: unknown): unknown[] {
  if (Array.isArray(data)) return data;
  if (isRecord(data) && Array.isArray(data.items)) return data.items;
  throw new ValidationError("expected a list response from the API");
}

function positiveInteger(value: number, label: string): number {
  if (!Number.isInteger(value) || value <= 0) {
    throw new ValidationError(`${label} must be a positive integer`);
  }
  return value;
}

export class Client {
  readonly baseUrl: string;
  readonly apiToken?: string;
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly backoffFactorMs: number;
  readonly maxBackoffMs: number;
  readonly userAgent: string;
  readonly jitter: boolean;

  private readonly fetchImpl: typeof fetch;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(options: ClientOptions) {
    const {
      baseUrl,
      apiToken,
      timeoutMs = 30_000,
      maxRetries = 2,
      backoffFactorMs = 500,
      maxBackoffMs = 8_000,
      userAgent = DEFAULT_USER_AGENT,
      jitter = true,
      fetchImpl,
      sleep,
    } = options;

    if (typeof baseUrl !== "string" || !baseUrl.trim()) {
      throw new ValidationError("baseUrl must be a non-empty string");
    }
    let parsed: URL;
    try {
      parsed = new URL(baseUrl);
    } catch {
      throw new ValidationError("baseUrl must be an absolute http(s) URL");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new ValidationError("baseUrl must be an absolute http(s) URL");
    }
    if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new ValidationError("timeoutMs must be a positive number");
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0) {
      throw new ValidationError("maxRetries must be a non-negative integer");
    }
    if (typeof backoffFactorMs !== "number" || backoffFactorMs < 0) {
      throw new ValidationError("backoffFactorMs must be a non-negative number");
    }
    if (typeof maxBackoffMs !== "number" || maxBackoffMs < 0) {
      throw new ValidationError("maxBackoffMs must be a non-negative number");
    }

    const resolvedFetch = fetchImpl ?? globalThis.fetch;
    if (typeof resolvedFetch !== "function") {
      throw new ValidationError("a fetch implementation is required");
    }

    this.baseUrl = baseUrl.trim().replace(/\/+$/, "");
    this.apiToken = apiToken;
    this.timeoutMs = timeoutMs;
    this.maxRetries = maxRetries;
    this.backoffFactorMs = backoffFactorMs;
    this.maxBackoffMs = maxBackoffMs;
    this.userAgent = userAgent;
    this.jitter = jitter;
    this.fetchImpl = resolvedFetch;
    this.sleep = sleep ?? defaultSleep;
  }

  /**
   * Send a request and return the decoded response.
   *
   * Retries happen only when the request is idempotent **and** the failure is
   * transient (a retryable status or a transport failure).
   */
  async request(method: string, path: string, options: RequestOptions = {}): Promise<ApiResponse> {
    if (typeof method !== "string" || !method.trim()) {
      throw new ValidationError("method must be a non-empty string");
    }
    const normalized = method.trim().toUpperCase();
    const idempotent = options.idempotent ?? IDEMPOTENT_METHODS.has(normalized);
    const url = this.buildUrl(path, options.query);

    const headers: Record<string, string> = {
      Accept: "application/json",
      "User-Agent": this.userAgent,
    };
    let body: string | undefined;
    if (options.json !== undefined) {
      try {
        body = JSON.stringify(options.json);
      } catch (error) {
        throw new ValidationError(`json payload is not serializable: ${describeError(error)}`);
      }
      headers["Content-Type"] = "application/json";
    }
    if (this.apiToken) headers.Authorization = `Bearer ${this.apiToken}`;
    if (options.headers) Object.assign(headers, options.headers);

    let attempt = 0;
    for (;;) {
      attempt += 1;
      try {
        return await this.attempt(normalized, url, body, headers);
      } catch (error) {
        if (!(error instanceof SDKError)) throw error;
        const retryable =
          error.statusCode === undefined || RETRYABLE_STATUSES.has(error.statusCode);
        if (!retryable || !idempotent || attempt > this.maxRetries) throw error;
        await this.delay(attempt, error.retryAfterMs);
      }
    }
  }

  get(
    path: string,
    options: Omit<RequestOptions, "json" | "idempotent"> = {},
  ): Promise<ApiResponse> {
    return this.request("GET", path, options);
  }

  post(path: string, options: Omit<RequestOptions, "idempotent"> = {}): Promise<ApiResponse> {
    return this.request("POST", path, { ...options, idempotent: false });
  }

  put(path: string, options: Omit<RequestOptions, "idempotent"> = {}): Promise<ApiResponse> {
    return this.request("PUT", path, { ...options, idempotent: true });
  }

  patch(path: string, options: Omit<RequestOptions, "idempotent"> = {}): Promise<ApiResponse> {
    return this.request("PATCH", path, { ...options, idempotent: false });
  }

  delete(path: string, options: Omit<RequestOptions, "idempotent"> = {}): Promise<ApiResponse> {
    return this.request("DELETE", path, { ...options, idempotent: true });
  }

  async listProjects(query: ListQuery = {}): Promise<Project[]> {
    const response = await this.get("/api/projects", {
      query: { page: query.page ?? 1, per_page: query.per_page ?? 20 },
    });
    return items(response.data).map(parseProject);
  }

  async getProject(projectId: number): Promise<Project> {
    const id = positiveInteger(projectId, "project_id");
    const response = await this.get(`/api/projects/${id}`);
    return parseProject(response.data);
  }

  async listReviews(query: ListQuery & { project_id?: number } = {}): Promise<Review[]> {
    const response = await this.get("/api/reviews", {
      query: {
        page: query.page ?? 1,
        per_page: query.per_page ?? 20,
        project_id: query.project_id,
      },
    });
    return items(response.data).map(parseReview);
  }

  async getUsage(query: { project_id?: number } = {}): Promise<Usage> {
    const response = await this.get("/api/usage", {
      query: { project_id: query.project_id },
    });
    return parseUsage(response.data);
  }

  // -- Pagination helpers ----------------------------------------------------

  async *iteratePages<T = unknown>(
    path: string,
    options: PageOptions = {},
  ): AsyncGenerator<Page<T>> {
    const perPage = options.per_page ?? 20;
    if (!Number.isInteger(perPage) || perPage < 1 || perPage > 100) {
      throw new ValidationError("per_page must be an integer between 1 and 100");
    }
    const maxPages = options.maxPages;
    if (maxPages !== undefined && (!Number.isInteger(maxPages) || maxPages < 1)) {
      throw new ValidationError("maxPages must be a positive integer");
    }

    let page = options.page ?? 1;
    let cursor = options.cursor;
    let pagesSeen = 0;
    while (true) {
      const query: Record<string, QueryValue> = {
        ...(options.params ?? {}),
        page,
        per_page: perPage,
      };
      if (cursor) query.cursor = cursor;
      const response = await this.get(path, { query });
      const data = response.data;
      const rawItems = items(data);
      const record = isRecord(data) ? data : {};
      const total = typeof record.total === "number" ? record.total : null;
      const totalPages = typeof record.total_pages === "number" ? record.total_pages : null;
      const nextCursor = typeof record.next_cursor === "string" ? record.next_cursor : null;
      yield {
        items: rawItems as T[],
        page,
        perPage,
        total,
        totalPages,
        nextCursor,
      };
      pagesSeen += 1;
      if (maxPages !== undefined && pagesSeen >= maxPages) return;
      if (total !== null && page * perPage >= total) return;
      if (totalPages !== null && page >= totalPages) return;
      if (rawItems.length === 0 || rawItems.length < perPage) return;
      if (nextCursor) cursor = nextCursor;
      page += 1;
    }
  }

  async *iterateProjects(options: PageOptions = {}): AsyncGenerator<Project> {
    for await (const page of this.iteratePages<unknown>("/api/projects", options)) {
      for (const item of page.items) {
        yield parseProject(item);
      }
    }
  }

  async *iterateReviews(
    options: PageOptions & { project_id?: number } = {},
  ): AsyncGenerator<Review> {
    const params =
      options.project_id !== undefined ? { project_id: options.project_id } : undefined;
    for await (const page of this.iteratePages<unknown>("/api/reviews", {
      ...options,
      params,
    })) {
      for (const item of page.items) {
        yield parseReview(item);
      }
    }
  }

  private buildUrl(path: string, query?: Record<string, QueryValue>): string {
    if (typeof path !== "string" || !path.trim()) {
      throw new ValidationError("path must be a non-empty string");
    }
    if (/^https?:\/\//i.test(path)) {
      throw new ValidationError("path must be relative; set baseUrl for the host");
    }
    const url = new URL(`${this.baseUrl}/${path.replace(/^\/+/, "")}`);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  private async attempt(
    method: string,
    url: string,
    body: string | undefined,
    headers: Record<string, string>,
  ): Promise<ApiResponse> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      let response: Response;
      try {
        response = await this.fetchImpl(url, {
          method,
          headers,
          body,
          signal: controller.signal,
        });
      } catch (error) {
        if (isAbortError(error)) {
          throw new APITimeoutError(`Request timed out after ${this.timeoutMs}ms`);
        }
        throw new TransportError(`Unable to reach ${this.baseUrl}: ${describeError(error)}`);
      }

      const text = await response.text();
      const data = parseBody(text, response.headers.get("Content-Type") ?? "");
      const requestId =
        response.headers.get(REQUEST_ID_HEADER) ?? extractRequestId(data) ?? undefined;

      if (!response.ok) {
        throw errorFromResponse(
          response.status,
          data,
          requestId,
          parseRetryAfter(response.headers.get("Retry-After")),
        );
      }

      return { status: response.status, headers: response.headers, data, requestId };
    } finally {
      clearTimeout(timer);
    }
  }

  private async delay(attempt: number, retryAfterMs?: number): Promise<void> {
    if (retryAfterMs !== undefined) {
      await this.sleep(Math.max(0, Math.min(retryAfterMs, this.maxBackoffMs)));
      return;
    }
    let base = this.backoffFactorMs * 2 ** (attempt - 1);
    if (base <= 0) {
      await this.sleep(0);
      return;
    }
    if (this.jitter) base += Math.random() * base * 0.1;
    await this.sleep(Math.min(base, this.maxBackoffMs));
  }
}
