import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llm_agent.api import create_app  # noqa: E402
from tests.fake_llm import make_fake_app  # noqa: E402

ADMIN_PASSWORD = "admin-pass-123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "LLM_", "SEARCH_")) or key in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "BRAVE_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


@pytest_asyncio.fixture
async def env(tmp_path):
    fake = make_fake_app()
    cfg = {"openai": {"base_url": "http://fake/v1", "model": "fake-model", "max_retries": 1}}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    app = create_app(config_path=str(cfg_path), base_dir=str(tmp_path), llm_transport=httpx.ASGITransport(app=fake))
    orch = app.state.orch
    await orch.startup()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def new_client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def signup(name, password="password-123", c=None):
        c = c or await new_client()
        r = await c.post("/api/auth/register", json={"username": name, "password": password})
        assert r.status_code == 200, r.text
        return c

    async def chat(c, sid, prompt):
        r = await c.post(f"/api/sessions/{sid}/chat", json={"prompt": prompt})
        assert r.status_code == 200, r.text
        return parse_sse(r.text)

    ns = SimpleNamespace(app=app, orch=orch, fake=fake, client=client, tmp=tmp_path, settings=app.state.settings,
                         new_client=new_client, signup=signup, chat=chat, cfg_path=cfg_path)
    yield ns
    await client.aclose()
    await orch.shutdown()
