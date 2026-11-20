/**
 * Typed errors for the AI Code Assistant TypeScript SDK.
 *
 * The mapping mirrors `sdk/fixtures/error-mapping.json`, which is the shared
 * contract with the Python SDK and is asserted by both test suites.
 */

export const REQUEST_ID_HEADER = "X-Request-ID";

export const IDEMPOTENT_METHODS: ReadonlySet<string> = new Set([
  "GET",
  "HEAD",
  "OPTIONS",
  "PUT",
  "DELETE",
]);

export const RETRYABLE_STATUSES: ReadonlySet<number> = new Set([429, 500, 502, 503, 504]);

export type ErrorType =
  | "ValidationError"
  | "AuthenticationError"
  | "PermissionDeniedError"
  | "NotFoundError"
  | "ConflictError"
  | "RateLimitError"
  | "ServerError";

export const STATUS_TO_ERROR: Readonly<Record<number, ErrorType>> = {
  400: "ValidationError",
  401: "AuthenticationError",
  403: "PermissionDeniedError",
  404: "NotFoundError",
  409: "ConflictError",
  422: "ValidationError",
  429: "RateLimitError",
  500: "ServerError",
  502: "ServerError",
  503: "ServerError",
  504: "ServerError",
};

export const CODE_TO_ERROR: Readonly<Record<string, ErrorType>> = {
  invalid_request: "ValidationError",
  invalid_input: "ValidationError",
  validation_error: "ValidationError",
  unauthorized: "AuthenticationError",
  invalid_token: "AuthenticationError",
  authentication_required: "AuthenticationError",
  forbidden: "PermissionDeniedError",
  insufficient_scope: "PermissionDeniedError",
  not_found: "NotFoundError",
  conflict: "ConflictError",
  rate_limited: "RateLimitError",
  internal_error: "ServerError",
  server_error: "ServerError",
};

export interface SDKErrorOptions {
  statusCode?: number;
  code?: string;
  requestId?: string;
  payload?: unknown;
  retryAfterMs?: number;
}

export class SDKError extends Error {
  readonly statusCode?: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly payload?: unknown;
  readonly retryAfterMs?: number;

  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message);
    this.name = "SDKError";
    this.statusCode = options.statusCode;
    this.code = options.code;
    this.requestId = options.requestId;
    this.payload = options.payload;
    this.retryAfterMs = options.retryAfterMs;
  }

  toJSON(): Record<string, unknown> {
    return {
      type: this.name,
      message: this.message,
      statusCode: this.statusCode,
      code: this.code,
      requestId: this.requestId,
      payload: this.payload,
    };
  }
}

export class ValidationError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "ValidationError";
  }
}

export class AuthenticationError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "AuthenticationError";
  }
}

export class PermissionDeniedError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "PermissionDeniedError";
  }
}

export class NotFoundError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "NotFoundError";
  }
}

export class ConflictError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "ConflictError";
  }
}

export class RateLimitError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "RateLimitError";
  }
}

export class ServerError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "ServerError";
  }
}

export class TransportError extends SDKError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "TransportError";
  }
}

export class APITimeoutError extends TransportError {
  constructor(message: string, options: SDKErrorOptions = {}) {
    super(message, options);
    this.name = "APITimeoutError";
  }
}

const ERROR_CLASSES: Record<ErrorType | "SDKError", typeof SDKError> = {
  SDKError,
  ValidationError,
  AuthenticationError,
  PermissionDeniedError,
  NotFoundError,
  ConflictError,
  RateLimitError,
  ServerError,
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function extractMessage(payload: unknown, statusCode: number): string {
  if (isRecord(payload)) {
    const error = payload.error;
    if (isRecord(error)) {
      for (const key of ["message", "detail", "description"]) {
        const value = error[key];
        if (typeof value === "string" && value.trim()) return value.trim();
      }
    }
    if (typeof error === "string" && error.trim()) return error.trim();
    for (const key of ["message", "detail", "error_description"]) {
      const value = payload[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  if (typeof payload === "string" && payload.trim()) return payload.trim().slice(0, 500);
  return `API request failed with status ${statusCode}`;
}

export function extractCode(payload: unknown): string | undefined {
  if (!isRecord(payload)) return undefined;
  const error = payload.error;
  if (isRecord(error) && typeof error.code === "string" && error.code.trim()) {
    return error.code.trim();
  }
  if (typeof payload.code === "string" && payload.code.trim()) return payload.code.trim();
  return undefined;
}

export function extractRequestId(payload: unknown): string | undefined {
  if (!isRecord(payload)) return undefined;
  for (const key of ["request_id", "requestId"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

export function errorFromResponse(
  statusCode: number,
  payload: unknown,
  requestId?: string,
  retryAfterMs?: number,
): SDKError {
  const code = extractCode(payload);
  const typeName =
    (code !== undefined ? CODE_TO_ERROR[code] : undefined) ??
    STATUS_TO_ERROR[statusCode] ??
    (statusCode >= 500 ? "ServerError" : "SDKError");
  const ErrorClass = ERROR_CLASSES[typeName] ?? SDKError;
  return new ErrorClass(extractMessage(payload, statusCode), {
    statusCode,
    code,
    requestId,
    payload,
    retryAfterMs,
  });
}
