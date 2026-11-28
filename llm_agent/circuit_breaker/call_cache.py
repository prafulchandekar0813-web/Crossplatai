"""Remembers what identical tool calls returned during one task, so asking again costs nothing and changes nothing.

A repeated call is answered from here (it does not count toward the tool-call limit or the failure streak) with a note
telling the model to move on. Results stay valid until something changes the world they described: web lookups never
go stale within a task; file tools go stale as soon as any file is changed or a command runs.
"""
from typing import Any, Dict, Optional, Tuple

from llm_agent.circuit_breaker.loop_detector import ToolLoopDetector

WEB = frozenset({"web_search", "read_url", "scrape_page", "knowledgebase_search", "use_skill"})   # never stale within a task
FILES = frozenset({"read_file", "list_files", "write_file", "write_files", "patch_file", "patch_files",
                   "insert_text", "edit_lines", "move_file", "check_build"})
CHANGES = frozenset({"write_file", "write_files", "patch_file", "patch_files", "insert_text", "edit_lines", "move_file", "bash",
                     "stop_app", "delegate_task"})


class CallCache:
    def __init__(self) -> None:
        self._items: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._epoch = 0
        self.hits: Dict[str, int] = {}

    @staticmethod
    def cacheable(name: str) -> bool:
        return name in WEB or name in FILES

    def lookup(self, name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.cacheable(name):
            return None
        fp = ToolLoopDetector.fingerprint(name, args)
        got = self._items.get(fp)
        if not got or (name not in WEB and got[0] != self._epoch):
            return None
        self.hits[fp] = self.hits.get(fp, 0) + 1
        result = dict(got[1])
        result["repeated"] = True
        result["note"] = (f"You already made this exact call and this is the same result (seen {self.hits[fp] + 1} times). "
                          "Do not repeat it: use the result, or change your approach.")
        return result

    def store(self, name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Call after a real run. A change to files or the machine makes earlier file results stale."""
        ok = result.get("status") != "error"
        if ok and name in CHANGES:
            self._epoch += 1
        if self.cacheable(name):
            self._items[ToolLoopDetector.fingerprint(name, args)] = (self._epoch, result)
