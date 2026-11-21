"""Manage app adapts to the app: sections and fields come from the project's manage.json and the owner can change them."""
import json

import pytest

from llm_agent import content_schema as cs
from tests.test_appkit import b64
from tests.test_apps import make_zip

BOOKINGS = {"sections": [
    {"name": "services", "label": "Services", "item": "service", "fields": [
        {"key": "title", "label": "Service", "type": "text", "required": True}, {"key": "minutes", "label": "Minutes", "type": "number"},
        {"key": "price", "label": "Price", "type": "money"}]},
    {"name": "bookings", "label": "Bookings", "item": "booking", "fields": [
        {"key": "guest", "label": "Guest name", "type": "text", "required": True}, {"key": "day", "label": "Day", "type": "date"},
        {"key": "status", "label": "Status", "type": "choice", "options": ["new", "confirmed", "done"]}, {"key": "paid", "label": "Paid", "type": "yesno"}]}]}
PROJECT = {"app.py": "x = 1\n", "requirements.txt": "flask\n", "manage.json": json.dumps(BOOKINGS)}


def test_layout_validation():
    assert cs.clean(BOOKINGS)["sections"][1]["fields"][2]["options"] == ["new", "confirmed", "done"]
    bad = [None, {}, [], {"sections": [{"label": "A", "fields": []}]}, "not json",
           {"sections": [{"label": "A", "fields": [{"key": "Bad Key", "label": "x"}]}]},
           {"sections": [{"label": "A", "fields": [{"key": "id", "label": "x"}]}]},
           {"sections": [{"label": "A", "fields": [{"key": "a", "label": "x", "type": "wat"}]}]},
           {"sections": [{"label": "A", "fields": [{"key": "a", "label": "x", "type": "choice", "options": ["one"]}]}]},
           {"sections": [{"label": "A", "fields": [{"key": "a", "label": "x"}, {"key": "a", "label": "y"}]}]},
           {"sections": [{"name": "A", "label": "A", "fields": [{"label": "x"}]}, {"name": "A", "label": "B", "fields": [{"label": "x"}]}]}]
    for b in bad:
        with pytest.raises(cs.SchemaError):
            cs.clean(b)
    assert cs.clean({"sections": [{"label": "Blog Posts", "fields": [{"label": "Post title"}]}]})["sections"][0]["name"] == "blog-posts"   # names made from labels


def test_values_are_checked_per_type():
    s = cs.clean(BOOKINGS)["sections"][1]
    ok = cs.check_values(s, {"guest": " Ann  Lee ", "day": "2026-12-31", "status": "new", "paid": 1, "junk": "x"})
    assert ok == {"guest": "Ann Lee", "day": "2026-12-31", "status": "new", "paid": True}
    for bad in ({"guest": ""}, {"guest": "a", "day": "31/12/2026"}, {"guest": "a", "status": "maybe"}):
        with pytest.raises(cs.SchemaError):
            cs.check_values(s, bad)


async def test_a_booking_app_gets_its_own_manage_screens_and_the_owner_can_change_them(env):
    owner = await env.signup("ben")
    app = (await owner.post("/api/apps/create", json={"name": "Salon", "version": "1.0.0", "project": b64(make_zip(PROJECT))})).json()
    auth = {"X-App-Id": app["app_id"], "X-App-Password": app["password"]}
    ov = (await owner.post("/api/apps/manage/overview", headers=auth)).json()
    assert [s["label"] for s in ov["schema"]["sections"]] == ["Services", "Bookings"]          # not "Products"

    r = await owner.post("/api/apps/manage/items/list?collection=bookings", headers=auth)
    assert r.json()["section"]["label"] == "Bookings" and r.json()["items"] == []
    save = await owner.post("/api/apps/manage/items/save", headers=auth, json={"collection": "bookings", "values": {"guest": "Ann", "day": "2026-12-01", "status": "new"}})
    assert save.status_code == 200
    bad = await owner.post("/api/apps/manage/items/save", headers=auth, json={"collection": "bookings", "values": {"guest": "Ann", "status": "weird"}})
    assert bad.status_code == 400 and "one of" in bad.json()["error"]
    item = (await owner.post("/api/apps/manage/items/list?collection=bookings", headers=auth)).json()["items"][0]
    assert item["heading"] == "Ann" and "Status: new" in item["line"] and item["values"]["paid"] is False

    # customers see the fields the app defined, nothing about shops
    await owner.post("/api/apps/manage/publish", headers=auth)
    visitor = await env.new_client()
    import llm_agent.apps as apps
    snap_path = env.orch.apps._snap(app["app_id"])
    snap = json.loads(snap_path.read_text())
    snap["effective_at"] = 0
    snap_path.write_text(json.dumps(snap))
    cat = (await visitor.get(f"/api/catalog/{app['app_id']}/bookings")).json()
    assert cat["items"][0]["guest"] == "Ann" and "in_stock" not in cat["items"][0] and "price" not in cat["items"][0]

    # the owner adds a field and a new section; the old data stays
    layout = cs.clean(BOOKINGS)
    layout["sections"][1]["fields"].append({"key": "notes", "label": "Notes", "type": "longtext"})
    layout["sections"].append({"name": "staff", "label": "Staff", "fields": [{"key": "name", "label": "Name", "type": "text"}]})
    assert (await owner.post("/api/apps/manage/schema", headers=auth, json=layout)).status_code == 200
    again = (await owner.post("/api/apps/manage/items/list?collection=bookings", headers=auth)).json()
    assert again["items"][0]["values"]["guest"] == "Ann" and any(f["key"] == "notes" for f in again["section"]["fields"])
    assert (await owner.post("/api/apps/manage/schema", headers=auth, json={"sections": []})).status_code == 400

    # a new version of the project does not undo the owner's layout
    ver = await owner.post("/api/apps/manage/details", headers=auth, json={"version": "1.0.1", "name": "Salon", "project": b64(make_zip(PROJECT))})
    assert ver.status_code == 200
    ov = (await owner.post("/api/apps/manage/overview", headers=auth)).json()
    assert "Staff" in [s["label"] for s in ov["schema"]["sections"]]


async def test_apps_without_a_layout_still_work_like_before_and_a_bad_file_only_warns(env):
    owner = await env.signup("cyrus")
    bad = {"app.py": "x=1\n", "manage.json": "{oops"}
    r = (await owner.post("/api/apps/create", json={"name": "Plain", "version": "1.0.0", "project": b64(make_zip(bad))})).json()
    assert any("manage.json" in w for w in r["warnings"])
    auth = {"X-App-Id": r["app_id"], "X-App-Password": r["password"]}
    ov = (await owner.post("/api/apps/manage/overview", headers=auth)).json()
    assert ov["schema"]["sections"][0]["label"] == "Content"
    assert (await owner.post("/api/apps/manage/items/save", headers=auth, json={"title": "Thing", "price": "2.5"})).status_code == 200
