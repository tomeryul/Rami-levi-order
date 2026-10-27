# Rami Levy MCP connector

FastMCP (Streamable HTTP) + Playwright, Azure Container Apps (israelcentral), OAuth via Entra ID, secrets in Key Vault.

## Tools
| Tool | What |
|---|---|
| `login` | Logs in with email+password from Key Vault. Returns `awaiting_code` when an SMS is sent |
| `submit_sms_code` | You send the SMS code in chat → Claude passes it here |
| `search_product` | `POST /api/catalog` for the account's store |
| `add_to_cart` | Absolute quantity (0 = remove). Pushes the whole cart via `POST /api/v2/cart` |
| `get_cart` | Connector-side mirror (no known cart-read endpoint yet) |
| `get_shopping_list` | **Discovery mode** — returns the raw API data the lists page loads |
| `prepare_order` | Two-phase: summary + `confirmation_id` → only after your explicit "yes" a second call. Never pays |
| `debug_*`, `mapping_report` | Mapping tools, only while `DEBUG_TOOLS=1` |

## Safety
- No payment data ever passes through the server; it refuses to click payment / submit-order buttons.
- `prepare_order` phase 2 requires an id tied to the exact cart contents, valid for 15 min.
- OAuth: Entra app with *assignment required* — only your user can connect.
- Captures under `/data/captures` redact passwords, codes, tokens, card fields, email and phone.

## Setup (from your phone: Azure Cloud Shell)
1. Push this repo to a personal GitHub repo.
2. Cloud Shell → `git clone … && cd rami-levy-mcp && GITHUB_REPO=<you>/rami-levy-mcp bash infra/bootstrap.sh`
3. Copy the printed values into the repo's Actions **Variables**.
4. Claude → Settings → Connectors → Add custom connector → `https://<fqdn>/mcp`.

Every push to `main` builds in ACR and rolls out a new revision.

## Known risk
Rami Levy sits behind Cloudflare. If `login` returns a Cloudflare error, the Azure IP is being challenged — the server
does not try to bypass it. The fallback is running the same container on a home machine.
