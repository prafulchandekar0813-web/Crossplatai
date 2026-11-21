"""Screen designs: the format, the ready-made templates, saving, and showing a design as an app."""
import pytest

from llm_agent import design_schema as ds
from llm_agent.designs import Designs
from tests.test_appkit import make_kit


def page(*blocks, pid="home"):
    return {"id": pid, "title": "Home", "blocks": list(blocks)}


def test_values_are_checked_and_trimmed_not_trusted():
    d = ds.clean({"pages": [page(
        {"id": "a", "type": "heading", "text": "  Hi   there ", "size": "huge", "color": "red"},
        {"id": "b", "type": "image", "src": "javascript:alert(1)", "height": 99999},
        {"id": "c", "type": "image", "src": "https://example.com/a.png\" onerror=\"x"},
        {"id": "d", "type": "button", "label": "Go", "action": {"type": "link", "url": "javascript:alert(1)"}},
        {"id": "e", "type": "button", "label": "Go", "action": {"type": "page", "target": "nowhere"}},
        {"id": "f", "type": "button", "label": "Call", "action": {"type": "link", "url": "tel:+100"}},
        {"id": "g", "type": "mystery"}, "not a block")]}) ["pages"][0]["blocks"]
    assert [b["id"] for b in d] == ["a", "b", "c", "d", "e", "f"]                  # unknown things are dropped
    assert d[0]["text"] == "Hi there" and d[0]["size"] == "m" and d[0]["color"] == ""
    assert d[1]["src"] == "" and d[1]["height"] == 600 and d[2]["src"] == ""
    assert d[3]["action"] == {"type": "none"} and d[4]["action"] == {"type": "none"} and d[5]["action"]["url"] == "tel:+100"


def test_structure_limits():
    with pytest.raises(ds.DesignError):
        ds.clean({"pages": []})
    with pytest.raises(ds.DesignError):
        ds.clean({"pages": [page(), page()]})                                     # the same page id twice
    with pytest.raises(ds.DesignError):
        ds.clean({"pages": [page({"id": "a", "type": "spacer"}, {"id": "a", "type": "spacer"})]})
    deep = {"id": "x0", "type": "group", "children": []}
    node = deep
    for i in range(1, 6):
        child = {"id": f"x{i}", "type": "group", "children": []}
        node["children"].append(child)
        node = child
    with pytest.raises(ds.DesignError):
        ds.clean({"pages": [page(deep)]})                                          # boxes inside boxes too deep
    with pytest.raises(ds.DesignError):
        ds.clean({"pages": [page(*[{"id": f"s{i}", "type": "spacer"} for i in range(ds.MAX_BLOCKS + 1)])]})


def test_the_editor_gets_its_palette_from_the_same_table():
    s = ds.spec()
    assert set(s["blocks"]) == set(ds.BLOCKS) and s["blocks"]["group"]["container"] is True
    assert {f["key"] for f in s["blocks"]["button"]["fields"]} == {"label", "action", "style", "icon", "full", "align"}


def test_every_shipped_template_is_valid_and_stable():
    templates = Designs.templates()
    assert {t["key"] for t in templates} >= {"shop", "menu", "portfolio", "booking"}
    for t in templates:
        assert ds.clean(t["design"]) == t["design"], t["key"]                       # cleaning changes nothing: the file is already clean


async def test_free_users_see_templates_but_cannot_design(env):
    c = await env.signup("freya", plan="free")
    assert (await c.get("/api/designs/templates")).status_code == 200 and (await c.get("/api/designs/blocks")).status_code == 200
    r = await c.post("/api/designs", json={"template": "shop"})
    assert r.status_code == 402 and "Pro" in r.json()["error"]
    assert (await c.get("/api/designs")).status_code == 402


async def test_pick_a_template_edit_it_and_keep_it_private(env):
    c = await env.signup("paula", plan="pro")
    made = (await c.post("/api/designs", json={"template": "shop", "name": "My shop"})).json()
    assert made["name"] == "My shop" and len(made["design"]["pages"]) == 3
    blank = (await c.post("/api/designs", json={"name": "Blank"})).json()
    assert blank["design"]["pages"][0]["blocks"] == [] and blank["design"]["theme"]["accent"]
    edited = made["design"]
    edited["pages"][0]["blocks"][0] = {"id": "z1", "type": "heading", "text": "Changed"}
    saved = (await c.put(f"/api/designs/{made['id']}", json={"name": "Renamed", "design": edited})).json()
    assert saved["name"] == "Renamed" and saved["design"]["pages"][0]["blocks"][0]["text"] == "Changed"
    bad = await c.put(f"/api/designs/{made['id']}", json={"name": "x", "design": {"pages": []}})
    assert bad.status_code == 400
    assert {d["name"] for d in (await c.get("/api/designs")).json()["designs"]} == {"Renamed", "Blank"}
    other = await env.signup("oscar", plan="pro")
    assert (await other.get(f"/api/designs/{made['id']}")).status_code == 404            # your own designs are only yours
    assert (await other.put(f"/api/designs/{made['id']}", json={"name": "x", "design": edited})).status_code == 404
    assert (await other.delete(f"/api/designs/{made['id']}")).status_code == 404
    assert (await other.get("/api/designs")).json()["designs"] == []
    assert (await c.delete(f"/api/designs/{made['id']}")).status_code == 200


