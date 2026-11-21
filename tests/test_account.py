"""Plans (Free / Pro / Master), the profile with e-mail verification, and Razorpay payment."""
from pathlib import Path

import httpx
import pytest

from llm_agent.plans import PlanError
from tests.test_appkit import make_kit


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(env):
    c = Clock()
    env.orch.plans.clock = c
    return c


async def test_free_users_chat_until_the_daily_tokens_are_used_then_wait(env, clock):
    c = await env.signup("freya", plan="free")
    env.settings.update({"free_daily_tokens": 1000, "free_cooldown_hours": 8})
    await env.chat(c, "s1", "hello")                       # a normal chat works
    await env.orch.plans.record_tokens("freya", 5000)      # the allowance is spent
    r = await c.post("/api/sessions/s1/chat", json={"prompt": "again"})
    assert r.status_code == 429 and "8 h" in r.json()["error"]
    status = (await c.get("/api/account")).json()["plan"]
    assert status["plan"] == "free" and status["limits"]["cooldown_seconds"] > 7 * 3600
    clock.t += 8 * 3600 + 5                                # the cool-down is over: a fresh allowance
    await env.chat(c, "s1", "back again")
    assert (await c.get("/api/account")).json()["plan"]["limits"]["tokens_used"] < 1000


async def test_tokens_reset_each_day_and_paid_plans_have_no_limit(env, clock):
    await env.signup("dana", plan="free")
    env.settings.update({"free_daily_tokens": 1000})
    await env.orch.plans.record_tokens("dana", 600)
    clock.t += 86400
    await env.orch.plans.record_tokens("dana", 600)        # a new day, so 600 not 1200
    await env.orch.plans.check_chat("dana")
    await env.orch.plans.grant("dana", "pro", "monthly")
    await env.orch.plans.record_tokens("dana", 10_000_000)
    await env.orch.plans.check_chat("dana")


async def test_free_cannot_build_or_manage_apps(env):
    c = await env.signup("freya", plan="free")
    r = await make_kit(c)
    assert r.status_code == 402 and "Pro" in r.json()["error"]
    r = await c.post("/api/apps/build?name=Site", content=b"x")
    assert r.status_code == 402
    master = await env.signup("owner")
    app = (await make_kit(master)).json()
    headers = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    assert (await c.post("/api/apps/manage/open", headers=headers)).status_code == 402   # signed in, right password, still Free


async def test_pro_covers_five_distinct_apps_a_month(env, clock):
    c = await env.signup("paula", plan="pro")
    ids = []
    for i in range(5):
        r = await make_kit(c, name=f"Shop {i}")
        assert r.status_code == 200, r.text
        ids.append(r.json())
    r = await make_kit(c, name="Sixth")
    assert r.status_code == 429 and "5 apps" in r.json()["error"]
    first = {"X-App-Id": ids[0]["app_id"], "X-App-Password": ids[0]["password"]}
    assert (await c.post("/api/apps/manage/open", headers=first)).status_code == 200      # an app already counted is free to use again
    lim = (await c.get("/api/account")).json()["plan"]["limits"]
    assert lim["apps_per_month"] == 5 and lim["apps_used"] == 5
    clock.t += 32 * 86400                                                                  # next month
    assert (await make_kit(c, name="Sixth")).status_code == 200


async def test_master_is_unlimited_and_expired_plans_fall_back_to_free(env, clock):
    c = await env.signup("max", plan="master")
    for i in range(7):
        assert (await make_kit(c, name=f"App {i}")).status_code == 200
    await env.signup("temp", plan="free")
    await env.orch.plans.grant("temp", "pro", "monthly")
    assert (await env.orch.plans.current("temp"))["plan"] == "pro"
    clock.t += 31 * 86400
    assert (await env.orch.plans.current("temp"))["plan"] == "free"
    with pytest.raises(PlanError):
        await env.orch.plans.check_app("temp", None)


async def test_admin_sets_plans_and_sees_them(env):
    await env.signup("kim", plan="free")
    admin = await env.new_client()
    await admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    r = await admin.post("/api/admin/users/kim/plan", json={"plan": "pro", "period": "yearly"})
    assert r.status_code == 200 and r.json()["plan"] == "pro"
    users = (await admin.get("/api/admin/users")).json()["users"]
    assert [u["plan"] for u in users if u["user_id"] == "kim"] == ["pro"]
    assert (await admin.post("/api/admin/users/nobody/plan", json={"plan": "pro"})).status_code == 404
    assert (await admin.post("/api/admin/users/kim/plan", json={"plan": "gold"})).status_code == 400


# ---- profile and e-mail verification ---------------------------------------------------------------
class FakeMailer:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body):
        self.sent.append((to, body))


async def test_profile_validation_and_saving(env):
    c = await env.signup("pat", plan="free")
    r = await c.post("/api/account/profile", json={"name": "  Pat   Lee ", "phone": "+91 98765 43210", "email": "Pat@Example.com"})
    prof = r.json()["profile"]
    assert prof["name"] == "Pat Lee" and prof["email"] == "pat@example.com" and prof["email_verified"] is False
    for bad in ({"phone": "12ab"}, {"email": "nope"}, {"name": "x" * 61}):
        assert (await c.post("/api/account/profile", json=bad)).status_code in (400, 422)
    assert (await c.get("/api/account")).json()["profile"]["phone"] == "+91 98765 43210"


