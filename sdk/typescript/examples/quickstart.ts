/**
 * Runnable TypeScript SDK quickstart (#664).
 *
 * Start the bundled local mock server first, then run this file with your
 * preferred TypeScript runner:
 *
 *   python sdk/python/examples/mock_server.py
 *   npx tsx sdk/typescript/examples/quickstart.ts
 *
 * Authentication, pagination, structured errors, and webhook delivery logs are
 * shown. Set AICA_BASE_URL / AICA_API_TOKEN to target another API.
 */

import { Client, NotFoundError } from "../src/index.js";

const baseUrl = process.env.AICA_BASE_URL ?? "http://127.0.0.1:8000";
const apiToken = process.env.AICA_API_TOKEN ?? "demo-token";

const client = new Client({ baseUrl, apiToken, maxRetries: 1 });

async function main(): Promise<void> {
  const pageOne = await client.listProjects({ page: 1, per_page: 2 });
  const pageTwo = await client.listProjects({ page: 2, per_page: 2 });

  let structuredError: { type: string; statusCode: number | null } | null = null;
  try {
    await client.getProject(9999);
  } catch (error) {
    if (error instanceof NotFoundError) {
      structuredError = { type: error.name, statusCode: error.statusCode };
    } else {
      throw error;
    }
  }

  const deliveries = await client.request(
    "GET",
    "/webhooks/api/subscriptions/1/deliveries",
    { query: { status: "failure", per_page: 5 } },
  );

  console.log(
    "Projects (page 1):",
    pageOne.map((project) => project.name),
  );
  console.log(
    "Projects (page 2):",
    pageTwo.map((project) => project.name),
  );
  console.log("Structured error:", structuredError);
  console.log("Webhook deliveries:", deliveries.data);
}

void main();
