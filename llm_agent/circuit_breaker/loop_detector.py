import hashlib
import json
from collections import deque
from typing import Any, Deque, Dict, Tuple


class ToolLoopDetector:
    """Flags an agent that keeps issuing the same tool call.

    A call counts as a repeat when an identical one (same tool, same arguments) already appears in the last
    `window` calls, so it also catches A-B-A-B ping-pong loops, not only back-to-back repeats.
    """

    def __init__(self, max_duplicates: int = 3, window: int = 0):
        self.max_duplicates = max(2, max_duplicates)
        self.window = window or max(6, self.max_duplicates * 2)
        self._recent: Deque[str] = deque(maxlen=self.window)

    @staticmethod
    def fingerprint(tool_name: str, tool_args: Dict[str, Any]) -> str:
        try:
            args = json.dumps(tool_args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args = str(tool_args)
        return hashlib.sha256(f"{tool_name}:{args}".encode("utf-8")).hexdigest()

    def inspect_call(self, tool_name: str, tool_args: Dict[str, Any]) -> Tuple[bool, int]:
        fp = self.fingerprint(tool_name, tool_args)
        count = sum(1 for x in self._recent if x == fp) + 1
        self._recent.append(fp)
        return count >= self.max_duplicates, count

    def reset(self) -> None:
        self._recent.clear()
