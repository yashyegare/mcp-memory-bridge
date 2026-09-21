# Deploying the memory server on GCP free tier

The target: one always-free `e2-micro` VM (us-central1 / us-east1 /
us-west1), running the **lean** server in HTTP mode, reachable from your
laptop through a **Cloudflare Tunnel** — no public IP on the VM, no open
firewall ports, TLS at the Cloudflare edge. Everything below is chosen so
the monthly bill stays $0 even after the trial ends.

## Why this shape

- **Lean mode on the VM** (`MCP_EMBEDDINGS=off`): e2-micro has 1GB RAM and
  the embedding model wants ~500MB plus a slow first load. Semantic search
  stays a laptop feature; the remote server serves the four storage tools.
- **No public IP**: the e2-micro free allowance is VM hours + 30GB disk +
  1GB egress; external IPv4 addresses are charged on paid accounts. The
  tunnel needs **no inbound ports at all** — cloudflared dials out to
  Cloudflare, so the VM keeps no external IP and no firewall openings.
- **No egress concerns**: your traffic is key/values, megabytes at most.
- **One demo domain**: you need a domain on Cloudflare (free plan is fine).
  Without one, the alternative is `cloudflared`'s quick-tunnel URL
  (trycloudflare.com, ephemeral, fine for a one-off demo) — no account or
  domain needed.

## Bill safety, in order of appearance

1. **Free Trial billing accounts cannot charge you.** $300 credit / 90
   days; when it lapses, resources stop. Charging starts only if you
   *manually upgrade* to a paid billing account (console → Billing →
   check whether the account says "Free trial account" or "Paid account").
2. Even on a paid account this deploy uses only Always-Free resources:
   **e2-micro in us-west1** (or us-central1/us-east1), **30GB standard
   disk**, ~1GB egress. Budget alerts make any drift loud.
3. **The 100% guarantee is deletion**: a deleted *project* is unrecoverable
   and produces no further billing. Schedule it — see the teardown section.

## Step 0 — console prerequisites (~10 min)

1. console.cloud.google.com → top bar → confirm/select your project (or
   create one, e.g. `mcp-memory-bridge`).
2. **Billing → Budgets & alerts → Create budget**: scope = your project,
   amount = **$1**, thresholds 50/90/100%. Email = yours. This is the
   alarm bell; it emails you at $0.50.
