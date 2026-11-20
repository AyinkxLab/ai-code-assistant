# Helm chart (#706)

The chart in [`deploy/helm/ai-code-assistant`](../deploy/helm/ai-code-assistant/)
renders the web Deployment, the job-queue worker Deployment, a Service, an
ingress, a pre-upgrade migration Job, and an **optional** in-cluster PostgreSQL
StatefulSet.

```bash
helm upgrade --install aica deploy/helm/ai-code-assistant \
  --namespace aica --create-namespace
```

## Before installing

Create the Secret the chart references. It contains no default credentials:

```bash
kubectl -n aica create secret generic aica-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=DATABASE_URL="postgresql://aica:<password>@<host>:5432/aica"
```

| Key in the Secret | Required | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | yes | Flask sessions and stored provider-key encryption. |
| `DATABASE_URL` | yes | PostgreSQL URL; production requires `postgresql://`. |
| `METRICS_TOKEN` | no | Bearer token for `GET /metrics`. |

Both Deployments and the migration Job use `envFrom.secretRef`, so every key in
`secrets.existingSecret` becomes an environment variable. The default name is
`aica-secrets`; override it with `--set secrets.existingSecret=<name>`.

The TLS secret named by `ingress.tls.secretName` (default `aica-tls`) must also
exist in the namespace.

## Values

| Value | Default | Notes |
| --- | --- | --- |
| `image.repository` / `image.tag` | `ghcr.io/ayinkxlab/ai-code-assistant` / `0.1.0` | Pin a released tag, not `latest`. |
| `secrets.existingSecret` | `aica-secrets` | Never contains a value in the chart. |
| `web.replicaCount` | `2` | RollingUpdate with `maxUnavailable: 0`. |
| `web.service.type` / `web.service.port` | `ClusterIP` / `80` | |
| `worker.replicaCount` | `1` | Set `0` to pause background work. |
| `ingress.enabled` | `true` | Nginx ingress with SSE-safe timeouts. |
| `ingress.host` | `assistant.example.com` | Replace it. |
| `ingress.tls.enabled` / `secretName` | `true` / `aica-tls` | |
| `database.enabled` | `false` | Prefer a managed database. |
| `database.auth.existingSecret` | `aica-database` | Must contain the username/password keys below. |
| `database.persistence.size` | `20Gi` | PVC template when the database is enabled. |

`values.schema.json` is enforced by Helm. Invalid input (missing image tag,
empty secret name, a non-positive replica count) fails at render time with a
structured values error instead of creating partial resources. Do not weaken
the schema to accept a default credential.

## Workloads

- **Web**: gunicorn on port 5000, startup/readiness/liveness probes on
  `/health`, requests `250m`/`512Mi` and limits `1`/`1Gi` by default, a
  non-root security context, dropped capabilities and a 10 second `preStop`
  delay. `/health` does not query the database; see
  [Multi-region deployment](multi-region.md#health-checks).
- **Worker**: `flask jobs worker`, the same image and Secret, one replica, and
  an exec liveness probe (`flask jobs list --limit 1`) that checks database
  connectivity.
- **Migration Job**: annotated `helm.sh/hook: pre-install,pre-upgrade`, so
  `flask db upgrade` completes before new pods roll. It is recreated on each
  upgrade (`before-hook-creation`) and bounded by `backoffLimit` and
  `activeDeadlineSeconds`. See [Safe migration rollout](migration-rollout.md).
- **Database (optional)**: a single PostgreSQL 16 StatefulSet with a headless
  Service and a `ReadWriteOnce` PVC. Credentials come from
  `database.auth.existingSecret`, never from values. Enable it only when you
  accept running the database yourself; point `DATABASE_URL` at
  `<release>-postgres:5432` in that case.

## Storage

- Web and worker pods mount no volume; `/tmp` is ephemeral and all durable data
  is in PostgreSQL.
- With `database.enabled=true`, the StatefulSet's volume claim template requests
  `database.persistence.size` from the cluster's default StorageClass. Set
  `database.persistence.storageClass` to pin one.
- With the default external database, storage and backups are the database
  provider's responsibility; see [Backup and restore](backup-restore.md).

## Permissions

Installing needs RBAC for `deployments`, `services`, `ingresses`, `jobs`,
`statefulsets`, `persistentvolumeclaims` and `pods` (hook execution) in the
namespace, plus `get` on the referenced Secrets. An authorization failure is
returned by the API server as a structured `Status` object; Helm prints it and
stops. There is no chart value that broadens cluster permissions.

## Validation and tests

```bash
# If helm is installed:
helm lint deploy/helm/ai-code-assistant
helm template aica deploy/helm/ai-code-assistant --namespace aica

# Always available:
python -m pytest tests/test_deploy_helm.py -q
```

`tests/test_deploy_helm.py` uses `helm template` when the binary is on `PATH`.
**In this repository's test environment helm is not installed**, so the test
falls back to a small renderer for the template subset the chart uses and then
asserts the rendered Kubernetes objects: images, probes, resources, secret
references, ingress routing/TLS, migration hook, optional database StatefulSet
and absence of `Secret` objects or inline credentials. It does not run
`helm lint` or a cluster-side `--dry-run`.

## Rollback

```bash
helm rollback aica <revision> --namespace aica
```

The migration hook is not rolled back. Prefer a forward migration fix over a
schema downgrade; see [Safe migration rollout](migration-rollout.md).
