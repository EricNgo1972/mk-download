# mk-provisioning definition

mk-download runs as one product in mk-provisioning's catalog and one tenant for our own use. Enter the product on
the console's **Catalog** page (it writes the Azure Table rows below), then provision the tenant.

## Product: `Products` table

`PartitionKey` = `Product`, `RowKey` = **`mkdownload`**

| Column | Value | Why |
|---|---|---|
| `Name` | `Maple Download` | Label in the console's pickers. |
| `ImageRepository` | `ghcr.io/ericngo1972/mk-download` | Pushed by *Release Container* (`.github/workflows/release_container.yml`). |
| `ImageTag` | the released version, e.g. `1.0.0` | Pin a version; `latest` also exists. |
| `InternalPort` | `8000` | The service's only HTTP port (`mk-start.sh`). |
| `Memory` | `2g` | Python, yt-dlp, libtorrent and Kelpie idle well under 500 MB; ffmpeg merges and precise clips take the rest. |
| `HealthPath` | `/healthz` | Anonymous; answers once the process is up. |
| `DataMountPath` | `/data` | Jobs database, downloads, cookies, torrent resume data, Kelpie state, the generated link secret. |
| `InjectDotNetDefaults` | `false` | Not a .NET app. |
| `Args` | *(blank)* | |
| `IssuesActivation` | `false` | No MapleKiosk activation: access is the MCP token and the admin password. |
| `ClientPath` | `/settings` | The settings page (admin password). |
| `BackupByDump` | `false` | Downloads are temporary by design. Keep the tenant's backup schedule **Off**. |
| `HostNetwork` | `false` | HTTP through the tunnel is all the agent needs. Torrents and eD2k still work, connecting out; turn it on only if they are too slow (inbound peers, eD2k High ID). |

## Env variables: `ProductEnvVariable` table

`PartitionKey` = `mkdownload`

| `RowKey` | `IsSecret` | `DefaultValue` | `Description` | `Sort` |
|---|---|---|---|---|
| `MCP_TOKEN` | `true` | *(none)* | Required, 16+ characters. The agent sends it as `Authorization: Bearer`. | `1` |
| `ADMIN_PASSWORD` | `true` | *(none)* | Required, 16+ characters, different from `MCP_TOKEN`. Opens `/settings`. | `2` |
| `WEBHOOK_SECRET` | `true` | *(none)* | Signs completion webhooks (`X-MK-Signature`). Without it, jobs can't take a `callback_url`. | `3` |
| `LINK_SECRET` | `true` | *(none)* | Optional. Signs download links; generated once and kept in `/data` when unset. Changing it voids every link. | `4` |
| `PUBLIC_URL` | `false` | *(none)* | Optional, e.g. `https://download.maplekiosk.ca`. Links are built on it; when unset, the service uses the host the agent reaches `/mcp` on. | `5` |
| `PROXY_URL` | `false` | *(none)* | Optional, for when the host's IP gets blocked by a video site: `socks5://user:pass@host:1080` or `http://host:3128`. Also editable on the settings page. | `6` |

Generate each secret with `openssl rand -hex 24`. A tenant without `MCP_TOKEN` or `ADMIN_PASSWORD` fails its health check
with `refusing to start` in `docker logs tenant-{key}`; give it values and **Retry** / **Redeploy**. A container's env
is fixed at creation: change a value, then **Redeploy**.

## Tenant

Only on a host we own (`spc-nuc`, `lenovo`, `lenovo-m02`, `lenovo-mini`): BitTorrent and eD2k traffic leaves from the
host's public IP, and copyright notices must never reach a customer's premises.

New tenant on the console (or the agent's `provision_tenant`): slug `download`, product `mkdownload`, domain
`maplekiosk.ca`, host `spc-nuc`, backups **Off**. Set `MCP_TOKEN`, `ADMIN_PASSWORD` and `WEBHOOK_SECRET` before the
first deploy.

## After the first deploy

1. Open `https://download.maplekiosk.ca/settings`, sign in with `ADMIN_PASSWORD`, check the limits.
2. Upload cookies for the sites that need a login (Instagram and Facebook Reels always; YouTube for age-restricted
   videos; Bilibili for HD). Export them with a browser extension such as *Get cookies.txt LOCALLY*, from a
   browser profile you keep only for this.
3. Add the agent's MCP server: URL `https://download.maplekiosk.ca/mcp`, transport Streamable HTTP, header
   `Authorization: Bearer <MCP_TOKEN>`. For Claude Code:
   `claude mcp add --transport http mk-download https://download.maplekiosk.ca/mcp --header "Authorization: Bearer <MCP_TOKEN>"`
