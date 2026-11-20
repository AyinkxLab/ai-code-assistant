// @vitest-environment node
import { describe, expect, it } from "vitest";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  APITimeoutError,
  Client,
  IDEMPOTENT_METHODS,
  NotFoundError,
  PermissionDeniedError,
  RETRYABLE_STATUSES,
  RateLimitError,
  ServerError,
  ValidationError,
  errorFromResponse,
  parseProject,
  parseReview,
  parseUsage,
} from "../../sdk/typescript/src/index.ts";

const FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../sdk/fixtures/error-mapping.json", import.meta.url)),
    "utf-8",
  ),
);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function startServer(responder) {
  const calls = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("error", () => {});
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", async () => {
      const call = {
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: Buffer.concat(chunks).toString("utf-8"),
      };
      calls.push(call);
      let reply;
      try {
        reply = await responder(call, calls.length);
      } catch (error) {
        reply = { status: 500, body: { error: String(error) } };
      }
      const body = reply.body;
      const payload =
        body === undefined || body === null
          ? ""
          : typeof body === "string"
            ? body
            : JSON.stringify(body);
      try {
        res.writeHead(reply.status ?? 200, {
          "Content-Type": "application/json",
          ...(reply.headers ?? {}),
          "Content-Length": Buffer.byteLength(payload),
        });
        res.end(payload);
      } catch {
        // The client may have aborted the request (timeout tests).
      }
    });
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, url: `http://127.0.0.1:${port}`, calls });
    });
  });
}

