"""Notes: the conversation written up as useful, private, searchable notes."""
import pytest

from llm_agent.notes import NoteError, parse


async def test_notes_update_instead_of_duplicating_and_search_by_words(env):
    n = env.orch.notes
    a = await n.add("u1", "Salon app plan", "Booking app with services and a calendar.")
    b = await n.add("u1", "Salon app plan", "Now with prices added.")
    assert a["id"] == b["id"] and b["updated"] and len(await n.list("u1")) == 1
    await n.add("u1", "Shipping rules", "Free delivery above 500 rupees.")
    assert (await n.search("u1", "delivery rupees"))[0]["title"] == "Shipping rules"
    assert await n.search("u1", "zebra") == [] and len(await n.search("u1", "")) == 2
    assert await n.search("u2", "salon") == []                                   # never another user's notes
    with pytest.raises(NoteError):
        await n.add("u1", "x", "short")


def test_the_model_reply_is_parsed_defensively():
    assert parse('Sure! {"notes":[{"title":"A","body":"Body text"},{"title":"","body":"x"},"junk"]} done') == [{"title": "A", "body": "Body text"}]
    assert parse("no json") == [] and parse('{"notes": 5}') == []


async def test_a_chat_can_be_written_up_as_notes_and_the_notes_are_private(env):
    c = await env.signup("ann")
    other = await env.signup("bob")
    await c.post("/api/sessions/s1/chat", json={"prompt": "Let's plan a salon booking app with a calendar"})
    r = await c.post("/api/notes/from-chat", json={"session_id": "s1"})
    assert r.status_code == 200 and r.json()["notes"][0]["title"] == "Salon app plan"
    notes = (await c.get("/api/notes")).json()["notes"]
    assert notes[0]["source"] == "chat" and "calendar" in notes[0]["body"]
    assert (await other.get("/api/notes")).json()["notes"] == []
    assert (await other.post("/api/notes/from-chat", json={"session_id": "s1"})).status_code == 404
    assert (await other.delete(f"/api/notes/{notes[0]['id']}")).status_code == 404
    assert (await c.delete(f"/api/notes/{notes[0]['id']}")).status_code == 200


async def test_unusable_model_reply_and_empty_chats_give_a_clear_error(env):
    c = await env.signup("ann")
    await c.post("/api/sessions/s1/chat", json={"prompt": "hello there"})
    env.fake.state.notes_reply = "I cannot do that"
    r = await c.post("/api/notes/from-chat", json={"session_id": "s1"})
    assert r.status_code == 400 and "usable notes" in str(r.json())


async def test_the_assistant_saves_a_note_and_sees_its_title_next_time(env):
    c = await env.signup("ann")
    await c.post("/api/sessions/s1/chat", json={"prompt": "keepnote please"})
    assert [n["title"] for n in (await c.get("/api/notes")).json()["notes"]] == ["Launch checklist"]
    await c.post("/api/sessions/s1/chat", json={"prompt": "hi again"})
    assert "#1: Launch checklist" in env.fake.state.calls[-1]["messages"][0]["content"]
    r = await c.post("/api/notes", json={"title": "My own", "body": "Written by hand."})
    assert r.status_code == 200 and (await c.get("/api/notes")).json()["notes"][0]["source"] == "user"
