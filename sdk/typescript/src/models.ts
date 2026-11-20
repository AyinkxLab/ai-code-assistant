/**
 * Typed models for the AI Code Assistant TypeScript SDK.
 *
 * Each `parse*` helper performs runtime validation so a malformed payload
 * raises a `ValidationError` instead of producing a half-valid object.
 */

import { ValidationError } from "./errors.js";

export interface Finding {
  id: number | null;
  severity: string;
  category: string | null;
  title: string;
  description: string | null;
  path: string | null;
  line: number | null;
}

export interface Project {
  id: number;
  name: string;
  owner_id: number | null;
  description: string | null;
  github_repo: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Review {
  id: number;
  status: string;
  project_id: number | null;
  quality_score: number | null;
  security_score: number | null;
  tests_score: number | null;
  findings: Finding[];
  created_at: string | null;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  period_start: string | null;
  period_end: string | null;
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ValidationError(`${label} must be a JSON object`);
  }
  return value as Record<string, unknown>;
}

function requiredString(data: Record<string, unknown>, key: string, label: string): string {
  const value = data[key];
  if (typeof value !== "string" || !value.trim()) {
    throw new ValidationError(`${label}.${key} must be a non-empty string`);
  }
  return value;
}

function requiredInteger(data: Record<string, unknown>, key: string, label: string): number {
  const value = data[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new ValidationError(`${label}.${key} must be an integer`);
  }
  return value;
}

function optionalInteger(
  data: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = data[key];
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new ValidationError(`${label}.${key} must be an integer or null`);
  }
  return value;
}

function optionalNumber(
  data: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = data[key];
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || Number.isNaN(value)) {
    throw new ValidationError(`${label}.${key} must be a number or null`);
  }
  return value;
}

function optionalString(
  data: Record<string, unknown>,
  key: string,
  label: string,
): string | null {
  const value = data[key];
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") {
    throw new ValidationError(`${label}.${key} must be a string or null`);
  }
  return value;
}

export function parseFinding(value: unknown): Finding {
  const data = asRecord(value, "finding");
  return {
    id: optionalInteger(data, "id", "finding"),
    severity: requiredString(data, "severity", "finding"),
    category: optionalString(data, "category", "finding"),
    title: requiredString(data, "title", "finding"),
    description: optionalString(data, "description", "finding"),
    path: optionalString(data, "path", "finding"),
    line: optionalInteger(data, "line", "finding"),
  };
}

export function parseProject(value: unknown): Project {
  const data = asRecord(value, "project");
  return {
    id: requiredInteger(data, "id", "project"),
    name: requiredString(data, "name", "project"),
    owner_id: optionalInteger(data, "owner_id", "project"),
    description: optionalString(data, "description", "project"),
    github_repo: optionalString(data, "github_repo", "project"),
    created_at: optionalString(data, "created_at", "project"),
    updated_at: optionalString(data, "updated_at", "project"),
  };
}

export function parseReview(value: unknown): Review {
  const data = asRecord(value, "review");
  const rawFindings = data.findings ?? [];
  if (!Array.isArray(rawFindings)) {
    throw new ValidationError("review.findings must be a list");
  }
  return {
    id: requiredInteger(data, "id", "review"),
    status: requiredString(data, "status", "review"),
    project_id: optionalInteger(data, "project_id", "review"),
    quality_score: optionalNumber(data, "quality_score", "review"),
    security_score: optionalNumber(data, "security_score", "review"),
    tests_score: optionalNumber(data, "tests_score", "review"),
    findings: rawFindings.map(parseFinding),
    created_at: optionalString(data, "created_at", "review"),
  };
}

export function parseUsage(value: unknown): Usage {
  const data = asRecord(value, "usage");
  const inputTokens = requiredInteger(data, "input_tokens", "usage");
  const outputTokens = requiredInteger(data, "output_tokens", "usage");
  const providedTotal = optionalInteger(data, "total_tokens", "usage");
  return {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    total_tokens: providedTotal ?? inputTokens + outputTokens,
    cost_usd: optionalNumber(data, "cost_usd", "usage"),
    period_start: optionalString(data, "period_start", "usage"),
    period_end: optionalString(data, "period_end", "usage"),
  };
}
