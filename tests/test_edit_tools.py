"""Multi-edit tools (patch, patch_files, write_files, insert_text inserts) and a contract check over every registered tool."""
import pytest

from llm_agent.tools.file_manager import WorkspaceFiles, WorkspaceError
from llm_agent.tools.registry import build_registry, validate_args


def ws(tmp_path):
    return WorkspaceFiles(str(tmp_path))


def test_patch_many_edits_in_one_call_is_atomic(tmp_path):
    w = ws(tmp_path)
    w.write("a.py", "def f():\n    return 1\n\ndef g():\n    return 2\n")
    r = w.patch("a.py", [{"old": "return 1", "new": "return 10"}, {"old": "return 2", "new": "return 20"}])
    assert r["replacements"] == 2
    assert "return 10" in w.read("a.py")["content"] and "return 20" in w.read("a.py")["content"]
    with pytest.raises(WorkspaceError) as e:
        w.patch("a.py", [{"old": "return 10", "new": "X"}, {"old": "nothing like this", "new": "Y"}])
    assert "Edit 2" in str(e.value) and "return 10" in w.read("a.py")["content"]      # first edit rolled back with the second


def test_patch_tolerates_indentation_and_trailing_space_slips(tmp_path):
    w = ws(tmp_path)
    w.write("a.py", "class A:\n    def f(self):\n        x = 1   \n        return x\n")
    r = w.patch("a.py", [{"old": "def f(self):\nx = 1\nreturn x", "new": "def f(self):\n    return 5"}])
    assert r["replacements"] == 1 and r["notes"]
    assert w.read("a.py")["content"] == "class A:\n    def f(self):\n        return 5\n" or "return 5" in w.read("a.py")["content"]


def test_patch_miss_points_at_the_closest_line(tmp_path):
    w = ws(tmp_path)
    w.write("a.txt", "alpha line\nbeta line here\ngamma\n")
    with pytest.raises(WorkspaceError) as e:
        w.patch("a.txt", [{"old": "beta line herr", "new": "x"}])
    assert "line 2" in str(e.value)


def test_patch_ambiguous_needs_replace_all_and_crlf_files_work(tmp_path):
    w = ws(tmp_path)
    w.write("a.txt", "x = 1\r\nx = 1\r\n")
    with pytest.raises(WorkspaceError):
        w.patch("a.txt", [{"old": "x = 1", "new": "x = 2"}])
    assert w.patch("a.txt", [{"old": "x = 1", "new": "x = 2", "replace_all": True}])["replacements"] == 2
    w.write("b.txt", "one\r\ntwo\r\n")
    w.patch("b.txt", [{"old": "one\ntwo", "new": "1\n2"}])
    assert w.read("b.txt")["content"] == "1\r\n2\r\n"


def test_patch_many_files_all_or_nothing(tmp_path):
    w = ws(tmp_path)
    w.write("a.txt", "one\n")
    w.write("b.txt", "two\n")
    r = w.patch_many([{"path": "a.txt", "edits": [{"old": "one", "new": "1"}]}, {"path": "b.txt", "edits": [{"old": "two", "new": "2"}]}])
    assert r["replacements"] == 2 and w.read("b.txt")["content"] == "2\n"
    with pytest.raises(WorkspaceError):
        w.patch_many([{"path": "a.txt", "edits": [{"old": "1", "new": "ONE"}]}, {"path": "b.txt", "edits": [{"old": "zzz", "new": "q"}]}])
    assert w.read("a.txt")["content"] == "1\n"
    # two entries for the same file build on each other
    w.patch_many([{"path": "a.txt", "edits": [{"old": "1", "new": "A"}]}, {"path": "a.txt", "edits": [{"old": "A", "new": "B"}]}])
    assert w.read("a.txt")["content"] == "B\n"


def test_write_many_checks_everything_first(tmp_path):
    w = ws(tmp_path)
    r = w.write_many([{"path": "x/a.txt", "content": "a"}, {"path": "b.txt", "content": "b"}])
    assert len(r["files"]) == 2 and w.read("x/a.txt")["content"] == "a"
    for bad in ([{"path": "c.txt", "content": "c"}, {"path": "c.txt", "content": "d"}],
                [{"path": "c.txt", "content": "c"}, {"path": "../out.txt", "content": "d"}],
                [{"path": "c.txt", "content": "c"}, {"path": "d.txt"}]):
        with pytest.raises(WorkspaceError):
            w.write_many(bad)
    assert not (tmp_path / "c.txt").exists()
    with pytest.raises(WorkspaceError):
        w.write_many([{"path": f"f{i}.txt", "content": "x"} for i in range(21)])


