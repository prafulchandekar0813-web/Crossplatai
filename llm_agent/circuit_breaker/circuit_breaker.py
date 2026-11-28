from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from llm_agent.circuit_breaker.loop_detector import ToolLoopDetector
from llm_agent.config import CircuitBreakerConfig


class CircuitState(Enum):
    CLOSED = "CLOSED"
    TRIPPED = "TRIPPED"


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""


class AgentCircuitBreaker:
    """Guards ONE agent during ONE task: LLM rounds, tool calls, repeated calls and consecutive failures.

    A fresh breaker is created for every task, so a trip never carries over to the next request.
    """

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.state = CircuitState.CLOSED
        self.trip_reason: Optional[str] = None
        self.turns = 0
        self.tool_calls = 0
        self.consecutive_failures = 0
        self.repeats = 0            # identical calls answered from the cache since the last fresh one
        self.limit_hit = False      # the trip was a round / tool-call allowance running out (not a loop or failures)
        self.checkpoints = 0        # how many times the allowance has been renewed
        self._carried_turns = self._carried_calls = 0
        self.loop_detector = ToolLoopDetector(config.max_consecutive_duplicates)

    @property
    def tripped(self) -> bool:
        return self.state is CircuitState.TRIPPED

    def _trip(self, reason: str, limit: bool = False) -> Verdict:
        self.state = CircuitState.TRIPPED
        self.trip_reason = reason
        self.limit_hit = limit
        return Verdict(False, reason)

    @property
    def all_turns(self) -> int:
        return self._carried_turns + self.turns

    @property
    def all_tool_calls(self) -> int:
        return self._carried_calls + self.tool_calls

    def can_renew(self) -> bool:
        return self.tripped and self.limit_hit and self.checkpoints < self.config.max_checkpoints

    def renew(self) -> None:
        """A checkpoint: the round and tool-call allowance starts again, as long as the agent was making progress."""
        self._carried_turns += self.turns
        self._carried_calls += self.tool_calls
        self.turns = self.tool_calls = self.repeats = self.consecutive_failures = 0
        self.state, self.trip_reason, self.limit_hit = CircuitState.CLOSED, None, False
        self.loop_detector = ToolLoopDetector(self.config.max_consecutive_duplicates)
        self.checkpoints += 1

    def check_turn(self) -> Verdict:
        """Call before every LLM request."""
        if self.tripped:
            return Verdict(False, self.trip_reason or "tripped")
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            return self._trip(f"{self.consecutive_failures} tool calls in a row failed.")
        if self.turns >= self.config.max_total_turns:
            return self._trip(f"Reached the limit of {self.config.max_total_turns} reasoning rounds.", limit=True)
        self.turns += 1
        return Verdict(True)

    def check_tool_call(self, tool_name: str, tool_args: Dict[str, Any]) -> Verdict:
        """Call before executing every tool call."""
        if self.tripped:
            return Verdict(False, self.trip_reason or "tripped")
        if self.tool_calls >= self.config.max_tool_calls:
            return self._trip(f"Reached the limit of {self.config.max_tool_calls} tool calls.", limit=True)
        if self.config.enable_loop_fingerprinting:
            is_loop, count = self.loop_detector.inspect_call(tool_name, tool_args)
            if is_loop:
                return self._trip(f"Repeated the same `{tool_name}` call {count} times (loop detected).")
        self.tool_calls += 1
        self.repeats = 0
        return Verdict(True)

    def note_repeat(self, tool_name: str) -> Verdict:
        """An identical call was answered from the cache. It is free, but asking over and over is a loop."""
        if self.tripped:
            return Verdict(False, self.trip_reason or "tripped")
        self.repeats += 1
        if self.repeats > self.config.max_consecutive_duplicates:
            return self._trip(f"Kept repeating the same `{tool_name}` call without making progress (loop detected).")
        return Verdict(True)

    def record_result(self, is_error: bool) -> None:
        self.consecutive_failures = self.consecutive_failures + 1 if is_error else 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.trip_reason,
            "turns": self.all_turns,
            "checkpoints": self.checkpoints,
            "max_turns": self.config.max_total_turns,
            "tool_calls": self.all_tool_calls,
            "max_tool_calls": self.config.max_tool_calls,
            "consecutive_failures": self.consecutive_failures,
        }
