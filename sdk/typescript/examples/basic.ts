import { Client } from "../src/index.js";

const client = new Client({
  baseUrl: process.env.AICA_BASE_URL ?? "https://api.example.com",
  apiToken: process.env.AICA_API_TOKEN,
  timeoutMs: 15_000,
});

const projects = await client.listProjects();
for (const project of projects) {
  console.log(`${project.id}: ${project.name}`);
}
