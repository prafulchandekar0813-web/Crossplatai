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
        self.loop_detector = ToolLoopDetector(config.max_consecutive_duplicates)

    @property
    def tripped(self) -> bool:
        return self.state is CircuitState.TRIPPED

    def _trip(self, reason: str) -> Verdict:
        self.state = CircuitState.TRIPPED
        self.trip_reason = reason
        return Verdict(False, reason)

    def check_turn(self) -> Verdict:
        """Call before every LLM request."""
        if self.tripped:
            return Verdict(False, self.trip_reason or "tripped")
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            return self._trip(f"{self.consecutive_failures} tool calls in a row failed.")
        if self.turns >= self.config.max_total_turns:
            return self._trip(f"Reached the limit of {self.config.max_total_turns} reasoning rounds.")
        self.turns += 1
        return Verdict(True)

    def check_tool_call(self, tool_name: str, tool_args: Dict[str, Any]) -> Verdict:
        """Call before executing every tool call."""
        if self.tripped:
            return Verdict(False, self.trip_reason or "tripped")
        if self.tool_calls >= self.config.max_tool_calls:
            return self._trip(f"Reached the limit of {self.config.max_tool_calls} tool calls.")
        if self.config.enable_loop_fingerprinting:
            is_loop, count = self.loop_detector.inspect_call(tool_name, tool_args)
            if is_loop:
                return self._trip(f"Repeated the same `{tool_name}` call {count} times (loop detected).")
        self.tool_calls += 1
        return Verdict(True)

    def record_result(self, is_error: bool) -> None:
        self.consecutive_failures = self.consecutive_failures + 1 if is_error else 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.trip_reason,
            "turns": self.turns,
            "max_turns": self.config.max_total_turns,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.config.max_tool_calls,
            "consecutive_failures": self.consecutive_failures,
        }
