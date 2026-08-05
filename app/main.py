"""dash — Sutliff homelab status dashboard.

FastAPI backend that aggregates cluster/service/queue/Gitea/telemetry state
for a mobile-friendly widget frontend. Read-only: talks to the k8s API via its
in-cluster ServiceAccount, probes services, and proxies a few internal APIs.

Endpoints:
  GET /                  static widget UI (index.html)
  GET /health            liveness
  GET /api/overview      cluster health (nodes, deployments, namespaces)
  GET /api/services      IngressRoute services + HTTP probe, up-first
  GET /api/queues        worker queues (cs-downloader + Gitea Actions)
  GET /api/gitea         repos: recent commits + recent Actions runs
  GET /api/telemetry     proxy to telemetry /feed (internal port 8081)

Env:
  PORT            listen port (default 8650)
  GITEA_URL       e.g. http://192.168.1.2:3000
  GITEA_TOKEN     read-only Gitea API token
  CS_URL          cs-downloader internal URL (default http://cs-downloader.cs-downloader.svc.cluster.local:8643)
  TELEMETRY_URL   telemetry internal feed URL (default http://telemetry.telemetry.svc.cluster.local:8081)
  REFRESH_TTL     server-side cache TTL for slow aggregations (default 10s)
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PORT = int(os.getenv("PORT", "8650"))
GITEA_URL = os.getenv("GITEA_URL", "http://192.168.1.2:3000")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
CS_URL = os.getenv("CS_URL", "http://cs-downloader.cs-downloader.svc.cluster.local:8643")
TELEMETRY_URL = os.getenv("TELEMETRY_URL", "http://telemetry.telemetry.svc.cluster.local:8081")
REFRESH_TTL = float(os.getenv("REFRESH_TTL", "10"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="dash", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---------------------------------------------------------------- helpers

_cache: dict[str, tuple[float, Any]] = {}


def cached(key: str, ttl: float = REFRESH_TTL):
    """Decorator: cache the function result for ttl seconds."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            now = time.time()
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            val = fn(*args, **kwargs)
            _cache[key] = (now, val)
            return val
        return wrapper
    return deco


