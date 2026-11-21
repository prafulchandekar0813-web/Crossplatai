"""Plans and their limits: Free, Pro and Master.

Free   chat, code and preview only; no building or managing apps; a daily token allowance, then a cool-down.
Pro    everything in Free without the token limit, and up to N distinct apps built or managed per calendar month.
Master everything, unlimited.

The administrator account always counts as Master. A subscription that has expired counts as Free.
"""
import time
from typing import Any, Callable, Dict, Optional

from llm_agent.config import SystemConfig
from llm_agent.db import Database

PLANS = ("free", "pro", "master")
PERIODS = {"monthly": 30 * 86400, "yearly": 365 * 86400}


class PlanError(Exception):
    """A request the user's plan does not allow (402), or a limit they have reached (429)."""

    def __init__(self, message: str, status: int = 402):
        super().__init__(message)
        self.message, self.status = message, status


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _month(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def _wait(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


class Plans:
    def __init__(self, db: Database, cfg: Callable[[], SystemConfig], clock: Callable[[], float] = time.time):
        self.db, self.cfg, self.clock = db, cfg, clock

    # ---- what plan a user is on ----------------------------------------------------------------
    async def current(self, user_id: str, is_admin: bool = False) -> Dict[str, Any]:
        if is_admin:
            return {"plan": "master", "period": "", "expires_at": 0, "source": "admin"}
        row = await self.db.fetchone("SELECT plan, period, expires_at, source FROM subscriptions WHERE user_id=?", (user_id,))
        if row and row["plan"] in PLANS and (not row["expires_at"] or row["expires_at"] > self.clock()):
            return {"plan": row["plan"], "period": row["period"], "expires_at": row["expires_at"], "source": row["source"]}
        return {"plan": "free", "period": "", "expires_at": 0, "source": ""}

    async def grant(self, user_id: str, plan: str, period: str = "", source: str = "admin") -> None:
        """Give a user a paid plan. A paid period starts now, or at the end of an unexpired one of the same plan."""
        if plan == "free":
            await self.db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            return
        if plan not in PLANS:
            raise PlanError("Unknown plan", 400)
        now = self.clock()
        expires = 0.0
        if period:
            if period not in PERIODS:
                raise PlanError("Unknown billing period", 400)
            have = await self.current(user_id)
            start = have["expires_at"] if have["plan"] == plan and have["expires_at"] > now else now
            expires = start + PERIODS[period]
        await self.db.execute(
            "INSERT INTO subscriptions (user_id, plan, period, started_at, expires_at, source) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, period=excluded.period, started_at=excluded.started_at, "
            "expires_at=excluded.expires_at, source=excluded.source", (user_id, plan, period, now, expires, source))

    def price(self, plan: str, period: str) -> int:
        """Price in whole currency units."""
        if plan not in ("pro", "master") or period not in PERIODS:
            raise PlanError("Choose Pro or Master, monthly or yearly.", 400)
        return int(getattr(self.cfg(), f"price_{plan}_{period}"))

    # ---- Free plan: daily tokens and cool-down ---------------------------------------------------
    async def check_chat(self, user_id: str, is_admin: bool = False) -> None:
        if (await self.current(user_id, is_admin))["plan"] != "free":
            return
        row = await self.db.fetchone("SELECT blocked_until FROM token_usage WHERE user_id=?", (user_id,))
        left = (row["blocked_until"] if row else 0) - self.clock()
        if left > 0:
            raise PlanError(f"You have used today's free tokens. Chat is available again in {_wait(left)}. "
                            "Pro and Master have no daily token limit.", 429)

    async def record_tokens(self, user_id: str, tokens: int, is_admin: bool = False) -> None:
        if tokens <= 0 or (await self.current(user_id, is_admin))["plan"] != "free":
            return
        cfg, now = self.cfg(), self.clock()

        def op(c):
            row = c.execute("SELECT day, tokens, blocked_until FROM token_usage WHERE user_id=?", (user_id,)).fetchone()
            used = row["tokens"] if row and row["day"] == _day(now) else 0
            blocked = row["blocked_until"] if row else 0
            used += tokens
            if used >= cfg.free_daily_tokens:  # the allowance is spent: start the cool-down, and the next allowance starts clean
                blocked, used = now + cfg.free_cooldown_hours * 3600, 0
            c.execute("INSERT INTO token_usage (user_id, day, tokens, blocked_until) VALUES (?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET day=excluded.day, tokens=excluded.tokens, blocked_until=excluded.blocked_until",
                      (user_id, _day(now), used, blocked))
        await self.db.transaction(op)

    # ---- apps: Free cannot build or manage; Pro has a monthly number of distinct apps ------------------
    async def check_app(self, user_id: str, app_id: Optional[str], is_admin: bool = False) -> None:
        """Raise if the user's plan does not allow working with this app now; otherwise count it for this month (Pro).

        With no app id (an app about to be built) it only checks that there is room, and counts nothing."""
        plan = (await self.current(user_id, is_admin))["plan"]
        if plan == "master":
            return
        if plan == "free":
            raise PlanError("Building and managing apps is part of the Pro and Master plans. Open Account > Subscription to upgrade.")
        month, limit = _month(self.clock()), self.cfg().pro_apps_per_month

        def op(c):
            if app_id and c.execute("SELECT 1 FROM app_quota WHERE user_id=? AND month=? AND app_id=?", (user_id, month, app_id)).fetchone():
                return True
            used = c.execute("SELECT COUNT(*) AS n FROM app_quota WHERE user_id=? AND month=?", (user_id, month)).fetchone()["n"]
            if used >= limit:
                return False
            if app_id is None:
                return True
            c.execute("INSERT INTO app_quota (user_id, month, app_id, ts) VALUES (?,?,?,?)", (user_id, month, app_id, self.clock()))
            return True
        if not await self.db.transaction(op):
            raise PlanError(f"The Pro plan covers {limit} apps per month and you have used them all. "
                            "They reset next month, or upgrade to Master for unlimited apps.", 429)

    async def require_paid(self, user_id: str, is_admin: bool, what: str) -> None:
        if (await self.current(user_id, is_admin))["plan"] == "free":
            raise PlanError(f"{what} is part of the Pro and Master plans. Open Account > Subscription to upgrade.")

    # ---- the summary the Account screen shows -----------------------------------------------------
    async def status(self, user_id: str, is_admin: bool = False) -> Dict[str, Any]:
        cfg, now = self.cfg(), self.clock()
        cur = await self.current(user_id, is_admin)
        out: Dict[str, Any] = {**cur, "currency": cfg.plan_currency,
                               "terms": {"free_daily_tokens": cfg.free_daily_tokens, "free_cooldown_hours": cfg.free_cooldown_hours,
                                         "pro_apps_per_month": cfg.pro_apps_per_month},
                               "prices": {p: {k: getattr(cfg, f"price_{p}_{k}") for k in PERIODS} for p in ("pro", "master")}}
        usage = await self.db.fetchone("SELECT day, tokens, blocked_until FROM token_usage WHERE user_id=?", (user_id,))
        used = usage["tokens"] if usage and usage["day"] == _day(now) else 0
        blocked = max(0.0, (usage["blocked_until"] if usage else 0) - now)
        apps = await self.db.fetchone("SELECT COUNT(*) AS n FROM app_quota WHERE user_id=? AND month=?", (user_id, _month(now)))
        out["limits"] = {
            "daily_tokens": cfg.free_daily_tokens if cur["plan"] == "free" else None,
            "tokens_used": used, "cooldown_seconds": int(blocked) if cur["plan"] == "free" else 0,
            "apps_per_month": 0 if cur["plan"] == "free" else (cfg.pro_apps_per_month if cur["plan"] == "pro" else None),
            "apps_used": apps["n"]}
        return out

    async def forget_user(self, user_id: str) -> None:
        for table in ("subscriptions", "token_usage", "app_quota", "email_otps"):
            await self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
