// Account: Profile (name, phone, e-mail with a verification code) and Subscription (Free / Pro / Master, paid through Razorpay).
import { api } from "./api.js";
import { h, toast, fmtDuration } from "./dom.js";
import { passwordDialog } from "./views.js";

const page = (title, lede, ...body) => h("div", { class: "page" }, h("h2", null, title), h("p", { class: "lede" }, lede), ...body);
const errorNote = (e) => h("div", { class: "form-error" }, e.message || String(e));
const num = (n) => Number(n).toLocaleString();

function busy(button, label, fn) {
  return async (...args) => {
    const was = button.textContent; button.disabled = true; button.textContent = label;
    try { await fn(...args); } finally { button.disabled = false; button.textContent = was; }
  };
}

// ================================================================================ Profile
export class ProfileView {
  constructor() { this.el = h("div"); }

  async load() {
    let data;
    try { data = await api("/api/account"); } catch (e) { this.el.replaceChildren(page("Profile", "", errorNote(e))); return; }
    if (!data.profile) { this.el.replaceChildren(page("Profile", "The administrator account has no personal profile. It has every feature.")); return; }
    this.paint(data.profile);
  }

  paint(p) {
    const name = h("input", { id: "pf-name", type: "text", maxlength: "60", value: p.name, autocomplete: "name" });
    const phone = h("input", { id: "pf-phone", type: "tel", maxlength: "24", value: p.phone, autocomplete: "tel", placeholder: "+91 98765 43210" });
    const email = h("input", { id: "pf-email", type: "email", maxlength: "254", value: p.email, autocomplete: "email", placeholder: "you@gmail.com" });
    const save = h("button", { class: "btn primary", type: "submit" }, "Save profile");
    const form = h("form", { onsubmit: (e) => e.preventDefault() },
      h("div", { class: "field" }, h("label", { for: "pf-name" }, "Name"), name),
      h("div", { class: "field" }, h("label", { for: "pf-phone" }, "Phone number"), phone, h("span", { class: "help" }, "Digits only, with a country code if you like.")),
      h("div", { class: "field" }, h("label", { for: "pf-email" }, "E-mail ", p.email ? h("span", { class: `tag ${p.email_verified ? "ok" : "warn"}` }, p.email_verified ? "Verified" : "Not verified") : null), email),
      save);
    save.onclick = busy(save, "Saving…", async () => {
      try { const r = await api("/api/account/profile", { method: "POST", body: { name: name.value, phone: phone.value, email: email.value } }); toast("Profile saved"); this.paint(r.profile); }
      catch (e) { toast(e.message, { kind: "bad" }); }
    });
    this.el.replaceChildren(page("Profile", `Signed in as ${p.user_id}.`, form, this.verifyBox(p),
      h("div", { style: { marginTop: "24px" } }, h("button", { class: "btn", type: "button", onclick: passwordDialog }, "Change password"))));
  }

  verifyBox(p) {
    if (!p.email || p.email_verified) return null;
    if (!p.email_ready) return h("p", { class: "muted" }, "Verifying e-mail is not set up on this server yet. The administrator needs to add the Gmail app password.");
    const code = h("input", { type: "text", inputmode: "numeric", maxlength: "6", placeholder: "6-digit code", autocomplete: "one-time-code", "aria-label": "Verification code", hidden: true });
    const send = h("button", { class: "btn", type: "button" }, "Send me a code");
    const check = h("button", { class: "btn primary", type: "button", hidden: true }, "Verify");
    send.onclick = busy(send, "Sending…", async () => {
      try { const r = await api("/api/account/email/send", { method: "POST" }); toast(`A code was sent to ${r.to}`); code.hidden = check.hidden = false; code.focus(); send.textContent = "Send again"; }
      catch (e) { toast(e.message, { kind: "bad" }); }
    });
    check.onclick = busy(check, "Checking…", async () => {
      try { const r = await api("/api/account/email/verify", { method: "POST", body: { code: code.value } }); toast("E-mail verified"); this.paint(r.profile); }
      catch (e) { toast(e.message, { kind: "bad" }); }
    });
    return h("div", { class: "verify-box" }, h("b", null, "Verify your e-mail"), h("p", { class: "muted" }, `We send a 6-digit code to ${p.email}. It works for 10 minutes.`),
      h("div", { class: "form-row" }, send, code, check));
  }
}

// ================================================================================ Subscription
export class SubscriptionView {
  constructor() { this.el = h("div"); this.period = "monthly"; }

  async load() {
    let data;
    try { data = await api("/api/account"); } catch (e) { this.el.replaceChildren(page("Subscription", "", errorNote(e))); return; }
    this.data = data; this.paint();
  }