def http_get(url: str, timeout: float = 3.0, headers: Optional[dict] = None) -> tuple[int, Any, float]:
    """GET url -> (status, parsed-json-or-text, elapsed_ms). Never raises for HTTP errors."""
    req = urllib.request.Request(url, headers=headers or {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ms = (time.time() - t0) * 1000.0
            try:
                return resp.status, json.loads(body), ms
            except Exception:
                return resp.status, body.decode("utf-8", "replace")[:2000], ms
    except urllib.error.HTTPError as e:
        ms = (time.time() - t0) * 1000.0
        return e.code, e.read().decode("utf-8", "replace")[:500], ms
    except Exception as e:  # noqa: BLE001 — timeout/conn refused/URLError
        ms = (time.time() - t0) * 1000.0
        return 0, str(e), ms


# ------------------------------------------------------- k8s (in-cluster SA)

def _k8s_token() -> str:
    with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
        return f.read().strip()


def _k8s_ctx():
    """SSL context trusting the in-cluster serviceaccount CA (self-signed)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.load_verify_locations("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    return ctx


def k8s_get(path: str) -> Any:
    """GET an API-server path using the in-cluster SA token."""
    url = "https://kubernetes.default.svc" + path
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _k8s_token()})
    with urllib.request.urlopen(req, timeout=8, context=_k8s_ctx()) as resp:  # noqa: S310 — internal cluster DNS
        return json.loads(resp.read())


@cached("overview")
def cluster_overview() -> dict:
    nodes, namespaces, deployments, pods = [], [], [], []
    try:
        for n in k8s_get("/api/v1/nodes").get("items", []):
            ready = any(c["type"] == "Ready" and c["status"] == "True"
                        for c in n.get("status", {}).get("conditions", []))
            nodes.append({
                "name": n["metadata"]["name"],
                "ready": ready,
                "version": n["status"].get("nodeInfo", {}).get("kubeletVersion", ""),
                "ip": next((a.get("address") for a in n.get("status", {}).get("addresses", [])
                            if a.get("type") == "InternalIP"), ""),
                "role": "control-plane" if (n["metadata"].get("labels") or {}).get(
                    "node-role.kubernetes.io/control-plane") else "worker",
            })
    except Exception as e:  # noqa: BLE001
        nodes = [{"error": str(e)}]
    try:
        namespaces = [n["metadata"]["name"] for n in k8s_get("/api/v1/namespaces").get("items", [])]
    except Exception:  # noqa: BLE001
        pass
    try:
        deps = k8s_get("/apis/apps/v1/deployments").get("items", [])
        deployments = [{
            "namespace": d["metadata"]["namespace"],
            "name": d["metadata"]["name"],
            "ready": d.get("status", {}).get("readyReplicas", 0),
            "desired": d.get("spec", {}).get("replicas", 0),
        } for d in deps]
    except Exception:  # noqa: BLE001
        pass
    try:
        pods = k8s_get("/api/v1/pods").get("items", [])
        pods = [{"namespace": p["metadata"]["namespace"], "name": p["metadata"]["name"],
                 "phase": p.get("status", {}).get("phase", "")} for p in pods]
    except Exception:  # noqa: BLE001
        pass
    return {"nodes": nodes, "namespaces": namespaces, "deployments": deployments,
            "pods": pods, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------- services

@cached("services")
def services_with_probes() -> list[dict]:
    """Every IngressRoute hostname -> backing svc:port, probed, up-first."""
    routes = []
    try:
        routes = k8s_get("/apis/traefik.io/v1alpha1/ingressroutes").get("items", [])
    except Exception:  # noqa: BLE001
        return [{"error": "cannot list ingressroutes"}]

    # service ClusterIP lookup: (ns, name) -> (ip, port map)
    svc_map: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for s in k8s_get("/api/v1/services").get("items", []):
            ports = {p.get("name") or str(p.get("port")): p.get("targetPort") or p.get("port")
                     for p in s.get("spec", {}).get("ports", [])}
            svc_map[(s["metadata"]["namespace"], s["metadata"]["name"])] = {
                "ip": s.get("spec", {}).get("clusterIP", ""),
                "ports": ports,
            }
    except Exception:  # noqa: BLE001
        pass

    entries: list[dict] = []
    for r in routes:
        ns = r["metadata"]["namespace"]
        name = r["metadata"]["name"]
        for route in r.get("spec", {}).get("routes", []):
            match = route.get("match", "")
            if "Host(`" not in match:
                continue
            host = match.split("Host(`", 1)[1].split("`", 1)[0]
            for svc_ref in route.get("services", []):
                svc_name = svc_ref.get("name", "")
                svc_port = svc_ref.get("port", 80)
                info = svc_map.get((ns, svc_name), {})
                ip = info.get("ip", "")
                target = info.get("ports", {}).get(str(svc_port)) or svc_port
                probe_url = f"http://{ip}:{target}/" if ip else ""
                entries.append({
                    "host": host,
                    "namespace": ns,
                    "service": svc_name,
                    "url": probe_url,
                    "probe": None,
                })

    def probe_entry(e: dict) -> dict:
        if not e["url"]:
            e["probe"] = {"status": 0, "detail": "no cluster IP", "ms": 0, "up": False}
            return e
        code, body, ms = http_get(e["url"], timeout=2.5)
        # Some services have no route at / (404) but expose /health — retry the
        # common health paths before declaring down.
        if code in (404, 405):
            base = e["url"].rstrip("/")
            for hp in ("/health", "/healthz", "/health/live"):
                c2, b2, ms2 = http_get(base + hp, timeout=2.0)
                if c2 in (200, 201, 202, 204):
                    code, body, ms = c2, b2, ms2
                    break
        up = 100 <= code < 600  # any HTTP response = reachable + serving
        e["probe"] = {"status": code, "detail": body[:120] if isinstance(body, str) else "ok",
                      "ms": round(ms, 1), "up": up}
        return e

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(probe_entry, e) for e in entries]
        entries = [f.result() for f in as_completed(futures)]

    # up first, then by hostname
    entries.sort(key=lambda e: (0 if (e["probe"] or {}).get("up") else 1, e["host"]))
    return entries


# ---------------------------------------------------------------- queues

@cached("queues")
def worker_queues() -> dict:
    out: dict[str, Any] = {}
    # cs-downloader download queue
    code, data, _ = http_get(f"{CS_URL}/api/downloads?limit=500", timeout=5)
    if code == 200 and isinstance(data, list):
        counts: dict[str, int] = {}
        for j in data:
            counts[j.get("status", "?")] = counts.get(j.get("status", "?"), 0) + 1
        out["downloads"] = {
            "counts": counts,
            "total": len(data),
            "recent": sorted(data, key=lambda j: j.get("id", 0), reverse=True)[:8],
        }
    else:
        out["downloads"] = {"error": f"cs-downloader /api/downloads -> {code}"}
    # cs-downloader wanted (attempting)
    code, wdata, _ = http_get(f"{CS_URL}/api/wanted?status=attempting", timeout=5)
    if code == 200 and isinstance(wdata, dict):
        out["wanted_attempting"] = len(wdata.get("wanted", []))
    # Gitea Actions runs (aggregated, from /api/gitea cache)
    try:
        g = gitea_data()
        all_runs = [r for repo in g["repos"] for r in repo.get("runs", [])]
        out["actions"] = {
            "total": len(all_runs),
            "by_status": {
                st: sum(1 for r in all_runs if r.get("status") == st)
                for st in ("queued", "running", "completed")
            },
            "by_conclusion": {
                c: sum(1 for r in all_runs if r.get("conclusion") == c)
                for c in ("success", "failure", "cancelled", "")
            },
            "recent": sorted(all_runs, key=lambda r: r.get("id", 0), reverse=True)[:10],
        }
    except Exception as e:  # noqa: BLE001
        out["actions"] = {"error": str(e)}
    return out


# ---------------------------------------------------------------- gitea

@cached("gitea")
def gitea_data() -> dict:
    if not GITEA_TOKEN:
        return {"repos": [], "error": "GITEA_TOKEN not set"}
    hdr = {"Authorization": "token " + GITEA_TOKEN, "Accept": "application/json"}
    code, data, _ = http_get(f"{GITEA_URL}/api/v1/repos/search?owner=reeds&limit=50&private=true",
                             timeout=8, headers=hdr)
    if code != 200:
        return {"repos": [], "error": f"repos/search -> {code}"}
    repos = data.get("data", []) if isinstance(data, dict) else []
    # skip stale agent-stack archives and empty repos; cap for latency
    repos = [r for r in repos if r.get("size", 0) > 0 and r.get("default_branch")]
    repos.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    repos = repos[:14]

    out_repos = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        def fetch(r: dict) -> dict:
            full = r.get("full_name", "")
            commits, runs = [], []
            c, cdata, _ = http_get(f"{GITEA_URL}/api/v1/repos/{full}/commits?limit=4",
                                   timeout=6, headers=hdr)
            if c == 200 and isinstance(cdata, list):
                commits = [{
                    "sha": x.get("sha", "")[:8],
                    "message": (x.get("commit", {}).get("message", "") or "").split("\n")[0][:80],
                    "author": (x.get("commit", {}).get("author", {}) or {}).get("name", ""),
                    "date": (x.get("commit", {}).get("author", {}) or {}).get("date", ""),
                } for x in cdata]
            r2, rdata, _ = http_get(f"{GITEA_URL}/api/v1/repos/{full}/actions/runs?limit=5",
                                    timeout=6, headers=hdr)
            if r2 == 200 and isinstance(rdata, dict):
                runs = [{
                    "id": x.get("id"),
                    "status": x.get("status", ""),
                    "conclusion": x.get("conclusion", ""),
                    "run_number": x.get("run_number"),
                    "sha": (x.get("head_sha") or "")[:8],
                } for x in rdata.get("workflow_runs", []) or rdata.get("runs", [])]
            return {"name": r.get("name", ""), "full_name": full,
                    "branch": r.get("default_branch", ""),
                    "updated": r.get("updated_at", ""), "commits": commits, "runs": runs}
        for f in as_completed([pool.submit(fetch, r) for r in repos]):
            out_repos.append(f.result())

    out_repos.sort(key=lambda r: r.get("updated") or "", reverse=True)
    return {"repos": out_repos}


# ---------------------------------------------------------------- telemetry

@app.get("/api/telemetry")
def telemetry_feed(limit: int = Query(50, ge=1, le=200)):
    code, data, ms = http_get(f"{TELEMETRY_URL}/feed?limit={limit}", timeout=5)
    if code != 200:
        return JSONResponse({"error": f"telemetry /feed -> {code}", "detail": data}, status_code=502)
    return {"rows": data if isinstance(data, list) else [], "fetched_at": time.time(),
            "latency_ms": round(ms, 1)}


# ----------------------------------------------------------------- routes

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/overview")
def api_overview():
    return cluster_overview()


@app.get("/api/services")
def api_services():
    return {"services": services_with_probes(),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


@app.get("/api/queues")
def api_queues():
    return worker_queues()


@app.get("/api/gitea")
def api_gitea():
    return gitea_data()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
