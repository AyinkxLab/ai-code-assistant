/**
 * Webhook signature helpers for the AI Code Assistant TypeScript SDK (#663).
 *
 * The server signs `<timestamp>.<raw body bytes>` with HMAC-SHA256 and sends:
 *
 * ```
 * X-AICA-Signature: v1=<lowercase hex>
 * X-AICA-Timestamp: <unix seconds>
 * ```
 *
 * Verification uses the Web Crypto API (available in modern browsers and
 * Node.js 18+) and checks the timestamp against a replay tolerance window.
 */

import { ValidationError } from "./errors.js";

export const SIGNATURE_HEADER = "X-AICA-Signature";
export const TIMESTAMP_HEADER = "X-AICA-Timestamp";
export const SIGNATURE_VERSION = "v1";
export const DEFAULT_TOLERANCE_SECONDS = 300;

const encoder = new TextEncoder();

export type WebhookBody = string | Uint8Array;

export interface VerifySignatureOptions {
  secret: string;
  body: WebhookBody;
  signature: string;
  timestamp: number;
  toleranceSeconds?: number;
  now?: number;
}

function asBody(body: WebhookBody): Uint8Array {
  if (typeof body === "string") return encoder.encode(body);
  if (body instanceof Uint8Array) return body;
  throw new ValidationError("webhook body must be a string or Uint8Array");
}

function asSecret(secret: string): string {
  if (typeof secret !== "string" || secret.length === 0) {
    throw new ValidationError("webhook secret must be a non-empty string");
  }
  return secret;
}

function asTimestamp(timestamp: number, label: string): number {
  if (!Number.isInteger(timestamp) || timestamp < 0) {
    throw new ValidationError(`${label} must be a non-negative integer`);
  }
  return timestamp;
}

/** Returns the exact bytes that are signed for `timestamp` and `body`. */
export function signedContent(timestamp: number, body: WebhookBody): Uint8Array {
  const prefix = encoder.encode(`${asTimestamp(timestamp, "webhook timestamp")}.`);
  const payload = asBody(body);
  const out = new Uint8Array(prefix.length + payload.length);
  out.set(prefix, 0);
  out.set(payload, prefix.length);
  return out;
}

async function hmacHex(secret: string, message: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(asSecret(secret)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, message as unknown as BufferSource);
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** Computes the `v1=<hex>` signature for a delivery. */
export async function computeSignature(
  secret: string,
  body: WebhookBody,
  timestamp: number,
): Promise<string> {
  const digest = await hmacHex(secret, signedContent(timestamp, body));
  return `${SIGNATURE_VERSION}=${digest}`;
}

function constantTimeEqual(left: string, right: string): boolean {
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
  }
  return difference === 0;
}

/**
 * Verifies a delivery signature and its replay window.
 *
 * Returns `false` for a malformed or mismatched signature and for a timestamp
 * outside `toleranceSeconds`. Throws `ValidationError` for invalid arguments.
 */
export async function verifySignature(options: VerifySignatureOptions): Promise<boolean> {
  const { secret, body, signature, timestamp } = options;
  const tolerance = options.toleranceSeconds ?? DEFAULT_TOLERANCE_SECONDS;
  asSecret(secret);
  asTimestamp(timestamp, "webhook timestamp");
  if (!Number.isInteger(tolerance) || tolerance < 0) {
    throw new ValidationError("toleranceSeconds must be a non-negative integer");
  }
  if (typeof signature !== "string") return false;
  const now = options.now === undefined ? Math.floor(Date.now() / 1000) : options.now;
  asTimestamp(now, "now");
  if (Math.abs(now - timestamp) > tolerance) return false;
  const expected = await computeSignature(secret, body, timestamp);
  return constantTimeEqual(expected, signature);
}

/** Reads `(signature, timestamp)` from headers, case-insensitively. */
export function extractSignature(headers: Headers | Record<string, string>): {
  signature?: string;
  timestamp?: number;
} {
  const get = (name: string): string | undefined => {
    if (headers instanceof Headers) return headers.get(name) ?? undefined;
    const lowered = name.toLowerCase();
    for (const [key, value] of Object.entries(headers)) {
      if (key.toLowerCase() === lowered) return value;
    }
    return undefined;
  };
  const signature = get(SIGNATURE_HEADER);
  const rawTimestamp = get(TIMESTAMP_HEADER);
  if (signature === undefined || rawTimestamp === undefined) return {};
  const timestamp = Number.parseInt(rawTimestamp, 10);
  if (!Number.isInteger(timestamp) || timestamp < 0) return {};
  return { signature, timestamp };
}
