"""A scripted OpenAI-compatible model for tests and demos (no real LLM needed).

Run it standalone to try the UI:   python -m tests.fake_llm     (serves http://127.0.0.1:9099/v1)
Then set the model URL to http://127.0.0.1:9099/v1 and model name to anything.

Prompts it understands (case-insensitive keywords):
  "build"     plan -> two parallel workers write files -> final answer
  "loop"      calls list_files forever (the circuit breaker must stop it)
  "remember"  calls remember() once
  "kb"        calls knowledgebase_search once
  "badjson"   first tool call has broken JSON, then recovers
  "slow"      takes 30 s to answer (for testing Stop)
  "fail"      HTTP 500
  anything else: plain text echo
"""
import asyncio
import json
import re
import uuid
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


TODO_PAGE = '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Todo</title>
<style>body{font-family:system-ui;margin:0;padding:24px;background:#eef2ff}h1{margin:0 0 12px}li{padding:6px 0}@media(min-width:700px){body{max-width:560px;margin:auto}}</style></head>
<body><h1>Todo</h1><input id=t placeholder="Add a task"><button id=a>Add</button><ul id=l></ul>
<script>var items=JSON.parse(localStorage.getItem('i')||'[]');function r(){l.innerHTML=items.map(function(x){return '<li>'+x+'</li>'}).join('')}
a.onclick=function(){items.push(t.value);localStorage.setItem('i',JSON.stringify(items));t.value='';r()};r()</script></body></html>
'''


def _call(name: str, args: Any) -> Dict[str, Any]:
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def _reply(content=None, tool_calls=None, usage=(120, 40)) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-fake", "choices": [{"index": 0, "message": msg,
            "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}}


def _is_notice(m: Dict[str, Any]) -> bool:
    return m["role"] == "user" and str(m.get("content", "")).startswith("[System notice]")


def make_fake_app() -> FastAPI:
    app = FastAPI()
    app.state.calls: List[Dict[str, Any]] = []
    app.state.reject_tools = False
    app.state.fail_next = 0

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        app.state.calls.append(body)
        msgs: List[Dict[str, Any]] = body["messages"]
        tools = body.get("tools")
        if app.state.reject_tools and tools:
            return JSONResponse({"error": {"message": "tools are not supported"}}, status_code=400)
        if app.state.fail_next > 0:
            app.state.fail_next -= 1
            return JSONResponse({"error": {"message": "overloaded"}}, status_code=503)

        system = msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else ""
        if msgs and _is_notice(msgs[-1]):
            return JSONResponse(_reply("I stopped early. Summary: part of the work was done; the rest is unfinished."))
        real = [i for i, m in enumerate(msgs) if m["role"] == "user" and not _is_notice(m)]
        last_i = real[-1] if real else 0
        latest = str(msgs[last_i].get("content", "")).split("\n\n")[-1] if msgs else ""  # merged turns: newest paragraph
        prompt = latest.lower()
        steps = [m for m in msgs[last_i + 1:] if m["role"] == "assistant"]
        step = len(steps)
        worker = re.search(r'worker agent \("([^"]+)"\)', system)

        if worker:
            role = worker.group(1).lower()
            if step == 0 and tools:
                if "backend" in role:
                    return JSONResponse(_reply(tool_calls=[_call("write_file", {
                        "path": "todo/backend/app.py", "content": "print('todo api')\n"})]))
                return JSONResponse(_reply(tool_calls=[_call("write_file", {
                    "path": "todo/frontend/index.html", "content": TODO_PAGE})]))
            return JSONResponse(_reply(f"{worker.group(1)} done: wrote my file."))

        if "slow" in prompt:
            await asyncio.sleep(30)
            return JSONResponse(_reply("finally"))
        if "fail" in prompt:
            return JSONResponse({"error": {"message": "internal boom"}}, status_code=500)
        if "build" in prompt and tools:
            if step == 0:
                return JSONResponse(_reply("I'll plan this first.", [_call("plan_task", {
                    "task_title": "Todo app", "steps": ["Backend", "Frontend", "Verify"],
                    "files": ["todo/backend/app.py", "todo/frontend/index.html"]})]))
            if step == 1:
                return JSONResponse(_reply(tool_calls=[
                    _call("delegate_task", {"task": "Write the backend", "role": "Backend developer"}),
                    _call("delegate_task", {"task": "Write the frontend", "role": "Frontend developer"})]))
            return JSONResponse(_reply("Built the todo app: `todo/backend/app.py` and `todo/frontend/index.html`."))
        if "loop" in prompt and tools:
            return JSONResponse(_reply(tool_calls=[_call("list_files", {})]))
        if "remember" in prompt and tools:
            if step == 0:
                return JSONResponse(_reply(tool_calls=[_call("remember", {"fact": "The user's name is Praful", "category": "profile"})]))
            return JSONResponse(_reply("Okay, I'll remember that."))
        if "kb" in prompt and tools:
            if step == 0:
                return JSONResponse(_reply(tool_calls=[_call("knowledgebase_search", {"query": "memory decay"})]))
            return JSONResponse(_reply("From the knowledge base: see the result above."))
        if "badjson" in prompt and tools:
            if step == 0:
                return JSONResponse(_reply(tool_calls=[_call("write_file", "{not valid json")]))
            return JSONResponse(_reply("Recovered after the bad tool call."))
        return JSONResponse(_reply("Echo: " + latest))

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(make_fake_app(), host="127.0.0.1", port=9099, log_level="warning")
