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
```
