/**
 * AI Code Assistant TypeScript SDK (Phase 12).
 *
 * @example
 * ```ts
 * import { Client } from "@ai-code-assistant/sdk";
 *
 * const client = new Client({
 *   baseUrl: "https://api.example.com",
 *   apiToken: "token",
 *   timeoutMs: 15_000,
 * });
 *
 * const project = await client.getProject(42);
 * console.log(project.name);
 * ```
 */

export {
  Client,
  type ApiResponse,
  type ClientOptions,
  type ListQuery,
  type Page,
  type PageOptions,
  type QueryValue,
  type RequestOptions,
} from "./client.js";
export {
  APITimeoutError,
  AuthenticationError,
  CODE_TO_ERROR,
  ConflictError,
  IDEMPOTENT_METHODS,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  REQUEST_ID_HEADER,
  RETRYABLE_STATUSES,
  SDKError,
  STATUS_TO_ERROR,
  ServerError,
  TransportError,
  ValidationError,
  errorFromResponse,
  type ErrorType,
  type SDKErrorOptions,
} from "./errors.js";
export {
  parseFinding,
  parseProject,
  parseReview,
  parseUsage,
  type Finding,
  type Project,
  type Review,
  type Usage,
} from "./models.js";
export {
  DEFAULT_TOLERANCE_SECONDS,
  SIGNATURE_HEADER,
  SIGNATURE_VERSION,
  TIMESTAMP_HEADER,
  computeSignature,
  extractSignature,
  signedContent,
  verifySignature,
  type VerifySignatureOptions,
  type WebhookBody,
} from "./webhooks.js";
