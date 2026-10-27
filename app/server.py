"""Rami Levy connector — FastMCP over Streamable HTTP.

Tools: login, submit_sms_code, get_shopping_list, get_cart, search_product,
add_to_cart, prepare_order (+ mapping/debug tools while DEBUG_TOOLS=1).

Rules baked in:
- The server never enters or receives payment details and never presses a
  payment / final-submit control.
- prepare_order is two-phase: the first call returns a summary + confirmation_id;
  only a second call with that id (after the user said yes in chat) continues.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import secrets
import time
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import Field

from browser import CHECKOUT, SITE, CloudflareBlocked, NotLoggedIn, Session

DEBUG_TOOLS = os.environ.get("DEBUG_TOOLS") == "1"
SESSION = Session()
PENDING: dict[str, dict[str, Any]] = {}  # confirmation_id -> {hash, expires}
CONFIRM_TTL = 15 * 60


# ---------------------------------------------------------------------- auth
def build_auth():
    if os.environ.get("AUTH_DISABLED") == "1":  # local testing only
        return None
    from fastmcp.server.auth.providers.azure import AzureProvider

    return AzureProvider(
        client_id=os.environ["ENTRA_CLIENT_ID"],
        client_secret=os.environ["ENTRA_CLIENT_SECRET"],
        tenant_id=os.environ["ENTRA_TENANT_ID"],
        base_url=os.environ["PUBLIC_BASE_URL"],
        required_scopes=["access"],
        jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:*",
        ],
    )


@lifespan
async def browser_lifespan(server):
    yield {"session": SESSION}
    await SESSION.close()


mcp = FastMCP(
    "Rami Levy",
    instructions=(
        "Shopping at Rami Levy (Israeli supermarket). Search in Hebrew. "
        "If a tool says the session expired, call `login`; if it returns awaiting_code, ask the user for the SMS code "
        "and call `submit_sms_code`. Never call prepare_order with a confirmation_id unless the user explicitly approved "
        "the exact summary in chat. Payment is always done by the user on the site, never through this server."
    ),
    auth=build_auth(),
    lifespan=browser_lifespan,
)


def guarded(fn):
    """Serialize browser access and turn known failures into readable tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with SESSION.lock:
            try:
                return await fn(*args, **kwargs)
            except NotLoggedIn as e:
                raise ToolError(f"{e} Call `login`.") from e
            except CloudflareBlocked as e:
                raise ToolError(
                    f"{e} Retrying won't help. This server's outbound IP is challenged by the site's bot protection; "
                    "it has to run from an IP the site accepts (e.g. a home machine)."
                ) from e
            except PermissionError as e:
                raise ToolError(str(e)) from e

    return wrapper