3. Note your **project ID** (the console shows it under the project name;
   you'll need it for gcloud commands).

## Step 1 — the VM (~10 min)

Console → **Compute Engine → VM instances → Create instance**:

| Field | Value |
|---|---|
| Name | `memory-server` |
| Region | `us-west1` (Oregon) — always-free; us-central1/us-east1 also OK |
| Zone | any (e.g. `us-west1-b`) |
| Machine type | **e2-micro** (2 vCPU shared, 1GB RAM) |
| Boot disk → Change | **Standard persistent disk**, **30GB**, OS: **Debian 12** |
| Identity | leave defaults (one service account is fine) |
| Firewall | leave **both boxes unchecked** — we open nothing |

**Networking detail that saves $3–4/month**: after creation, click the
instance → **Edit → Network interfaces → default → External IP →** set to
**None**, save. (Debian + cloudflared need no inbound or static outbound
IP; outbound uses Google's transient NAT.)

Optional, if you prefer CLI over console for the rest: click the SSH
button's dropdown → there's a `gcloud` command-line equivalent for every
step below; the browser SSH window is all you strictly need.

## Step 2 — code + setup on the VM (~10 min)

Open **SSH** (browser window) on the instance, then:

```bash
sudo apt-get update && sudo apt-get install -y git python3-venv curl
git clone https://github.com/yashyegare/mcp-memory-bridge.git
cd mcp-memory-bridge
sudo bash deploy/setup.sh
```

`setup.sh` creates a `memory` user, a lean venv (`mcp` + `numpy` only —
no torch), installs the systemd service, and installs cloudflared.

Generate a token for the server (any long random string):

```bash
openssl rand -hex 32
```

Write the env file (replace the token with the value you just got):

```bash
sudo tee /opt/mcp-memory/env >/dev/null <<EOF
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_AUTH_TOKEN=<paste-your-token>
MCP_EMBEDDINGS=off
MCP_CLIENT_ID=gcp-server
EOF
sudo chmod 600 /opt/mcp-memory/env && sudo chown memory:memory /opt/mcp-memory/env
sudo systemctl restart memory-server
sudo systemctl status memory-server --no-pager   # want: active (running)
```

Verify it answers locally:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
# expect: 401   (the auth layer works; we haven't sent the token)
```

## Step 3 — decide your tunnel type

**Option A — Named tunnel (recommended, needs a domain on Cloudflare,
free plan OK):** stable URL like `memory.yourdomain.com`, real TLS, session
survives reboots.

**Option B — Quick tunnel (zero setup, no domain):** one command gives you
an ephemeral `https://<random>.trycloudflare.com` URL. Great for the first
demo; the URL changes every run.

Do A if you have any domain; B otherwise.

## Step 4 — Option A, named tunnel (~10 min)

1. In the VM's SSH window: `cloudflared tunnel login` — it prints a URL;
   open it, pick your domain, authorize. This writes
   `/root/.cloudflared/cert.pem` (or the user's home you ran it from).
2. Create and route:
   ```bash
   cloudflared tunnel create mcp-memory
   cloudflared tunnel route dns mcp-memory memory.yourdomain.com
   ```
3. Config file (`/etc/cloudflared/config.yml`, as root):
   ```yaml
   tunnel: <tunnel-UUID-from-create-output>
   credentials-file: /root/.cloudflared/<tunnel-UUID>.json
   ingress:
     - hostname: memory.yourdomain.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```
4. Install as a service:
   ```bash
   cloudflared service install
   systemctl enable --now cloudflared
   ```
   Cloudflare Zero Trust dashboard → Networks → Tunnels should now list
   `mcp-memory` as HEALTHY.

## Step 4 — Option B, quick tunnel (~2 min, no domain)

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# prints: https://<random-words>.trycloudflare.com
```

Keep that SSH window open; the URL dies with the process.

## Step 5 — prove it from your laptop (~5 min)

```powershell
# Windows, from the repo:
venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'client'); from raw_client import RawHTTPMCPClient; c=RawHTTPMCPClient('https://YOUR-TUNNEL-HOST/mcp', token='THE-TOKEN-YOU-SET'); c.initialize(); print([t['name'] for t in c.list_tools()]); print(c.call_tool('memory_set',{'key':'cloud/hello','value':'written from my laptop','client_id':'laptop'}).get('content')[0].get('text')); c.close()"
```

Then the two-machine demo: `RawHTTPMCPClient` from your laptop while a
friend (or a second machine/VM) hammers the same key — the events log on
the server (`tools/inspect_memory.py` in the VM SSH window) shows the
interleaved, attributed writes from genuinely different machines.

## Day-2 operations cheat sheet (on the VM)

```bash
sudo systemctl status memory-server     # is the API up?
sudo journalctl -u memory-server -n 50  # last 50 log lines
sudo systemctl restart memory-server    # after code changes
cd ~/mcp-memory-bridge && git pull && sudo bash deploy/setup.sh   # update
venv alias not needed: /opt/mcp-memory/venv/bin/python server/memory_server.py --help
```

## Teardown — the only 100% guarantee

When the demo's purpose is served (or day ~55 of the trial arrives):

1. Console → **Compute Engine → VM instances** → tick `memory-server` →
   **Delete**.
2. Console → **IAM & Admin → Settings → Shut down** (deletes the whole
   project and everything in it after ~30 days).
3. Optional belt-and-suspenders: Billing → Account management → **close**
   the billing account, and remove the payment method if you're done with
   GCP entirely.

A calendar reminder for **day ~55** with the single word "teardown" is the
most reliable ops tool in this document.
