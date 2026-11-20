# Integration setup and local mocks

This repository has two integrations: **GitHub** (OAuth + REST API) and
**Stellar/Soroban** (read-only Horizon and RPC). Each one has a deterministic,
loopback-only mock used by the test suite.

| Integration | Code | Mock | Tests |
| --- | --- | --- | --- |
| GitHub | `app/services/github.py`, `app/github/` | `MockGitHubServer` in `tests/test_integrations_mock.py` | `tests/test_integrations_mock.py`, `tests/test_github_*.py` |
| Stellar | `app/services/stellar.py`, `app/services/soroban_rpc.py` | `app/services/stellar_mock.py` | `tests/test_integrations_mock.py`, `tests/test_stellar_mock_network.py` |

Both mocks bind to `127.0.0.1` on an ephemeral port. No test contacts a public
network. Credentials for workspace integrations are stored encrypted; see
[encrypted integration credentials](integration-credentials.md).

## GitHub

### Required scopes

The OAuth flow requests `read:user repo` by default (`GITHUB_SCOPES` overrides
it). `repo` is required for private repositories, issues, pull requests, and
commits. A token without it can still read public repositories.

### OAuth callback URL

Register an OAuth App at <https://github.com/settings/applications/new> with:

- **Homepage URL:** `http://localhost:5000`
- **Authorization callback URL:** `http://localhost:5000/github/callback`
  (or the value of `GITHUB_REDIRECT_URI`)

Environment:

```dotenv
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_SCOPES=read:user repo
GITHUB_REDIRECT_URI=http://localhost:5000/github/callback
```

No inbound webhook is used. The only inbound URL is the OAuth callback above;
all other traffic is outbound to `https://api.github.com`.

### Revocation

Disconnecting from the GitHub dashboard calls
`revoke_github_token()`, which deletes the token through
`DELETE https://api.github.com/applications/{client_id}/token` and then clears
the process-local response cache for that token. Revocation is best-effort: the
local connection is removed even when GitHub is unreachable. Users can also
revoke the app manually under GitHub **Settings → Applications**.

### Local mock test

`tests/test_integrations_mock.py` starts a loopback HTTP server and drives the
real client, refresh, and revocation code against it:

```bash
pytest tests/test_integrations_mock.py -o addopts=""
```

### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `state mismatch` after authorizing | Callback opened without the session that started the flow, or the state expired | Start again from `/github/connect` in the same browser session |
| Private repositories missing | Token lacks `repo` | Reconnect and grant `repo`; the dashboard flags a missing scope |
| `404` for a known private repo | GitHub returns 404 when the token cannot see it | Check the token's repository access |
| Rate-limit errors | GitHub quota exhausted | Wait until the reset time reported by GitHub |
| `Could not decrypt stored secret` | `SECRET_KEY` changed | Reconnect the account |

## Stellar / Soroban

### Configuration

The integration is read-only. It never signs, simulates, or submits
transactions and never handles keys. Defaults target testnet.

```dotenv
STELLAR_NETWORK=testnet            # or custom for a local node/mock
STELLAR_HORIZON_URL=https://horizon-testnet.stellar.org
STELLAR_RPC_URL=https://soroban-testnet.stellar.org
```

Custom endpoints are accepted only on loopback hosts and must be HTTP on
loopback / HTTPS otherwise. Clients refuse redirects, enforce timeouts, and cap
response sizes.

### Scopes and webhooks

There are no OAuth scopes and no inbound webhook URLs. Access is configured
entirely through the environment above; no secret is stored for Stellar.

### Revocation

There is nothing to revoke: the integration holds no credentials. To disable
it, unset the Stellar settings or point `STELLAR_NETWORK` at a network you
control.

### Local mock

Start the deterministic mock:

```bash
python -m app.services.stellar_mock
```

It prints loopback Horizon and RPC URLs. Point `STELLAR_NETWORK=custom` at them.
The same server is the `mock_stellar` pytest fixture used by
`tests/test_integrations_mock.py` and `tests/test_stellar_mock_network.py`.

### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `status: unreachable` | Wrong host/port or mock not running | Check the printed mock port and both URLs |
| Custom URL rejected | Non-loopback host with a custom network | Use a loopback host or HTTPS |
| Contract data marked not decoded | The RPC returned unsupported XDR | Inspect the raw XDR; decoding is intentionally bounded |
| `404` / not found | Unknown account, ledger, or contract on this network | Confirm the network and identifier |