async def test_email_code_flow(env, clock):
    mail = FakeMailer()
    env.orch.profiles.mailer, env.orch.profiles.clock = mail, clock
    c = await env.signup("pat", plan="free")
    assert (await c.post("/api/account/email/send")).status_code == 400            # no e-mail saved yet
    await c.post("/api/account/profile", json={"email": "pat@example.com"})
    assert (await c.post("/api/account/email/send")).status_code == 200
    code = mail.sent[0][1].split("code is ")[1][:6]
    assert (await c.post("/api/account/email/send")).status_code == 429             # too soon for another
    assert (await c.post("/api/account/email/verify", json={"code": "000000" if code != "000000" else "111111"})).status_code == 400
    r = await c.post("/api/account/email/verify", json={"code": code})
    assert r.status_code == 200 and r.json()["profile"]["email_verified"] is True
    r = await c.post("/api/account/profile", json={"email": "other@example.com"})   # a new address must be verified again
    assert r.json()["profile"]["email_verified"] is False


async def test_codes_expire_and_wrong_guesses_are_limited(env, clock):
    mail = FakeMailer()
    env.orch.profiles.mailer, env.orch.profiles.clock = mail, clock
    c = await env.signup("pat", plan="free")
    await c.post("/api/account/profile", json={"email": "pat@example.com"})
    await c.post("/api/account/email/send")
    code = mail.sent[0][1].split("code is ")[1][:6]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await c.post("/api/account/email/verify", json={"code": wrong})
    r = await c.post("/api/account/email/verify", json={"code": code})
    assert r.status_code == 429                                                       # locked out even with the right code
    clock.t += 700
    assert (await c.post("/api/account/email/verify", json={"code": code})).status_code == 400   # and it has expired


# ---- Razorpay --------------------------------------------------------------------------------------
class FakeRazorpay:
    def __init__(self):
        self.status, self.amount, self.created = "created", None, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            self.amount = body["amount"]
            return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})
        return httpx.Response(200, json={"id": "plink_1", "status": self.status, "amount": self.amount, "currency": "INR",
                                         "payments": [{"payment_id": "pay_9"}] if self.status == "paid" else None})


@pytest.fixture
def rzp(env, monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    fake = FakeRazorpay()
    env.orch.razorpay.client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return fake


async def test_paying_for_a_plan(env, rzp, clock):
    c = await env.signup("rita", plan="free")
    r = await c.post("/api/account/subscribe", json={"plan": "pro", "period": "yearly"})
    assert r.status_code == 200 and r.json()["url"] == "https://rzp.io/i/abc"
    assert rzp.created[0]["amount"] == 4999 * 100 and rzp.created[0]["currency"] == "INR"
    assert "callback_url" not in rzp.created[0]                                       # the test server is not https
    r = await c.post("/api/account/subscribe/confirm", json={"order_id": "plink_1"})
    assert r.json()["paid"] is False and (await env.orch.plans.current("rita"))["plan"] == "free"
    rzp.status = "paid"
    r = await c.post("/api/account/subscribe/confirm", json={"order_id": "plink_1"})
    assert r.json()["paid"] is True and r.json()["plan"]["plan"] == "pro"
    expires = (await env.orch.plans.current("rita"))["expires_at"]
    assert expires == clock.t + 365 * 86400
    await c.post("/api/account/subscribe/confirm", json={"order_id": "plink_1"})      # confirming again never adds time twice
    assert (await env.orch.plans.current("rita"))["expires_at"] == expires


async def test_payment_checks(env, rzp):
    c = await env.signup("rita", plan="free")
    other = await env.signup("sam", plan="free")
    await c.post("/api/account/subscribe", json={"plan": "master", "period": "monthly"})
    rzp.status = "paid"
    assert (await other.post("/api/account/subscribe/confirm", json={"order_id": "plink_1"})).status_code == 404   # not their payment
    rzp.amount = 100                                                                  # Razorpay says a different amount was paid
    r = await c.post("/api/account/subscribe/confirm", json={"order_id": "plink_1"})
    assert r.json()["paid"] is False
    assert (await c.post("/api/account/subscribe", json={"plan": "gold", "period": "monthly"})).status_code == 400
    assert (await c.post("/api/account/subscribe", json={"plan": "free", "period": "monthly"})).status_code == 400


async def test_payment_needs_keys(env):
    c = await env.signup("rita", plan="free")
    r = await c.post("/api/account/subscribe", json={"plan": "pro", "period": "monthly"})
    assert r.status_code == 503 and "Razorpay" in r.json()["error"]
    assert (await c.get("/api/account")).json()["payments_ready"] is False


async def test_installed_apps_get_the_system_name_for_their_footer(env):
    c = await env.signup("owner")
    app = (await make_kit(c)).json()
    anon = await env.new_client()
    assert (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["brand"] == "Agent Workbench"
    env.settings.update({"brand_name": "Crossplat AI"})
    assert (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["brand"] == "Crossplat AI"
    splash = Path(__file__).parent.parent / "llm_agent" / "shell_template" / "shell" / "lib" / "splash.dart"
    assert "Powered by ${widget.brand}" in splash.read_text()           # every waiting screen of a built app carries the footer
