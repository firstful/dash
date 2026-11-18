# dash — homelab status dashboard (dash.ffilt.us)

Mobile-friendly widget dashboard for the Sutliff homelab. Read-only aggregation
of cluster health, service web-UI reachability, worker queues, Gitea commits +
builds, and the telemetry live feed. **No login**: layout (order + hidden
widgets) is stored per-browser in localStorage.

## Widgets

| Widget | Data source |
|---|---|
| Cluster | k8s API via in-cluster SA (nodes, deployments, pods, namespaces) |
| Services (web UIs) | IngressRoutes → backing svc, HTTP-probed, **up first** |
| Health checks | per-service probe result (status code / latency) |
| Worker queues · live | cs-downloader `/api/downloads` + Gitea Actions runs |
| Gitea commits & builds | Gitea API (repos, commits, actions/runs) |
| Telemetry · live feed | telemetry internal `/feed` (port 8081, cluster-only) |
| Cron findings · high salience | POSTed by cron helper, persisted in `FINDINGS_FILE` |
| Wakeable services | Deployments labelled `rook.io/lifecycle=wakeable` (state: awake/parked) |

## Deploy

GitOps via `reeds/sutliff-cluster` (`apps/dash/`), image built by Gitea Actions
on push to main → `192.168.1.2:3000/reeds/dash:latest`. Bump the tag in
sutliff-cluster after a source change. Secrets (created imperatively, NOT in
the repo):

- `dash-gitea-token` (ns dash): `GITEA_TOKEN` = read-only Gitea API token

Network policy: default-deny; ingress only from Traefik; egress to DNS,
kube-apiserver (192.168.1.2:6443), Gitea (192.168.1.2:3000), cs-downloader
(port 8643), telemetry feed (port 8081).

## API

```
GET /                  widget UI
GET /health            liveness
GET /api/overview      nodes / deployments / namespaces / pods
GET /api/services      ingressroute services + probe, up-first
GET /api/queues        cs-downloader queue + Actions runs
GET /api/gitea         repos: commits + runs
GET /api/telemetry     proxy to telemetry /feed?limit=N
GET /api/wakeable      wakeable deployments (label rook.io/lifecycle=wakeable)
GET /api/findings      cron findings feed (query: ?limit=N, 1-200)
POST /api/findings     report a finding (header X-Findings-Token required)
```

### Findings API

`POST /api/findings` accepts JSON `{title, detail?, source?, severity?}` and
requires the `X-Findings-Token` header (matching `FINDINGS_TOKEN` env, or the
POST is rejected with 401). `severity` is one of `info|warn|critical`
(defaults to `info`). Findings are deduplicated on `(source, title)`: a repeat
refreshes its timestamp/detail/severity and increments `×count`. They are
persisted to `FINDINGS_FILE` (default `/tmp/dash-findings.json`, survives
restart) and capped at `FINDINGS_MAX` (default 100). `GET /api/findings?limit=N`
returns the newest findings first.

`/api/wakeable` lists Deployments labelled `rook.io/lifecycle=wakeable` with
their wake host/service/idle config and `state` (`awake` when desired or ready
replicas > 0, else `parked`), sourced from the in-cluster k8s API.