def test_insert_many_positions_refer_to_the_original_file(tmp_path):
    w = ws(tmp_path)
    w.write("a.txt", "one\ntwo\nthree\n")
    r = w.insert_many("a.txt", [{"text": "top", "where": "start"}, {"text": "after-two", "where": "after_text", "anchor": "two"},
                                {"text": "x", "where": "end"}, {"text": "y", "where": "end"}])
    assert w.read("a.txt")["content"] == "top\none\ntwo\nafter-two\nthree\nx\ny\n"
    assert r["inserts"] == 4
    with pytest.raises(WorkspaceError) as e:
        w.insert_many("a.txt", [{"text": "q", "where": "after_line", "line": 99}])
    assert "Insert 1" in str(e.value)


def test_registry_batch_tools_are_wired():
    reg = build_registry()
    assert {"patch_files", "write_files", "patch_file", "insert_text", "edit_lines"} <= set(reg)
    a, err = reg["patch_files"].check_args({"files": '[{"path": "a", "edits": [{"old": "x", "new": "y"}]}]'})
    assert not err and isinstance(a["files"], list)
    a, err = reg["insert_text"].check_args({"path": "a", "inserts": [{"text": "t", "where": "end"}]})
    assert not err and validate_args(reg["insert_text"], a) is None


def test_every_tool_has_a_sound_definition():
    reg = build_registry()
    for name, spec in reg.items():
        assert spec.name == name and spec.description.strip()
        params = spec.parameters
        assert params["type"] == "object"
        props = params["properties"]
        assert set(params.get("required", [])) <= set(props), name
        for k, v in props.items():
            assert "type" in v, f"{name}.{k}"
        for junk in ({}, {"path": None, "files": 5, "edits": "x", "query": [1], "url": {}}):      # labels must never crash on odd arguments
            assert isinstance(spec.describe(junk), str)
        for res in ({}, {"status": "error", "error": "boom"}, {"status": "success"}):
            assert isinstance(spec.summarize(res), str)


async def test_junk_arguments_become_tool_errors_not_crashes(tmp_path):
    """Every file tool, given wrong-typed arguments, must raise WorkspaceError (reported to the model) rather than TypeError/KeyError."""
    w = ws(tmp_path)
    w.write("a.txt", "one\ntwo\n")
    junk = [None, 5, "x", [], {}, [1], [{}], [{"old": 1}], [{"start_line": "a"}], {"a": 1}]
    calls = {"patch": lambda j: w.patch("a.txt", j), "patch_many": w.patch_many, "write_many": w.write_many,
             "edit_lines": lambda j: w.edit_lines("a.txt", j), "insert_many": lambda j: w.insert_many("a.txt", j),
             "insert": lambda j: w.insert("a.txt", j, j, j, j)}
    for name, fn in calls.items():
        for j in junk:
            try:
                fn(j)
            except WorkspaceError:
                pass
            except Exception as e:                                   # pragma: no cover - the failure message names the culprit
                pytest.fail(f"{name}({j!r}) raised {type(e).__name__}: {e}")
    assert w.read("a.txt")["content"] == "one\ntwo\n"


# ---- every file tool, driven the way the agent drives it, with the slips real models make --------------------

def _ctx(tmp_path, env=None):
    from types import SimpleNamespace
    from llm_agent.config import SystemConfig
    from llm_agent.tools.registry import ToolContext
    events = []

    async def emit(kind, **kw):
        events.append((kind, kw))
    rc = SimpleNamespace(written_files=set(), plan=None, scrapes=0)
    ctx = ToolContext(user_id="u", cfg=SystemConfig(), files=ws(tmp_path), kb=None, search=None, fetch=None, memory=None, rc=rc,
                      agent_id="a", emit=emit)
    return ctx, events


async def run_tool(ctx, name, args):
    """What Agent._execute does: normalise, then call the handler; only tool errors are allowed to escape as errors."""
    spec = build_registry()[name]
    args, problem = spec.check_args(args)
    if problem:
        return {"status": "error", "error": problem}
    try:
        res = await spec.handler(ctx, args)
    except WorkspaceError as e:
        return {"status": "error", "error": str(e)}
    assert isinstance(res, dict), (name, args)
    return res


async def test_no_file_tool_crashes_on_sloppy_arguments(tmp_path):
    ctx, _ = _ctx(tmp_path)
    ctx.files.write("src/app.py", "print(1)\nprint(2)\n")
    sloppy = [None, "", "x", 5, 1.5, True, [], {}, [1, "a"], [{}], {"a": 1}, {"path": 5}, {"path": "src/app.py"}, {"path": "../etc/passwd"},
              {"path": "nope.py", "offset": "abc"}, {"path": "src/app.py", "offset": "abc"}, {"path": "src/app.py", "offset": -5},
              {"steps": "one, two", "files": None}, {"steps": None}, {"task_title": 5, "steps": [1, None]},
              {"files": "x"}, {"files": [{"path": "a"}]}, {"from": "a", "to": None}, {"from": 5, "to": 6}, {"url": 5}, {"command": 5}]
    for name in ("plan_task", "write_file", "write_files", "patch_file", "patch_files", "insert_text", "edit_lines", "move_file",
                 "read_file", "list_files", "check_build"):
        for args in sloppy:
            res = await run_tool(ctx, name, dict(args) if isinstance(args, dict) else args)
            assert res.get("status") in ("success", "error") or "path" in res or "files" in res or "entries" in res or "ok" in res, (name, args, res)
    assert ctx.files.read("src/app.py")["content"] == "print(1)\nprint(2)\n"