async function withServer(responder, fn) {
  const { server, url, calls } = await startServer(responder);
  try {
    return await fn(url, calls);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

function makeClient(url, overrides = {}) {
  return new Client({
    baseUrl: url,
    apiToken: "secret-token",
    timeoutMs: 5000,
    maxRetries: 2,
    backoffFactorMs: 0,
    jitter: false,
    ...overrides,
  });
}

describe("TypeScript SDK client", () => {
  it("sends the bearer token and decodes JSON", async () => {
    await withServer(
      () => ({ status: 200, body: { ok: true } }),
      async (url, calls) => {
        const response = await makeClient(url).get("/api/ping");
        expect(response.status).toBe(200);
        expect(response.data).toEqual({ ok: true });
        expect(calls[0].headers.authorization).toBe("Bearer secret-token");
        expect(calls[0].headers.accept).toBe("application/json");
      },
    );
  });

  it("omits the authorization header without a token", async () => {
    await withServer(
      () => ({ status: 200, body: { ok: true } }),
      async (url, calls) => {
        const client = new Client({ baseUrl: url, backoffFactorMs: 0, jitter: false });
        await client.get("/api/ping");
        expect(calls[0].headers.authorization).toBeUndefined();
      },
    );
  });

  it("raises a typed timeout error", async () => {
    await withServer(
      async () => {
        await sleep(200);
        return { status: 200, body: { ok: true } };
      },
      async (url) => {
        const client = makeClient(url, { timeoutMs: 30, maxRetries: 0 });
        await expect(client.get("/api/slow")).rejects.toBeInstanceOf(APITimeoutError);
      },
    );
  });

  it("retries idempotent requests on transient status", async () => {
    await withServer(
      (_call, count) =>
        count === 1
          ? { status: 503, body: { error: "unavailable" } }
          : { status: 200, body: { ok: true } },
      async (url, calls) => {
        const response = await makeClient(url).get("/api/projects");
        expect(response.status).toBe(200);
        expect(calls.length).toBe(2);
      },
    );
  });

  it("never retries POST", async () => {
    await withServer(
      () => ({ status: 503, body: { error: "unavailable" } }),
      async (url, calls) => {
        await expect(
          makeClient(url).post("/api/projects", { json: { name: "demo" } }),
        ).rejects.toBeInstanceOf(ServerError);
        expect(calls.length).toBe(1);
      },
    );
  });

  it("does not retry non-transient statuses", async () => {
    await withServer(
      () => ({ status: 400, body: { error: "bad request" } }),
      async (url, calls) => {
        await expect(makeClient(url).get("/api/projects")).rejects.toBeInstanceOf(ValidationError);
        expect(calls.length).toBe(1);
      },
    );
  });

  it("bounds the number of retries", async () => {
    await withServer(
      () => ({ status: 500, body: { error: "boom" } }),
      async (url, calls) => {
        await expect(
          makeClient(url, { maxRetries: 2 }).get("/api/projects"),
        ).rejects.toBeInstanceOf(ServerError);
        expect(calls.length).toBe(3);
      },
    );
  });

  it("honors Retry-After", async () => {
    const delays = [];
    await withServer(
      (_call, count) =>
        count === 1
          ? { status: 429, body: { error: "slow down" }, headers: { "Retry-After": "2" } }
          : { status: 200, body: { ok: true } },
      async (url) => {
        const client = makeClient(url, {
          maxRetries: 1,
          maxBackoffMs: 5000,
          sleep: async (ms) => {
            delays.push(ms);
          },
        });
        const response = await client.get("/api/projects");
        expect(response.status).toBe(200);
      },
    );
    expect(delays).toEqual([2000]);
  });

  it("uses exponential backoff", async () => {
    const delays = [];
    await withServer(
      (_call, count) =>
        count < 3 ? { status: 500, body: { error: "boom" } } : { status: 200, body: { ok: true } },
      async (url) => {
        const client = makeClient(url, {
          maxRetries: 2,
          backoffFactorMs: 500,
          sleep: async (ms) => {
            delays.push(ms);
          },
        });
        await client.get("/api/projects");
      },
    );
    expect(delays).toEqual([500, 1000]);
  });

  it("preserves code, status and request id", async () => {
    const body = { error: { code: "insufficient_scope", message: "needs scope" } };
    await withServer(
      () => ({ status: 403, body, headers: { "X-Request-ID": "req-123" } }),
      async (url) => {
        let caught;
        try {
          await makeClient(url).get("/api/projects");
        } catch (error) {
          caught = error;
        }
        expect(caught).toBeInstanceOf(PermissionDeniedError);
        expect(caught.code).toBe("insufficient_scope");
        expect(caught.requestId).toBe("req-123");
        expect(caught.statusCode).toBe(403);
        expect(caught.message).toBe("needs scope");
      },
    );
  });

  it("reads the request id from the body when the header is missing", async () => {
    const body = { error: "gone", code: "not_found", request_id: "body-req" };
    await withServer(
      () => ({ status: 404, body }),
      async (url) => {
        let caught;
        try {
          await makeClient(url).get("/api/projects/9");
        } catch (error) {
          caught = error;
        }
        expect(caught).toBeInstanceOf(NotFoundError);
        expect(caught.requestId).toBe("body-req");
        expect(caught.code).toBe("not_found");
      },
    );
  });

  it("maps errors identically to the shared fixture", () => {
    for (const [status, name] of Object.entries(FIXTURE.status_to_error)) {
      expect(errorFromResponse(Number(status), { error: "x" }).name).toBe(name);
    }
    for (const [code, name] of Object.entries(FIXTURE.code_to_error)) {
      expect(errorFromResponse(418, { error: { code, message: "x" } }).name).toBe(name);
    }
    expect(new Set(FIXTURE.retryable_statuses)).toEqual(RETRYABLE_STATUSES);
    expect(new Set(FIXTURE.idempotent_methods)).toEqual(IDEMPOTENT_METHODS);
  });

  it("exposes typed error classes for common statuses", () => {
    expect(errorFromResponse(429, { error: "slow" })).toBeInstanceOf(RateLimitError);
    expect(errorFromResponse(404, { error: "gone" })).toBeInstanceOf(NotFoundError);
  });

  it("parses and validates typed models", () => {
    const project = parseProject({ id: 1, name: "Demo", github_repo: "owner/repo" });
    expect(project.name).toBe("Demo");
    expect(project.github_repo).toBe("owner/repo");

    const review = parseReview({
      id: 2,
      status: "complete",
      quality_score: 9,
      findings: [{ severity: "high", title: "Unsafe query" }],
    });
    expect(review.quality_score).toBe(9);
    expect(review.findings[0].title).toBe("Unsafe query");

    expect(parseUsage({ input_tokens: 10, output_tokens: 5 }).total_tokens).toBe(15);

    expect(() => parseProject({ id: "1", name: "Demo" })).toThrow(ValidationError);
    expect(() => parseProject({ id: 1 })).toThrow(ValidationError);
    expect(() => parseReview({ id: 2, status: "complete", findings: "nope" })).toThrow(
      ValidationError,
    );
  });

  it("parses resource helpers and drops absent query params", async () => {
    const responses = [
      { status: 200, body: { items: [{ id: 1, name: "Demo" }] } },
      { status: 200, body: { id: 1, name: "Demo" } },
      { status: 200, body: { items: [{ id: 2, status: "complete", findings: [] }] } },
      { status: 200, body: { input_tokens: 1, output_tokens: 2, total_tokens: 3 } },
    ];
    await withServer(
      (_call, count) => responses[Math.min(count - 1, responses.length - 1)],
      async (url, calls) => {
        const client = makeClient(url);
        expect((await client.listProjects())[0].name).toBe("Demo");
        expect((await client.getProject(1)).id).toBe(1);
        expect((await client.listReviews({ project_id: 1 }))[0].status).toBe("complete");
        expect((await client.getUsage()).total_tokens).toBe(3);

        expect(calls[0].url.startsWith("/api/projects?")).toBe(true);
        expect(calls[2].url).toContain("project_id=1");
        expect(calls[3].url).not.toContain("project_id");
      },
    );
  });

  it("rejects invalid configuration and input", async () => {
    expect(() => new Client({ baseUrl: "ftp://example.com" })).toThrow(ValidationError);
    expect(() => new Client({ baseUrl: "not-a-url" })).toThrow(ValidationError);
    expect(() => new Client({ baseUrl: "http://example.com", timeoutMs: 0 })).toThrow(
      ValidationError,
    );
    expect(() => new Client({ baseUrl: "http://example.com", maxRetries: -1 })).toThrow(
      ValidationError,
    );
    expect(() => new Client({ baseUrl: "http://example.com", backoffFactorMs: -1 })).toThrow(
      ValidationError,
    );

    const client = new Client({ baseUrl: "http://127.0.0.1:1", jitter: false });
    await expect(client.getProject(0)).rejects.toBeInstanceOf(ValidationError);
  });
});
