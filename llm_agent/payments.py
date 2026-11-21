"""Paying for Pro / Master with Razorpay Payment Links.

The user pays on Razorpay's own page, so no payment script ever runs on this site. Afterwards the server asks Razorpay
(with the secret key) whether that link is paid; the browser's word is never trusted. Keys come from the environment:
RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.
"""
import os
import time
from typing import Any, Callable, Dict, Optional

import httpx

from llm_agent.config import SystemConfig
from llm_agent.db import Database
from llm_agent.plans import PlanError, Plans

API = "https://api.razorpay.com/v1"


class Razorpay:
    def __init__(self, db: Database, plans: Plans, cfg: Callable[[], SystemConfig],
                 client: Optional[httpx.AsyncClient] = None, clock: Callable[[], float] = time.time):
        self.db, self.plans, self.cfg, self.clock = db, plans, cfg, clock
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @property
    def _auth(self) -> tuple:
        return os.environ.get("RAZORPAY_KEY_ID", "").strip(), os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    @property
    def enabled(self) -> bool:
        return all(self._auth)

    async def _call(self, method: str, path: str, **kw) -> Dict[str, Any]:
        try:
            resp = await self.client.request(method, API + path, auth=self._auth, **kw)
        except httpx.HTTPError:
            raise PlanError("Could not reach the payment service. Try again in a moment.", 502)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            reason = ((data.get("error") or {}).get("description") if isinstance(data, dict) else "") or f"HTTP {resp.status_code}"
            raise PlanError(f"The payment service refused the request: {reason}", 502)
        return data if isinstance(data, dict) else {}

    async def start(self, user_id: str, plan: str, period: str, return_url: str = "") -> Dict[str, Any]:
        """Create a payment link for one plan and period; the user is sent to its `url`."""
        if not self.enabled:
            raise PlanError("Online payment is not set up on this server yet. The administrator needs to add the Razorpay keys.", 503)
        cfg = self.cfg()
        amount = self.plans.price(plan, period) * 100  # paise
        body: Dict[str, Any] = {
            "amount": amount, "currency": cfg.plan_currency,
            "description": f"{cfg.brand_name} {plan.title()} ({period})",
            "notes": {"user": user_id, "plan": plan, "period": period}}
        if return_url.startswith("https://"):  # Razorpay sends the user back here; without it they use "Check payment"
            body.update(callback_url=return_url, callback_method="get")
        link = await self._call("POST", "/payment_links/", json=body)
        link_id, url = str(link.get("id") or ""), str(link.get("short_url") or "")
        if not link_id or not url:
            raise PlanError("The payment service did not return a payment link.", 502)
        await self.db.execute("INSERT INTO payments (order_id, user_id, plan, period, amount, currency, created_at) VALUES (?,?,?,?,?,?,?)",
                              (link_id, user_id, plan, period, amount, cfg.plan_currency, self.clock()))
        return {"order_id": link_id, "url": url}

    async def confirm(self, user_id: str, link_id: str) -> Dict[str, Any]:
        """Ask Razorpay whether this user's link is paid and, once, give them the plan."""
        row = await self.db.fetchone("SELECT * FROM payments WHERE order_id=? AND user_id=?", (link_id, user_id))
        if not row:
            raise PlanError("That payment was not found.", 404)
        if row["status"] == "paid":
            return {"paid": True, "plan": row["plan"]}
        link = await self._call("GET", f"/payment_links/{link_id}")
        if link.get("status") != "paid" or int(link.get("amount") or 0) != row["amount"] or link.get("currency") != row["currency"]:
            return {"paid": False, "status": str(link.get("status") or "unknown")}
        payments = link.get("payments") if isinstance(link.get("payments"), list) else []
        pid = str((payments[0] or {}).get("payment_id") or "") if payments and isinstance(payments[0], dict) else ""
        if await self.db.execute("UPDATE payments SET status='paid', payment_id=? WHERE order_id=? AND status='created'", (pid, link_id)):
            await self.plans.grant(user_id, row["plan"], row["period"], source="razorpay")  # only the first confirmation grants
        return {"paid": True, "plan": row["plan"]}

    async def pending(self, user_id: str) -> list:
        rows = await self.db.fetchall("SELECT order_id FROM payments WHERE user_id=? AND status='created' ORDER BY created_at DESC LIMIT 5",
                                      (user_id,))
        return [r["order_id"] for r in rows]

