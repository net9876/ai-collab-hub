# Costs (East US, pay-as-you-go, prices checked 2026-09-28)

Prices from the Azure Retail Prices API (`prices.azure.com`) and Azure pricing
pages on the date above. Verify again before applying; USD, excluding tax.

## Estimate

| Resource | SKU / setting | Pricing | Assumed usage | Est. / month |
|---|---|---|---|---|
| Container Apps | consumption, 0.25 vCPU / 0.5 GiB, min 1 / max 2 | $0.000024 per vCPU-s active, $0.000003 per GiB-s, $0.40 per 1M requests; **free each month: 180k vCPU-s, 360k GiB-s, 2M requests per subscription** | 2 h active/day ≈ 54k vCPU-s, 108k GiB-s, < 50k requests | **$0** (inside the free grant when the subscription has no other Container Apps) |
| Container Apps idle (always-on replica) | `min_replicas = 1` (since 2026-10-04) | idle: $0.000003 per vCPU-s, $0.000003 per GiB-s | 1 × 0.25 vCPU / 0.5 GiB × 2.59M s ≈ 648k vCPU-s + 1.30M GiB-s; minus the free grant | **≈ $4–6** (≈ $5.8 before grant; the grant is shared by all apps in the subscription) |
| Container Registry | Basic | $0.1666/day | always on | **≈ $5.00** |
| Storage (data) | StorageV2 Hot LRS, versioning | $0.0208/GB-month; writes $0.05 per 10k; reads $0.004 per 10k; table ops similar order | < 1 GB, < 100k ops | **≈ $0.05–0.30** |
| Storage (Terraform state) | Hot LRS | same | KBs | **< $0.01** |
| Log Analytics | PerGB2018, 30 d retention, daily cap 0.2 GB | $2.30/GB after the first 5 GB/month per billing account | ACA + storage write logs ≈ 0.1–0.5 GB/month | **$0–1** typical |
| Entra ID app registrations, managed identity, RBAC, GitHub OIDC | — | free | — | $0 |
| Key Vault | not created | — | — | $0 |
| GitHub | private repo, Actions | free plan includes 2,000 Actions minutes/month (private) | CI ≈ 5 min/run | $0 |

**Expected total: ≈ $9–12 per month**: ACR Basic (~$5) + the always-on replica (~$4–6). With `min_replicas = 0` it drops back to ≈ $5–7, at the price of an ~18 s cold start that broke connector setup/refresh (2026-10-04).

**Upper bound with the caps as configured:** Log Analytics can ingest at most
0.2 GB/day (≈ 6 GB/month ≈ $14 worst case if the free 5 GB is already used by
other workspaces); Container Apps is bounded by `max_replicas = 2` (a
pathological 24/7 load at 2 × 0.25 vCPU ≈ 1.3M vCPU-s ≈ $27). Realistic
worst case for personal use stays under ~$20.

## Minimum / always-on charges

- ACR Basic is billed per day whether used or not.
- The always-on Container Apps replica (`min_replicas = 1`) is billed at the idle rate around the clock.
- Everything else is usage-based.
- The free Container Apps grant is shared with every app in the subscription.

## Cheaper alternatives (not chosen)

- **Reuse an existing registry** in the subscription (saves ~$5/month) — couples
  lifecycles and permissions with another project; ask before doing this.
- **GHCR** — needs a long-lived pull token in the app (a stored secret) or a
  public image (would publish private code). Rejected.
- **Disable storage diagnostics** (`enable_storage_diagnostics = false`) to
  save a few cents of log ingestion.

## Durable events and the bridge (2026-10-11)

No new resources. Each event is one extra row (≈ 0.5–1 KB) in the write transaction
that already happens; at most one extra write operation (≈ $0.05 per 10k). Per bridge poll:

- 1 Container Apps request (`tools/call event_poll`; the MCP session is kept open, the
  handshake repeats only when the token cache changes, roughly hourly);
- 1 table point read (the subscription) + 1 range query per kind in scope (3 by
  default), each normally one page;
- 1 `tool_call` log line (≈ 200 bytes);
- an `event_ack` (point reads + 1 write) only when there were events.

At the default 45 s interval: ≈ 1,920 polls/day ≈ 58k requests/month (the free grant is
2M), ≈ 230k table reads/month (≈ $0.10 at $0.004 per 10k), ≈ 12 MB of logs/month (inside
the free 5 GB and far below the 0.2 GB/day cap). CPU per poll is milliseconds on the
always-on replica: **≈ $0.10/month per running bridge**. With `min_replicas = 0` a
running bridge would keep a replica warm (polls every 45 s are shorter than the
scale-in cooldown), which costs the idle rate above; use `--interval` ≥ 600 s there or
stop the bridge when not needed. Event retention is unbounded in v1: 100 events/day ≈
40 MB/year ≈ $0.01/month.

## Teardown cost

After `terraform destroy` of the main stack only the state account remains
(< $0.01/month). Soft-deleted blobs are retained for the configured days.
