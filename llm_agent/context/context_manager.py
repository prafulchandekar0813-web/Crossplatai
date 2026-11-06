"""Context-window management: token estimates, history summarisation, in-task compaction, output truncation."""
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from llm_agent.config import ContextConfig

COMPACTED_STUB = ('{"status":"compacted","note":"Older tool output was removed to save context; '
                  'call the tool again if you still need it."}')
Summarizer = Callable[[str], Awaitable[str]]


class ContextManager:
    def __init__(self, config: ContextConfig):
        self.config = config

    # ---- measuring -------------------------------------------------------------------------------
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Cheap, deliberately pessimistic estimate (~3 chars/token) that needs no tokenizer download."""
        return (len(text) + 2) // 3

    def message_tokens(self, msg: Dict[str, Any]) -> int:
        n = 6 + self.estimate_tokens(msg.get("content") or "")
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            n += self.estimate_tokens(fn.get("name", "")) + self.estimate_tokens(fn.get("arguments", "") or "")
        return n

    def total_tokens(self, messages: List[Dict[str, Any]], extra: int = 0) -> int:
        return extra + sum(self.message_tokens(m) for m in messages)

    # ---- tool output -----------------------------------------------------------------------------
    def truncate_tool_output(self, content: str) -> str:
        limit = self.config.max_tool_output_length
        if len(content) <= limit:
            return content
        return content[:limit] + f"\n...[truncated: showing {limit} of {len(content)} characters]"

    # ---- chat history ----------------------------------------------------------------------------
    @staticmethod
    def merge_adjacent(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Join consecutive messages of the same role (happens after a failed turn); strict chat templates need this."""
        out: List[Dict[str, str]] = []
        for m in messages:
            if out and out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append({"role": m["role"], "content": m["content"]})
        while out and out[0]["role"] != "user":  # a conversation must start with the user
            out.pop(0)
        return out

    async def prepare_history(
        self,
        rows: List[Dict[str, Any]],
        summary: str,
        summarizer: Optional[Summarizer],
        reserved_tokens: int,
    ) -> Tuple[List[Dict[str, str]], str, Optional[Tuple[int, str]]]:
        """Decide what earlier conversation the model sees.

        rows: successful messages not yet covered by `summary` (oldest first; each has id/role/content).
        Returns (messages, summary_text, new_summary) where new_summary=(upto_id, text) if one was produced.
        Recent messages stay verbatim; once the history is too big, the older part is folded into the summary.
        """
        cfg = self.config
        window = cfg.sliding_window_messages
        budget = max(512, cfg.summary_trigger_tokens - reserved_tokens)
        new_summary: Optional[Tuple[int, str]] = None

        def size(rs):
            return sum(6 + self.estimate_tokens(r["content"]) for r in rs) + self.estimate_tokens(summary)

        older: List[Dict[str, Any]] = []
        recent = rows
        if len(rows) > window:
            older, recent = rows[:-window], rows[-window:]
        # Fold older messages into the summary when the whole history no longer fits.
        if older and size(rows) > budget:
            if summarizer is not None:
                transcript = "\n".join(f"{r['role'].upper()}: {r['content'][:2000]}" for r in older)
                try:
                    text = (await summarizer(
                        (f"Existing summary:\n{summary}\n\n" if summary else "") + f"New messages:\n{transcript}"
                    )).strip()
                except Exception:
                    text = ""
                if text:
                    summary = text[:4000]
                    new_summary = (older[-1]["id"], summary)
            # Whether or not summarising worked, older messages are no longer sent verbatim.
        elif older:
            recent = rows
        # Still too big (a few huge messages)? Drop the oldest recent ones, keep at least the last exchange.
        while len(recent) > 2 and size(recent) > budget:
            recent = recent[1:]
        return self.merge_adjacent(recent), summary, new_summary

    # ---- in-task compaction ----------------------------------------------------------------------
    def compact(self, messages: List[Dict[str, Any]], keep_recent_tools: int = 3) -> Tuple[int, bool]:
        """Shrink old tool results in place once the trigger is crossed.

        Returns (number of messages compacted, still_over_hard_limit).
        """
        cfg = self.config
        total = self.total_tokens(messages)
        compacted = 0
        if total > cfg.summary_trigger_tokens:
            idx = [i for i, m in enumerate(messages) if m.get("role") == "tool" and m.get("content") != COMPACTED_STUB]
            for i in idx[:-keep_recent_tools] if keep_recent_tools else idx:
                if total <= cfg.summary_trigger_tokens:
                    break
                old = messages[i].get("content") or ""
                if len(old) <= len(COMPACTED_STUB) + 20:
                    continue
                messages[i] = {**messages[i], "content": COMPACTED_STUB}
                total -= self.estimate_tokens(old) - self.estimate_tokens(COMPACTED_STUB)
                compacted += 1
        return compacted, total > cfg.max_context_tokens
