# Kubernetes manifests (#707)

Plain manifests for the web runtime, the job-queue worker, a ClusterIP service,
an nginx ingress and a one-off migration job live in
[`deploy/k8s/`](../deploy/k8s/). They are applied with Kustomize:

```bash
kubectl apply -k deploy/k8s
```

The manifests assume a namespace called `aica`, a PostgreSQL database outside
the cluster, and an nginx ingress controller. Nothing here creates a database,
a secret, or a storage class.

## Required secrets

Create the secret out of band; **no secret is committed** and there is no
default password or token in the manifests:

```bash
kubectl -n aica create secret generic aica-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql://aica:<password>@<host>:5432/aica" \
  --from-literal=METRICS_TOKEN="<scraper-token>"
```

Both Deployments and the migration Job load `aica-secrets` with `envFrom`, so
every key becomes an environment variable.

| Key | Required | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | yes | Flask sessions and encryption of stored provider keys. Must match across all replicas. Rotating it invalidates stored provider keys. |
| `DATABASE_URL` | yes | PostgreSQL URL. Production startup refuses anything that is not `postgresql://`. |
| `METRICS_TOKEN` | no | Bearer token for `GET /metrics`. Without it, only a site-admin session may scrape. |

Provider and GitHub credentials (`OPENAI_API_KEY`, `GITHUB_CLIENT_ID`,
`GITHUB_CLIENT_SECRET`, ...) are optional and can be added to the same secret
when those features are enabled.

The TLS certificate is a second secret, `aica-tls`, referenced by the ingress.
Create it with cert-manager or:

```bash
kubectl -n aica create secret tls aica-tls --cert=tls.crt --key=tls.key
```

## Storage

- **Application pods are stateless.** Conversations, projects, the usage
  ledger and the job queue are all in PostgreSQL; the web and worker containers
  mount no PVC.
- **`/tmp` is ephemeral.** Import/export archives are built in memory or in
  temporary files and are bounded by the application's import and export limits.
  Losing `/tmp` on restart loses no durable data.
- **Database storage** must be provisioned outside these manifests (managed
  PostgreSQL or a StatefulSet you operate). Size it for the usage ledger and
  back it up according to [Backup and restore](backup-restore.md).
- The container runs as uid/gid 1000 (`runAsNonRoot`). Files written by a
  process with a different uid will be unreadable; do not override the security
  context.

## Deploy order

1. Create the namespace, then the secret:

   ```bash
   kubectl apply -f deploy/k8s/namespace.yaml
   kubectl -n aica create secret generic aica-secrets \
     --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
     --from-literal=DATABASE_URL="postgresql://aica:<password>@<host>:5432/aica" \
     --from-literal=METRICS_TOKEN="<scraper-token>"
   ```

   Do this before the workloads so pods do not stay in
   `CreateContainerConfigError`.
2. Apply the rest, wait for the migration job, then confirm the web rollout:

   ```bash
   kubectl apply -k deploy/k8s
   kubectl -n aica wait --for=condition=complete job/aica-migrate --timeout=15m
   kubectl -n aica rollout status deployment/aica-web
   ```

   The manifests ship the web deployment at two replicas, so a job is the only
   safe place to apply schema changes; see
   [Safe migration rollout](migration-rollout.md). The `Job` retains its pod for
   a day (`ttlSecondsAfterFinished`) and gives up after three attempts
   (`backoffLimit: 3`).
3. The worker uses the same image and secret, with the entrypoint replaced by
   `flask jobs worker`. Scale `aica-worker` to zero to pause background work.

Pin the image in `deploy/k8s/kustomization.yaml`:

```bash
kustomize edit set image ghcr.io/ayinkxlab/ai-code-assistant=ghcr.io/ayinkxlab/ai-code-assistant:0.1.0
```

## Probes and rollout

- The web pod has startup, readiness and liveness probes on `GET /health`.
  `/health` is a liveness signal only: it does not query the database (see
  [Multi-region deployment](multi-region.md#health-checks)). Use an external
  check for database-aware readiness if your platform needs one.
- Rolling updates use `maxUnavailable: 0` and `maxSurge: 1`; a 10 second
  `preStop` sleep lets the ingress stop sending traffic before gunicorn
  receives `SIGTERM`.
- The worker's liveness probe runs `flask jobs list --limit 1`, which checks
  both the application import and database connectivity.
- Roll back with `kubectl -n aica rollout undo deployment/aica-web`. Roll back
  the worker the same way. Prefer a forward migration fix over a schema
  downgrade.

## Permissions

Applying these manifests needs RBAC for `namespaces`, `deployments`,
`services`, `ingresses` and `jobs` in the target namespace, plus `get`/`list`
on the two secrets if your tooling resolves them client-side. A missing
permission comes back from the API server as a structured `Status` object with
`reason: Forbidden` and the failing verb/resource; read that message rather
than retrying with a broader role.

Validating manifests additionally needs `kubectl apply --dry-run=server` and
the CRDs/controllers for `Ingress` (nginx). This repository does not run a
cluster in tests.

## Tests

`tests/test_deploy_k8s.py` parses every YAML file and asserts identity fields,
probes, resource requests/limits, security context, secret references, service
selector, ingress routing/TLS/SSE annotations, the bounded migration job,
absent `Secret` objects and absence of default credentials. It also checks that
Kustomize lists every manifest.

```bash
python -m pytest tests/test_deploy_k8s.py -q
```

The tests do not run `kubectl apply`, `kubectl diff` or server-side validation.
