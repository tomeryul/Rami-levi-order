"""Playwright session for rami-levy.co.il.

One long-lived Chromium context. All API calls run *inside* the page with
fetch(), so they carry the same cookies/origin as the real site. Auth headers
(Authorization + ecomtoken) are captured from the site's own requests, so we
don't need to know where the SPA stores them.

Nothing here touches payment details, and nothing here tries to defeat
Cloudflare: if a challenge page appears, we stop and report it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)

SITE = "https://www.rami-levy.co.il"
HOME = f"{SITE}/he"
CHECKOUT = f"{SITE}/he/dashboard/checkout"
SEARCH_URL = f"{SITE}/api/catalog"
CART_URL = f"{SITE}/api/v2/cart"
ORDERS_URL = "https://www-api.rami-levy.co.il/api/v3/site/orders"

RL_HOST = re.compile(r"(^|\.)rami-levy\.co\.il$")
# Words on buttons the server must never press on its own (payment / final submit).
FORBIDDEN_CLICK = re.compile(r"תשלום|לתשלום|שלם|בצע\s*הזמנה|אישור\s*הזמנה|סיום\s*הזמנה|pay", re.I)
SENSITIVE_KEYS = re.compile(r"pass|otp|sms|^code$|verif|token|card|cvv|credit|ashrai|אשראי|mail|phone", re.I)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "browser-state.json"
CART_FILE = DATA_DIR / "cart-mirror.json"
CAPTURE_DIR = DATA_DIR / "captures"


class CloudflareBlocked(Exception):
    pass


class NotLoggedIn(Exception):
    pass


@dataclass
class Captured:
    ts: float
    method: str
    url: str
    status: int
    req_body: Any
    resp_body: Any


@dataclass
class Session:
    pw: Playwright | None = None
    browser: Browser | None = None
    ctx: BrowserContext | None = None
    page: Page | None = None
    auth: dict[str, str] = field(default_factory=dict)  # authorization / ecomtoken
    store_id: str | None = os.environ.get("RAMI_LEVY_STORE") or None
    login_state: str = "unknown"  # unknown | awaiting_code | logged_in
    capture: list[Captured] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------------ lifecycle
    async def ensure(self) -> Page:
        if self.page and not self.page.is_closed():
            return self.page
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        self.ctx = await self.browser.new_context(
            storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            viewport={"width": 1366, "height": 900},
        )
        self.ctx.on("request", self._on_request)
        self.ctx.on("response", lambda r: asyncio.ensure_future(self._on_response(r)))
        self.page = await self.ctx.new_page()
        await self.goto(HOME)
        return self.page

    async def close(self) -> None:
        try:
            if self.ctx:
                await self.save_state()
                await self.ctx.close()
            if self.browser:
                await self.browser.close()
            if self.pw:
                await self.pw.stop()
        finally:
            self.page = self.ctx = self.browser = self.pw = None

    async def save_state(self) -> None:
        if self.ctx:
            await self.ctx.storage_state(path=str(STATE_FILE))

    # ------------------------------------------------------------------ network hooks
    def _on_request(self, req: Request) -> None:
        host = urlparse(req.url).hostname or ""
        if not RL_HOST.search(host):
            return
        h = req.headers
        auth = h.get("authorization", "")
        if auth.lower().startswith("bearer ") and len(auth) > 20:
            self.auth["authorization"] = auth
        if h.get("ecomtoken"):
            self.auth["ecomtoken"] = h["ecomtoken"]
        # The site's own catalog/cart calls tell us which store the account shops from.
        if req.method == "POST" and ("/api/catalog" in req.url or "/api/v2/cart" in req.url):
            try:
                body = json.loads(req.post_data or "{}")
                if body.get("store"):
                    self.store_id = str(body["store"])
            except (ValueError, TypeError):
                pass

    async def _on_response(self, resp: Response) -> None:
        req = resp.request
        host = urlparse(resp.url).hostname or ""
        if not RL_HOST.search(host) or req.resource_type not in ("xhr", "fetch"):
            return
        try:
            body: Any = await resp.json()
        except Exception:
            return
        rec = Captured(time.time(), req.method, resp.url, resp.status, _redact(_safe_json(req.post_data)), _truncate(body))
        self.capture.append(rec)
        self.capture = self.capture[-400:]
        with (CAPTURE_DIR / f"{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ page helpers
    async def goto(self, url: str) -> None:
        page = self.page
        assert page
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self._settle()
        await self._check_cloudflare(resp)

    async def _settle(self) -> None:
        """Wait for the SPA's XHRs. Analytics can keep the network busy forever, so idle is best-effort."""
        assert self.page
        try:
            await self.page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        await asyncio.sleep(0.5)  # let response handlers finish parsing JSON

    async def _check_cloudflare(self, resp: Response | None = None) -> None:
        page = self.page
        assert page
        title = (await page.title()) or ""
        mitigated = bool(resp and resp.headers.get("cf-mitigated"))
        if mitigated or "just a moment" in title.lower() or await page.locator("#challenge-form, iframe[src*='challenges.cloudflare']").count():
            raise CloudflareBlocked("Cloudflare challenge page — the server's IP is being challenged.")

    async def screenshot(self) -> bytes:
        assert self.page
        return await self.page.screenshot(type="jpeg", quality=60, full_page=False)

    async def visible_controls(self) -> dict[str, list[str]]:
        """Short inventory of what's on screen — used for mapping from chat."""
        assert self.page
        return await self.page.evaluate(
            """() => {
              const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
              const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 60);
              const inputs = [...document.querySelectorAll('input,select,textarea')].filter(vis)
                .map(e => `${e.tagName.toLowerCase()} type=${e.type||''} name=${e.name||''} id=${e.id||''} placeholder=${e.placeholder||''} autocomplete=${e.autocomplete||''}`);
              const buttons = [...document.querySelectorAll('button,a,[role=button]')].filter(vis).map(txt).filter(Boolean);
              return { url: [location.href], inputs: inputs.slice(0, 40), buttons: [...new Set(buttons)].slice(0, 80) };
            }"""
        )

    async def click_text(self, text: str) -> None:
        if FORBIDDEN_CLICK.search(text):
            raise PermissionError("Refusing to press a payment/submit-order control from the server.")
        assert self.page
        loc = self.page.get_by_text(text, exact=False).first
        actual = (await loc.inner_text(timeout=10_000)).strip()
        if FORBIDDEN_CLICK.search(actual):  # e.g. "המשך" matching "המשך לתשלום"
            raise PermissionError(f"Refusing to press '{actual[:40]}' — payment/submit-order control.")
        await loc.click(timeout=10_000)
        await self._settle()
        await self._check_cloudflare()

    # ------------------------------------------------------------------ login (password + SMS)
    async def is_logged_in(self) -> bool:
        if not self.auth.get("authorization"):
            return False
        try:
            await self.api("GET", f"{ORDERS_URL}?page=1&activeFilter=0")
            return True
        except NotLoggedIn:
            return False

    async def login_start(self, email: str, password: str) -> str:
        page = await self.ensure()
        if await self.is_logged_in():
            self.login_state = "logged_in"
            return "logged_in"
        # Open the login dialog. Text is matched loosely; mapping will pin real selectors.
        for label in ("התחברות", "כניסה", "התחבר"):
            loc = page.get_by_role("button", name=re.compile(label)).or_(page.get_by_role("link", name=re.compile(label)))
            if await loc.count():
                await loc.first.click()
                break
        await page.wait_for_timeout(1500)

        email_in = page.locator("input[type=email], input[name*=mail i], input[autocomplete=email], input[autocomplete=username]").first
        pass_in = page.locator("input[type=password]").first
        if await email_in.count() and await pass_in.count():
            await email_in.fill(email)
            await pass_in.fill(password)
            await pass_in.press("Enter")
        else:
            raise RuntimeError("Login form fields not found — use debug_snapshot to map the login dialog.")

        await page.wait_for_timeout(4000)
        await self._check_cloudflare()
        if await self._otp_input().count():
            self.login_state = "awaiting_code"
            return "awaiting_code"
        if await self.is_logged_in():
            self.login_state = "logged_in"
            await self.save_state()
            return "logged_in"
        self.login_state = "unknown"
        return "unknown"

    def _otp_input(self):
        assert self.page
        return self.page.locator(
            "input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], input[inputmode=numeric][maxlength]"
        )

    async def login_submit_code(self, code: str) -> str:
        page = await self.ensure()
        boxes = self._otp_input()
        n = await boxes.count()
        if n == 0:
            raise RuntimeError("No SMS-code field on screen — call login first.")
        if n >= len(code):  # one box per digit
            for i, d in enumerate(code):
                await boxes.nth(i).fill(d)
        else:
            await boxes.first.fill(code)
        await boxes.last.press("Enter")
        await page.wait_for_timeout(5000)
        # Trigger an authenticated call from the site itself so the token gets captured.
        await self.goto(f"{SITE}/he/dashboard/orders")
        if await self.is_logged_in():
            self.login_state = "logged_in"
            await self.save_state()
            return "logged_in"
        return "unknown"

    # ------------------------------------------------------------------ API inside the page
    async def api(self, method: str, url: str, body: Any = None) -> Any:
        page = await self.ensure()
        headers = {"accept": "application/json, text/plain, */*", "locale": "he", **self.auth}
        if body is not None:
            headers["content-type"] = "application/json;charset=UTF-8"
        result = await page.evaluate(
            """async ([method, url, headers, body]) => {
              const r = await fetch(url, { method, headers, body: body === null ? undefined : JSON.stringify(body), credentials: 'include' });
              const ct = r.headers.get('content-type') || '';
              const cf = r.headers.get('cf-mitigated');
              let data = null;
              if (ct.includes('json')) { try { data = await r.json(); } catch (e) {} }
              return { status: r.status, json: ct.includes('json'), cf: !!cf, data };
            }""",
            [method, url, headers, body],
        )
        if result["cf"] or not result["json"]:
            raise CloudflareBlocked(f"Non-JSON response from {urlparse(url).path} (status {result['status']}).")
        if result["status"] in (401, 403):
            self.login_state = "unknown"
            raise NotLoggedIn("Rami Levy session expired — run login again.")
        if result["status"] >= 400:
            raise RuntimeError(f"HTTP {result['status']} from {urlparse(url).path}: {json.dumps(result['data'], ensure_ascii=False)[:300]}")
        return result["data"]

    # ------------------------------------------------------------------ catalog + cart
    def _store(self) -> str:
        if not self.store_id:
            raise RuntimeError("Store id unknown yet. Open the site once (login does it) or set RAMI_LEVY_STORE.")
        return self.store_id

    async def search(self, query: str, limit: int) -> list[dict]:
        data = await self.api("POST", SEARCH_URL, {"q": query, "aggs": 1, "store": self._store()})
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError("Unexpected search response shape.")
        if str(data.get("q") or "").strip() != query.strip():
            raise RuntimeError("Search query was ignored by the API (wire format changed?).")
        out = []
        for p in data["data"][:limit]:
            price = p.get("price")
            out.append(
                {
                    "product_id": p.get("id"),
                    "name": p.get("name"),
                    "price": price.get("price") if isinstance(price, dict) else price,
                    "by_kilo": bool((p.get("prop") or {}).get("by_kilo")),
                    "brand": ((p.get("gs") or {}).get("BrandName")),
                    "has_sale": bool(p.get("sale")),
                }
            )
        return out

    def cart_mirror(self) -> dict[str, dict]:
        if CART_FILE.exists():
            return json.loads(CART_FILE.read_text(encoding="utf-8"))
        return {}

    def _write_mirror(self, cart: dict[str, dict]) -> None:
        CART_FILE.write_text(json.dumps(cart, ensure_ascii=False, indent=1), encoding="utf-8")

    async def sync_cart(self, cart: dict[str, dict]) -> dict:
        supply = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        items = {pid: f"{float(v['quantity']):.2f}" for pid, v in cart.items()}
        data = await self.api(
            "POST",
            CART_URL,
            {"store": self._store(), "isClub": 0, "supplyAt": supply.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "items": items, "meta": None},
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("Unexpected cart response shape.")
        accepted = {str(i.get("id")) for i in data["items"] if isinstance(i, dict)}
        rejected = [pid for pid in cart if pid not in accepted]
        delivery = next((i.get("price") for i in data["items"] if isinstance(i, dict) and i.get("is_delivery")), None)
        return {"accepted": accepted, "rejected": rejected, "products_total": data.get("price"), "delivery_fee": delivery, "raw_items": data["items"]}

    async def set_item(self, product_id: str, quantity: float, name: str | None, price: float | None) -> dict:
        cart = self.cart_mirror()
        before = json.loads(json.dumps(cart))
        if quantity <= 0:
            cart.pop(product_id, None)
        else:
            cart[product_id] = {"quantity": quantity, "name": name or cart.get(product_id, {}).get("name"), "price": price}
        res = await self.sync_cart(cart)
        for pid in res["rejected"]:
            cart.pop(pid, None)
        # Refresh names/prices from the server's view.
        for it in res["raw_items"]:
            pid = str(it.get("id"))
            if pid in cart:
                cart[pid]["name"] = it.get("name") or cart[pid].get("name")
                cart[pid]["price"] = it.get("price")
        self._write_mirror(cart)
        res["before_count"] = len(before)
        return {"cart": cart, **{k: v for k, v in res.items() if k != "raw_items"}}

    # ------------------------------------------------------------------ mapping helpers
    def captured_since(self, t0: float) -> list[Captured]:
        return [c for c in self.capture if c.ts >= t0]

    def endpoint_report(self) -> list[dict]:
        seen: dict[tuple[str, str], dict] = {}
        for f in sorted(CAPTURE_DIR.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                u = urlparse(r["url"])
                path = re.sub(r"/\d{3,}", "/{id}", u.path)
                key = (r["method"], f"{u.hostname}{path}")
                e = seen.setdefault(key, {"method": key[0], "endpoint": key[1], "count": 0, "statuses": set(), "req_keys": set(), "resp_keys": set()})
                e["count"] += 1
                e["statuses"].add(r["status"])
                if isinstance(r.get("req_body"), dict):
                    e["req_keys"].update(r["req_body"].keys())
                if isinstance(r.get("resp_body"), dict):
                    e["resp_keys"].update(list(r["resp_body"].keys())[:25])
        return [{**v, "statuses": sorted(v["statuses"]), "req_keys": sorted(v["req_keys"]), "resp_keys": sorted(v["resp_keys"])} for v in seen.values()]


# ---------------------------------------------------------------------- utils
def _safe_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return "<non-json body>"


def _redact(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ("<redacted>" if SENSITIVE_KEYS.search(k) else _redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x) for x in v[:50]]
    return v


def _truncate(v: Any, depth: int = 0) -> Any:
    v = _redact(v)
    if depth > 5:
        return "…"
    if isinstance(v, dict):
        return {k: _truncate(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [_truncate(x, depth + 1) for x in v[:5]] + ([f"…+{len(v) - 5}"] if len(v) > 5 else [])
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "…"
    return v