async def test_paths_the_model_gets_slightly_wrong_still_resolve(tmp_path):
    ctx, _ = _ctx(tmp_path)
    ctx.files.write("shop/app.py", "a = 1\nb = 2\n")
    for path in ("app.py", "./shop/app.py", "/shop/app.py", "workspace/shop/app.py", "shop\\app.py"):
        r = await run_tool(ctx, "read_file", {"path": path})
        assert r.get("content") == "a = 1\nb = 2\n", (path, r)
    r = await run_tool(ctx, "patch_file", {"path": "app.py", "edits": [{"old": "b = 2", "new": "b = 3"}]})
    assert r["status"] == "success" and r["path"] == "shop/app.py"                  # the answer says which file really changed
    r = await run_tool(ctx, "edit_lines", {"path": "app.py", "changes": [{"start_line": 1, "end_line": 1, "new_text": "a = 9"}]})
    assert r["path"] == "shop/app.py" and ctx.files.read("shop/app.py")["content"] == "a = 9\nb = 3\n"
    ctx.files.write("other/app.py", "x")
    r = await run_tool(ctx, "read_file", {"path": "app.py"})                        # two matches: no guessing, name them
    assert r["status"] == "error" and "other/app.py" in r["error"] and "shop/app.py" in r["error"]
    r = await run_tool(ctx, "read_file", {"path": "shop/ap.py"})
    assert "shop/app.py" in r["error"]                                              # a typo gets a suggestion


async def test_patch_miss_shows_what_the_file_really_says(tmp_path):
    ctx, _ = _ctx(tmp_path)
    ctx.files.write("a.py", "def total(items):\n    return sum(items)\n")
    r = await run_tool(ctx, "patch_file", {"path": "a.py", "edits": [{"old": "def total(item):", "new": "x"}]})
    assert r["status"] == "error" and "2| " in r["error"] or "1| def total(items):" in r["error"]


async def test_move_file_and_folder(tmp_path):
    ctx, ev = _ctx(tmp_path)
    ctx.files.write("app.py", "x")
    ctx.files.write("pkg/a.txt", "a")
    ctx.files.write("pkg/sub/b.txt", "b")
    assert (await run_tool(ctx, "move_file", {"from": "app.py", "to": "backend/"}))["to"] == "backend/app.py"
    assert (await run_tool(ctx, "move_file", {"from": "backend/app.py", "to": "."}))["to"] == "app.py"       # out of the folder
    assert (await run_tool(ctx, "move_file", {"from": "app.py", "to": "main.py"}))["to"] == "main.py"        # rename
    r = await run_tool(ctx, "move_file", {"from": "pkg", "to": "lib"})
    assert r["folder"] and r["moved"] == 2 and ctx.files.read("lib/sub/b.txt")["content"] == "b" and not (tmp_path / "pkg").exists()
    assert (await run_tool(ctx, "move_file", {"from": "lib", "to": "lib/inner"}))["status"] == "error"        # into itself
    ctx.files.write("keep.txt", "k")
    assert "already exists" in (await run_tool(ctx, "move_file", {"from": "main.py", "to": "keep.txt"}))["error"]
    assert (await run_tool(ctx, "move_file", {"from": "main.py", "to": "keep.txt", "overwrite": True}))["status"] == "success"
    assert (await run_tool(ctx, "move_file", {"from": "main.py", "to": "z.py"}))["status"] == "error"          # source is gone
    for bad in ("../x", "a/../../x"):
        assert (await run_tool(ctx, "move_file", {"from": "keep.txt", "to": bad}))["status"] == "error"
    assert any(k == "file_changed" and v["action"] == "deleted" for k, v in ev)
    assert (tmp_path / "keep.txt").read_text() == "x"   # a leading "/" means the workspace root, never the real root


async def test_plan_records_what_needs_a_current_lookup(tmp_path):
    ctx, ev = _ctx(tmp_path)
    r = await run_tool(ctx, "plan_task", {"task_title": "Upload form", "steps": ["a"], "look_up": "current Flask file upload API\nlatest Flutter version"})
    assert r["status"] == "success" and ctx.rc.plan["look_up"] == ["current Flask file upload API", "latest Flutter version"]
    assert (await run_tool(ctx, "plan_task", {"steps": ["a"]}))["status"] == "success" and ctx.rc.plan["look_up"] == []