async def test_design_limit(env):
    c = await env.signup("paula", plan="master")
    env.orch.designs.limit = lambda: 2
    assert (await c.post("/api/designs", json={})).status_code == 200 and (await c.post("/api/designs", json={})).status_code == 200
    assert (await c.post("/api/designs", json={})).status_code == 409


async def test_a_design_becomes_the_app_screens(env):
    c = await env.signup("paula", plan="master")
    app = (await make_kit(c)).json()
    design = (await c.post("/api/designs", json={"template": "menu"})).json()
    anon = await env.new_client()
    assert (await anon.get(f"/api/ui/{app['app_id']}")).status_code == 404               # nothing applied yet
    assert not (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["settings"]["backend_url"]
    r = await c.post(f"/api/designs/{design['id']}/apply", json={"app_id": app["app_id"]})
    assert r.status_code == 200
    pub = (await anon.get(f"/api/ui/{app['app_id']}")).json()                              # anyone can see the screens, and only those
    assert pub["design"]["pages"][0]["id"] == "home" and set(pub) == {"design", "name", "logo"}
    assert (await anon.get(f"/ui/{app['app_id']}/")).status_code == 200
    addr = (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["settings"]["backend_url"]
    assert addr.endswith(f"/ui/{app['app_id']}/")                                          # the installed app opens it
    listing = (await c.get("/api/designs")).json()["designs"][0]
    assert listing["apps"] == [app["app_id"]]
    # somebody else's app cannot be used, and removing the design removes the screens
    stranger = await env.signup("sam", plan="master")
    assert (await stranger.post(f"/api/designs/{design['id']}/apply", json={"app_id": app["app_id"]})).status_code == 404
    other = (await stranger.post("/api/designs", json={})).json()
    assert (await stranger.post(f"/api/designs/{other['id']}/apply", json={"app_id": app["app_id"]})).status_code == 404
    await c.delete(f"/api/designs/{design['id']}")
    assert (await anon.get(f"/api/ui/{app['app_id']}")).status_code == 404


async def test_applying_a_design_counts_as_managing_the_app(env):
    c = await env.signup("paula", plan="pro")
    ids = [(await make_kit(c, name=f"Shop {i}")).json()["app_id"] for i in range(5)]
    design = (await c.post("/api/designs", json={})).json()
    assert (await c.post(f"/api/designs/{design['id']}/apply", json={"app_id": ids[0]})).status_code == 200   # already counted this month
    assert (await c.post("/api/designs/unapply", json={"app_id": ids[0]})).status_code == 200


# ---- icons, blocks, themes -------------------------------------------------------------------------------------------
def test_new_blocks_icons_and_theme_are_checked():
    d = ds.clean({"theme": {"accent": "#112233", "radius": "round", "surface": "nope"}, "pages": [page(
        {"id": "i", "type": "icon", "icon": "not-an-icon", "size": 5000},
        {"id": "j", "type": "items", "collection": "Bad Name!", "layout": "wide", "columns": 99, "limit": 0},
        {"id": "k", "type": "button", "label": "Buy", "icon": "cart"},
        {"id": "l", "type": "banner", "text": "Hi", "bg": "javascript:1"})]})
    i, j, k, banner = d["pages"][0]["blocks"]
    assert i["icon"] == "star" and i["size"] == 96
    assert j["collection"] == "products" and j["layout"] == "grid" and j["columns"] == 4 and j["limit"] == 1
    assert k["icon"] == "cart" and banner["bg"] == ""
    assert d["theme"]["radius"] == "round" and d["theme"]["accent"] == "#112233" and d["theme"]["surface"] == "#f1f4fa"


def test_every_preset_is_a_valid_theme():
    s = ds.spec()
    assert len(s["presets"]) >= 8
    for p in s["presets"]:
        assert ds.clean({"theme": p["theme"], "pages": [page()]})["theme"]["accent"] == p["theme"]["accent"], p["name"]


def test_the_editor_and_the_server_know_the_same_icons():
    import re
    from pathlib import Path
    js = (Path(ds.__file__).parent / "web_ui" / "js" / "design" / "icons.js").read_text()
    drawn = set(re.findall(r'^\s+"?([a-z-]+)"?:\s*"M', js, re.M))
    assert drawn == set(ds.ICONS)


def test_retargeting_items_to_a_section_the_app_has():
    d = ds.clean({"pages": [page({"id": "a", "type": "items", "collection": "products"},
                                 {"id": "g", "type": "group", "children": [{"id": "b", "type": "items", "collection": "menu"}]})]})
    assert ds.retarget_items(d, ["menu", "events"]) == 1
    assert d["pages"][0]["blocks"][0]["collection"] == "menu"


# ---- design in the app: products, Manage app, template choice --------------------------------------------------------
import base64
import json as _json

from tests.test_apps import make_zip

MENU_PROJECT = {"app.py": "import os\nprint(os.environ['PORT'])\n", "requirements.txt": "flask\n", "manage.json": _json.dumps({"sections": [
    {"name": "menu", "label": "Menu", "item": "dish", "fields": [{"key": "title", "label": "Dish", "type": "text", "required": True}, {"key": "price", "label": "Price", "type": "money"}],
     "items": [{"title": "Tea", "price": 3.5}, {"title": "Soup", "price": 6}, {"price": "bad"}, {"title": ""}]}]})}


async def test_starter_items_in_manage_json_become_editable_and_are_live(env):
    c = await env.signup("rita", plan="master")
    built = (await c.post("/api/apps/create", json={"name": "Cafe", "version": "1.0.0", "project": base64.b64encode(make_zip(MENU_PROJECT)).decode()})).json()
    assert built["imported"] == 2                                              # the two good rows; invalid ones are skipped
    auth = {"X-App-Id": built["app_id"], "X-App-Password": built["password"]}
    listed = (await c.post("/api/apps/manage/items/list?collection=menu", headers=auth)).json()
    assert [i["heading"] for i in listed["items"]] == ["Tea", "Soup"]          # visible in Manage app
    item = listed["items"][0]
    edited = await c.post("/api/apps/manage/items/save", headers=auth, json={"collection": "menu", "id": item["id"], "values": {"title": "Green tea", "price": 4}})
    assert edited.status_code == 200                                           # and editable
    anon = await env.new_client()
    live = (await anon.get(f"/api/catalog/{built['app_id']}/menu")).json()     # live at once, no Update needed for the first content
    assert [i["heading"] for i in live["items"]] == ["Tea", "Soup"] and live["items"][0]["price"] == 3.5
    assert live["update"]["pending"] is False


async def test_manage_app_picks_a_template_and_the_app_shows_it_with_its_items(env):
    c = await env.signup("rita", plan="master")
    built = (await c.post("/api/apps/create", json={"name": "Cafe", "version": "1.0.0", "project": base64.b64encode(make_zip(MENU_PROJECT)).decode()})).json()
    auth = {"X-App-Id": built["app_id"], "X-App-Password": built["password"]}
    st = (await c.post("/api/apps/manage/screens/state", headers=auth, json={})).json()
    assert st["current"] is None and {t["key"] for t in st["templates"]} >= {"shop", "menu"}
    st = (await c.post("/api/apps/manage/screens/use", headers=auth, json={"template": "shop"})).json()
    assert st["current"]["name"] and st["view_url"].endswith(f"/ui/{built['app_id']}/")
    anon = await env.new_client()
    pub = (await anon.get(f"/api/ui/{built['app_id']}")).json()["design"]
    items_blocks = [b for p in pub["pages"] for b in p["blocks"] if b["type"] == "items"]
    assert items_blocks and items_blocks[0]["collection"] == "menu"            # the shop template's "products" now shows this app's section
    settings = (await anon.get(f"/api/catalog/{built['app_id']}/menu")).json()["settings"]
    assert settings["backend_url"].endswith(f"/ui/{built['app_id']}/")
    opened = (await c.get(f"/api/designs/{st['current']['id']}")).json()
    assert [s["name"] for s in opened["sections"]] == ["menu"]                 # the editor can offer this app's sections
    # the owner edits the design from Designer; the app follows at once
    opened["design"]["pages"][0]["blocks"][0] = {"id": "z9", "type": "heading", "text": "Edited"}
    assert (await c.put(f"/api/designs/{opened['id']}", json={"name": opened["name"], "design": opened["design"]})).status_code == 200
    assert (await anon.get(f"/api/ui/{built['app_id']}")).json()["design"]["pages"][0]["blocks"][0]["text"] == "Edited"
    st = (await c.post("/api/apps/manage/screens/use", headers=auth, json={})).json()   # stop using it
    assert st["current"] is None and len(st["designs"]) == 1                   # the design itself is kept
    assert not (await anon.get(f"/api/catalog/{built['app_id']}/menu")).json()["settings"]["backend_url"]


async def test_a_chosen_design_wins_over_an_address_the_app_already_had(env):
    c = await env.signup("rita", plan="master")
    app = (await make_kit(c)).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    await c.post("/api/apps/manage/settings", headers=auth, json={"title": "", "accent": "", "announcement": "", "backend_url": "https://old.example.com"})
    await env.orch.apps.publish(app["app_id"], immediate=True)               # skip the one-minute cool-down
    anon = await env.new_client()
    assert (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["settings"]["backend_url"] == "https://old.example.com"
    await c.post("/api/apps/manage/screens/use", headers=auth, json={"template": "menu"})
    assert (await anon.get(f"/api/catalog/{app['app_id']}/products")).json()["settings"]["backend_url"].endswith(f"/ui/{app['app_id']}/")


async def test_free_accounts_cannot_use_screens_from_manage_app(env):
    owner = await env.signup("olga", plan="master")
    app = (await make_kit(owner)).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    free = await env.signup("fred", plan="free")
    assert (await free.post("/api/apps/manage/screens/state", headers=auth, json={})).status_code == 402


# ---- side by side, more widgets, details and forms -------------------------------------------------------------------
def test_rows_and_new_widgets_are_checked():
    d = ds.clean({"pages": [page(
        {"id": "r", "type": "group", "direction": "row", "sizing": "weird", "justify": "between", "wrap": "yes", "children": [{"id": "s", "type": "stat", "value": "9" * 50}]},
        {"id": "p", "type": "progress", "value": 500}, {"id": "t", "type": "rating", "value": -3},
        {"id": "f", "type": "form", "collection": "My Inbox!", "fields": "Name*\nPhone", "button": "Go"},
        {"id": "q", "type": "faq", "items": "A :: B"}, {"id": "i", "type": "items", "detail": 1, "order": True})]})
    r, p, t, f, q, i = d["pages"][0]["blocks"]
    assert r["sizing"] == "equal" and r["justify"] == "between" and r["wrap"] is False and len(r["children"][0]["value"]) == 20
    assert p["value"] == 100 and t["value"] == 0 and f["collection"] == "messages" and q["items"] == "A :: B"
    assert i["detail"] is False and i["order"] is True      # only a real true switches a switch on


def test_every_layout_is_valid_and_fits_the_depth_limit():
    def ids(b, n=[0]):
        n[0] += 1; b["id"] = f"n{n[0]}"
        for c in b.get("children", []):
            ids(c)
        return b
    import copy
    for layout in ds.spec()["layouts"]:
        out = ds.clean({"pages": [page(ids(copy.deepcopy(layout["block"])))]})
        assert out["pages"][0]["blocks"], layout["name"]


async def test_the_app_screens_take_orders_and_messages_into_the_inbox_and_show_item_fields(env):
    c = await env.signup("rita", plan="master")
    built = (await c.post("/api/apps/create", json={"name": "Cafe", "version": "1.0.0", "project": base64.b64encode(make_zip(MENU_PROJECT)).decode()})).json()
    auth = {"X-App-Id": built["app_id"], "X-App-Password": built["password"]}
    await c.post("/api/apps/manage/screens/use", headers=auth, json={"template": "menu"})
    anon = await env.new_client()
    live = (await anon.get(f"/api/catalog/{built['app_id']}/menu", headers={"x-device-id": "ab" * 12})).json()
    assert [f["key"] for f in live["fields"]] == ["title", "price"] and live["fields"][0]["label"] == "Dish"     # the detail sheet can label every value
    sent = await anon.post(f"/api/catalog/{built['app_id']}/submit/orders", headers={"x-device-id": "ab" * 12},
                           json={"Item": "Tea", "Price": 3.5, "Your name": "Ana", "Phone": "99999"})
    assert sent.status_code == 200
    inbox = (await c.post("/api/apps/manage/inbox/list", headers=auth)).json()["items"]
    assert inbox[0]["collection"] == "orders" and inbox[0]["data"]["Item"] == "Tea"
    pub = (await anon.get(f"/api/ui/{built['app_id']}")).json()["design"]
    kinds = {b["type"] for p in pub["pages"] for b in p["blocks"]}
    assert "items" in kinds and next(b for p in pub["pages"] for b in p["blocks"] if b["type"] == "items")["order"] is True