def _cart_hash(cart: dict) -> str:
    return hashlib.sha256(json.dumps(cart, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------- login
@mcp.tool(annotations=ToolAnnotations(title="Log in to Rami Levy", open_world_hint=True))
@guarded
async def login() -> dict:
    """Start (or verify) the Rami Levy login with the stored credentials.
    Returns status: logged_in | awaiting_code (ask the user for the SMS code) | unknown."""
    status = await SESSION.login_start(os.environ["RAMI_LEVY_EMAIL"], os.environ["RAMI_LEVY_PASSWORD"])
    hint = {
        "awaiting_code": "An SMS code was sent to the account's phone. Ask the user for it, then call submit_sms_code.",
        "logged_in": "Logged in.",
        "unknown": "Login result unclear — call debug_snapshot to see the screen.",
    }[status]
    return {"status": status, "store_id": SESSION.store_id, "hint": hint}


@mcp.tool(annotations=ToolAnnotations(title="Submit SMS login code", open_world_hint=True))
@guarded
async def submit_sms_code(code: Annotated[str, Field(pattern=r"^\d{4,8}$", description="Digits from the SMS")]) -> dict:
    """Finish login with the SMS code the user received."""
    if SESSION.login_state != "awaiting_code":
        raise ToolError("No login is waiting for a code. Call `login` first.")
    status = await SESSION.login_submit_code(code)
    return {"status": status, "store_id": SESSION.store_id}


# ---------------------------------------------------------------------- catalog / cart
@mcp.tool(annotations=ToolAnnotations(title="Search products", read_only_hint=True, open_world_hint=True))
@guarded
async def search_product(
    query: Annotated[str, Field(min_length=1, description="Product name, in Hebrew")],
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict:
    """Search the Rami Levy catalog for the account's store. Prices are current shelf prices."""
    await SESSION.ensure()
    return {"query": query, "store_id": SESSION.store_id, "results": await SESSION.search(query, limit)}


@mcp.tool(annotations=ToolAnnotations(title="Add to cart", destructive_hint=False, open_world_hint=True))
@guarded
async def add_to_cart(
    product_id: Annotated[str, Field(pattern=r"^\d+$")],
    quantity: Annotated[float, Field(ge=0, le=99, description="Units, or kg for by-weight items. 0 removes the item.")] = 1,
    name: str | None = None,
    price: float | None = None,
) -> dict:
    """Set the quantity of a product in the account's cart (absolute, not incremental). 0 removes it.
    The website shows server-side cart changes when the checkout page is (re)loaded."""
    await SESSION.ensure()
    res = await SESSION.set_item(product_id, quantity, name, price)
    return {
        "items": [{"product_id": k, **v} for k, v in res["cart"].items()],
        "rejected": res["rejected"],
        "products_total": res["products_total"],
        "delivery_fee": res["delivery_fee"],
        "checkout_url": CHECKOUT,
    }


@mcp.tool(annotations=ToolAnnotations(title="Get cart", read_only_hint=True))
@guarded
async def get_cart() -> dict:
    """What this connector has put in the cart since the last order.
    Rami Levy has no known cart-read endpoint yet, so edits made directly on the site are not reflected."""
    cart = SESSION.cart_mirror()
    est = sum(float(v.get("price") or 0) * float(v["quantity"]) for v in cart.values())
    return {
        "scope": "connector_mirror",
        "items": [{"product_id": k, **v} for k, v in cart.items()],
        "estimated_total": round(est, 2),
        "checkout_url": CHECKOUT,
    }


# ---------------------------------------------------------------------- shopping lists (discovery)
@mcp.tool(annotations=ToolAnnotations(title="Get shopping list", read_only_hint=True, open_world_hint=True))
@guarded
async def get_shopping_list(name: str | None = None) -> dict:
    """Read the account's saved shopping list(s) from the site.
    Mapping phase: returns the raw API data the lists page loads."""
    await SESSION.ensure()
    t0 = time.time()
    await SESSION.goto(f"{SITE}/he/dashboard")
    for label in ("רשימות הקניות", "רשימות קניות", "הרשימות שלי", "רשימות"):
        try:
            await SESSION.click_text(label)
            break
        except Exception:
            continue
    if name:
        try:
            await SESSION.click_text(name)
        except Exception:
            pass
    calls = [c for c in SESSION.captured_since(t0) if "list" in c.url.lower()]
    return {
        "mode": "discovery",
        "page": SESSION.page.url if SESSION.page else None,
        "api_calls": [{"method": c.method, "url": c.url, "status": c.status, "data": c.resp_body} for c in calls],
        "note": "If api_calls is empty, run debug_snapshot to find the lists page.",
    }


# ---------------------------------------------------------------------- order (two-phase, no payment)
@mcp.tool(
    annotations=ToolAnnotations(title="Prepare order (stops before payment)", destructive_hint=True, open_world_hint=True)
)
@guarded
async def prepare_order(
    confirmation_id: Annotated[
        str | None,
        Field(description="Leave empty for the summary. Pass the id ONLY after the user explicitly approved it in chat."),
    ] = None,
) -> list:
    """Phase 1 (no id): sync the cart, open checkout, return a summary and a confirmation_id.
    Phase 2 (with id, after explicit user approval): finalize up to the payment step and return the link.
    Payment details never pass through this server — the user pays on the site."""
    await SESSION.ensure()
    cart = SESSION.cart_mirror()
    if not cart:
        raise ToolError("Cart is empty.")

    if confirmation_id is None:
        sync = await SESSION.sync_cart(cart)
        t0 = time.time()
        await SESSION.goto(CHECKOUT)
        page_text = (await SESSION.page.inner_text("body"))[:2500] if SESSION.page else ""
        cid = secrets.token_hex(4)
        PENDING.clear()
        PENDING[cid] = {"hash": _cart_hash(cart), "expires": time.time() + CONFIRM_TTL}
        summary = {
            "confirmation_id": cid,
            "items": [{"product_id": k, **v} for k, v in cart.items()],
            "products_total": sync["products_total"],
            "delivery_fee": sync["delivery_fee"],
            "rejected": sync["rejected"],
            "checkout_page_text": page_text,
            "checkout_api_calls": [
                {"method": c.method, "url": c.url, "status": c.status} for c in SESSION.captured_since(t0)
            ],
            "next": "Show this summary to the user. Only after an explicit yes, call prepare_order(confirmation_id=...).",
        }
        return [Image(data=await SESSION.screenshot(), format="jpeg"), json.dumps(summary, ensure_ascii=False)]

    pend = PENDING.get(confirmation_id)
    if not pend or pend["expires"] < time.time():
        raise ToolError("Unknown or expired confirmation_id. Call prepare_order() again and re-confirm with the user.")
    if pend["hash"] != _cart_hash(cart):
        raise ToolError("Cart changed after the summary. Call prepare_order() again and re-confirm with the user.")
    PENDING.pop(confirmation_id, None)

    await SESSION.sync_cart(cart)
    await SESSION.goto(CHECKOUT)
    # Delivery-slot / address steps get wired here once the checkout flow is mapped.
    # By design this stops before the payment step.
    result = {
        "status": "ready_for_payment",
        "checkout_url": CHECKOUT,
        "message": "Cart is in the account and checkout is open. Choose the delivery slot and pay on the site/app.",
    }
    return [Image(data=await SESSION.screenshot(), format="jpeg"), json.dumps(result, ensure_ascii=False)]


# ---------------------------------------------------------------------- mapping tools (DEBUG_TOOLS=1)
if DEBUG_TOOLS:

    @mcp.tool(annotations=ToolAnnotations(title="Debug: screenshot + controls", read_only_hint=True))
    @guarded
    async def debug_snapshot() -> list:
        """Screenshot of the server's browser plus visible inputs/buttons (for mapping)."""
        await SESSION.ensure()
        return [Image(data=await SESSION.screenshot(), format="jpeg"), json.dumps(await SESSION.visible_controls(), ensure_ascii=False)]

    @mcp.tool(annotations=ToolAnnotations(title="Debug: open a site path", read_only_hint=True))
    @guarded
    async def debug_goto(path: Annotated[str, Field(pattern=r"^/[^\s]*$", description="Path on rami-levy.co.il, e.g. /he/dashboard")]) -> list:
        """Navigate the server's browser to a path on rami-levy.co.il and return what loaded."""
        await SESSION.ensure()
        t0 = time.time()
        await SESSION.goto(SITE + path)
        calls = [{"method": c.method, "url": c.url, "status": c.status} for c in SESSION.captured_since(t0)]
        return [Image(data=await SESSION.screenshot(), format="jpeg"), json.dumps({"api_calls": calls, **await SESSION.visible_controls()}, ensure_ascii=False)]

    @mcp.tool(annotations=ToolAnnotations(title="Debug: click by text", destructive_hint=True))
    @guarded
    async def debug_click(text: str) -> list:
        """Click the first element containing this text. Payment / final-submit buttons are refused."""
        await SESSION.ensure()
        t0 = time.time()
        await SESSION.click_text(text)
        calls = [{"method": c.method, "url": c.url, "status": c.status, "data": c.resp_body} for c in SESSION.captured_since(t0)]
        return [Image(data=await SESSION.screenshot(), format="jpeg"), json.dumps({"api_calls": calls, **await SESSION.visible_controls()}, ensure_ascii=False)]

    @mcp.tool(annotations=ToolAnnotations(title="Debug: API map", read_only_hint=True))
    async def mapping_report() -> dict:
        """All JSON endpoints seen so far (method, path, statuses, request/response keys). Secrets are redacted."""
        return {"endpoints": SESSION.endpoint_report(), "auth_headers_captured": sorted(SESSION.auth.keys()), "store_id": SESSION.store_id}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True, "login_state": SESSION.login_state})


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
