# A Code Assistant

AI-powered developer workspace for GitHub repos, project intelligence, code review, and team collaboration.

Build faster with AI that understands your real project context — not just generic chat. Connect your GitHub repositories, analyze codebases, review pull requests, and collaborate inside one workspace designed for real engineering work.

## Why this project matters

Most AI tools stop at chat. Real development happens across repositories, files, pull requests, issues, and team workflows. AI Code Assistant brings project-aware AI into the full software lifecycle so developers can explore code, understand architecture, review quality, and collaborate with context.

## What it does

- Chat with AI over project and repository context
- Analyze GitHub repositories, issues, and PRs
- Import projects and search across imported files
- Run AI code review and quality analysis
- Collaborate in workspaces with permissions and activity history
- Extend the platform with plugins and capability-based integrations
- Support Stellar / Soroban-aware development workflows

## Try it in 5 minutes

```bash
# 1. Clone the project
git clone https://github.com/AyindxLab/ai-code-assistant.git
cd ai-code-assistant

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env

# 5. Run the app
python run.py
```

Open http://localhost:5000 in your browser.

## Who it's for

- Developers who want AI help grounded in their actual codebase
- Teams reviewing Pull Requests and project quality
- Engineers working across GitHub repositories and project files
- Builders experimenting with AI workflows, plugins, and repo intelligence

## Why it stands out

- Project-aware AI instead of generic chat-only tools
- GitHub repo and PR intelligence built into the workflow
- Structured findings with review history and quality signals
- Team collaboration and workspace-level permissions
- Extensible plugin architecture for future integrations

## Marketplace and plugin configuration

Plugin installation and outbound requests are opt-in. The defaults are
fail-closed, so a fresh install cannot reach a marketplace or the network:

| Setting | Default | Why it is safe |
| ------- | ------- | -------------- |
| `PLUGIN_CATALOG_FILE` | empty | Remote/catalog installation is disabled until an operator points at a vetted catalog. |
| `PLUGIN_MARKETPLACE_ENABLED` | `1` | Read-only discovery is available, but the shipped catalog is empty. |
| `PLUGIN_MARKETPLACE_CATALOG` | `plugins/marketplace.json` | Offline catalog file; entries must validate against `plugins/plugin.schema.json`. |
| `PLUGIN_MARKETPLACE_URL` | empty | Remote marketplace fetching is disabled; when set it must be HTTPS, allowlisted, timeout/size-bounded, and redirect-free. |
| `PLUGIN_DISCOVERY_DIR` | `plugins` | Read-only directory scanned for manifests by `GET /plugins/discover`; nothing is installed or executed. |
| `PLUGIN_TRUST_POLICY` | `if-present` | Accepts unsigned local plugins, but always refuses forged or untrusted signatures. Use `required` in production. |
| `PLUGIN_TRUSTED_KEYS` | `{}` | No publisher is trusted by default; keys are validated at startup. |
| `PLUGIN_NETWORK_ALLOWLIST` | empty | Every plugin outbound request is denied. Add only the hosts a plugin needs. |
| `PLUGIN_NETWORK_HTTPS_ONLY` | `1` | Plugin traffic must use HTTPS. |
| `PLUGIN_NETWORK_ALLOW_PRIVATE` | `0` | Blocks private, loopback, and link-local targets. |
| `PLUGIN_EXECUTION_TIMEOUT` | `5` | Abandons a plugin callback after five seconds so it cannot stall a request. |
| `PLUGIN_CONFIG_MAX_BYTES` | `65536` | Caps the serialized size of a workspace plugin configuration. |
| `PLUGIN_CONFIG_MAX_KEYS` | `200` | Caps the number of top-level configuration keys. |
| `PLUGIN_CONFIG_MAX_DEPTH` | `8` | Caps configuration nesting depth. |
| `PLUGIN_ERROR_REPORT_LIMIT` | `100` | Caps owner-visible error reports (hard ceiling `500`). |

The catalog is read from disk for each install request; there is no active
cache TTL setting yet (caching is tracked in #377). See
[the plugin guide](docs/plugins.md#configuration-reference) for the full
reference, including signature and catalog formats.

## Observability and billing

Every LLM-backed path appends a metadata-only row to an append-only usage
ledger; prompts and completions are never stored. Dashboards, budgets, exports,
and cost estimates are computed from that ledger. Cost figures are estimates
from a documented price table, not provider invoices.

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `METRICS_ENABLED` | `1` | Serves protected Prometheus metrics at `/metrics`; `0` returns 404. |
| `METRICS_TOKEN` | empty | Bearer token for scrapers. Empty falls back to admin-session auth. |
| `USAGE_RETENTION_DAYS` | `365` | Minimum age before `flask usage purge` may delete ledger rows. |
| `BUDGET_ENFORCEMENT_ENABLED` | `1` | Refuses LLM calls that would exceed a monthly token/cost budget (fail closed). |
| `BUDGET_DEFAULT_WARN_THRESHOLD` | `0.8` | Default warning fraction for new budgets. |
| `BILLING_IMPORT_MAX_BYTES` | `1048576` | Maximum provider billing CSV upload size. |
| `BILLING_IMPORT_MAX_ROWS` | `5000` | Maximum billing CSV rows. |
| `CURRENCY_CODE` | `USD` | Currency code shown on every cost figure. |
| `LLM_MODEL` | empty | Pre-flight estimate model; when set, the price table is used. |
| `LLM_PRICE_TABLE` | empty | Optional JSON provider/model price overlay; invalid values log and fall back safely. |

See [Observability](docs/observability.md) for the ledger, metrics, dashboards,
and export contract, and [Billing](docs/billing.md) for the price table,
budgets, and reconciliation.

## Documentation

- [Chat feature guide](CONTRIBUTING.md#chat-feature-developer-guide)
- [GitHub integration](docs/reviews.md)
- [Stellar / Soroban tooling](docs/stellar.md)
- [Plugin system](docs/plugins.md)
- [Architecture](docs/architecture.md)
- [Security model](docs/security.md)
- [Soroban workflow guide](docs/soroban-workflow.md)
- [Observability design](docs/observability.md)
- [Billing](docs/billing.md)
- [Cost precision](docs/cost-precision.md)
- [Usage and billing data privacy](docs/usage-privacy.md)
- [Roadmap](https://github.com/AyindxLab/ai-code-assistant/issues)

## License

[MIT](./LICENSE)