  /** Called when Razorpay sends the user back to the site, or from "Check payment". */
  async confirm(orderId) {
    try {
      const r = await api("/api/account/subscribe/confirm", { method: "POST", body: { order_id: orderId } });
      toast(r.paid ? `Payment received. You are on ${r.plan.plan === "master" ? "Master" : "Pro"} now.` : "The payment has not been completed yet.", { kind: r.paid ? "info" : "bad" });
    } catch (e) { toast(e.message, { kind: "bad" }); }
    await this.load();
  }

  paint() {
    const { plan, payments_ready: ready } = this.data;
    const t = plan.terms, lim = plan.limits, cur = plan.plan;
    const money = (n) => new Intl.NumberFormat(undefined, { style: "currency", currency: plan.currency, maximumFractionDigits: 0 }).format(n);
    const names = { free: "Free", pro: "Pro", master: "Master" };

    const status = [h("span", { class: "tag accent" }, `${names[cur]} plan`)];
    if (plan.expires_at) status.push(h("span", { class: "muted" }, ` renews or ends on ${new Date(plan.expires_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })}`));
    const meters = [];
    if (lim.daily_tokens) meters.push(this.meter("Tokens used today", lim.tokens_used, lim.daily_tokens, num));
    if (lim.cooldown_seconds) meters.push(h("div", { class: "form-error" }, `Chat is paused. It is available again in ${fmtDuration(lim.cooldown_seconds * 1000)}.`));
    if (lim.apps_per_month) meters.push(this.meter("Apps used this month", lim.apps_used, lim.apps_per_month, num));

    const seg = h("div", { class: "seg", role: "group", "aria-label": "Billing period" }, ...["monthly", "yearly"].map((p) =>
      h("button", { class: "btn small", type: "button", "aria-pressed": String(this.period === p), onclick: () => { this.period = p; this.paint(); } }, p === "monthly" ? "Monthly" : "Yearly")));

    const tier = (id, blurb, lines) => {
      const price = id === "free" ? money(0) : money(plan.prices[id][this.period]);
      const btn = id === "free" ? h("button", { class: "btn", type: "button", disabled: true }, cur === "free" ? "Your plan" : "Included")
        : h("button", { class: `btn ${cur === id ? "" : "primary"}`, type: "button", disabled: !ready }, cur === id ? "Renew" : `Choose ${names[id]}`);
      if (id !== "free") btn.onclick = busy(btn, "Opening payment…", async () => {
        try { const r = await api("/api/account/subscribe", { method: "POST", body: { plan: id, period: this.period } }); location.assign(r.url); }
        catch (e) { toast(e.message, { kind: "bad" }); }
      });
      return h("div", { class: "tier", "aria-current": String(cur === id) }, h("h3", null, names[id]),
        h("div", { class: "tier-price" }, price, id === "free" ? h("small", null, " forever") : h("small", null, this.period === "monthly" ? " / month" : " / year")),
        h("p", { class: "muted" }, blurb), h("ul", null, ...lines.map((l) => h("li", null, l))), btn);
    };
    const tiers = h("div", { class: "tiers" },
      tier("free", "To try the assistant.", ["Chat, write code and see the preview", "Cannot build or manage apps", `${num(t.free_daily_tokens)} tokens a day, then a ${t.free_cooldown_hours} hour cool-down`]),
      tier("pro", "For people building their own apps.", ["Everything in Free, with no token limit", `Build and manage ${t.pro_apps_per_month} apps a month`]),
      tier("master", "For heavy use.", ["Everything in Pro", "Unlimited apps and unlimited use"]));

    const check = h("button", { class: "btn", type: "button" }, "I have paid: check my payment");
    check.onclick = busy(check, "Checking…", async () => {
      const { orders } = await api("/api/account/subscribe/pending");
      if (!orders.length) { toast("No payment is waiting to be checked."); return; }
      await this.confirm(orders[0]);
    });
    this.el.replaceChildren(page("Subscription", "Pick the plan that fits. You pay on Razorpay's secure page, never here.",
      h("div", { style: { marginBottom: "14px" } }, ...status), ...meters,
      cur === "master" && !plan.expires_at ? null : seg, tiers,
      !ready && cur !== "master" ? h("p", { class: "muted", style: { marginTop: "14px" } }, "Online payment is not set up on this server yet. Ask the administrator to upgrade your plan.") : null,
      cur === "master" && !plan.expires_at ? null : h("div", { style: { marginTop: "18px" } }, check)));
  }

  meter(label, used, total, fmt) {
    const pct = Math.min(100, Math.round((used / total) * 100));
    return h("div", { class: "meter" }, h("div", { class: "meter-top" }, h("span", null, label), h("span", null, `${fmt(used)} of ${fmt(total)}`)),
      h("div", { class: "meter-bar" }, h("i", { class: pct >= 85 ? "hot" : "", style: { width: `${pct}%` } })));
  }
}
