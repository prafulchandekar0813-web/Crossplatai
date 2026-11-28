"""The user's "run anyway" for a repository that the safety scan only warned about.

The assistant cannot approve for the user: an approval is recorded by the server when the user's OWN chat message says so,
and only for a repository that is waiting for it. Held in memory, so a server restart simply asks again.
"""
import re
import time
from typing import Dict, Tuple

TTL = 3600
PHRASE = re.compile(r"^\W*(yes\W+)?(please\W+)?(run|use|continue|proceed|go ahead)(\s+with)?(\s+it)?\s+anyway\W*$", re.I)


class RepoApprovals:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.pending: Dict[Tuple[str, str], float] = {}
        self.granted: Dict[Tuple[str, str], float] = {}

    def _prune(self) -> None:
        now = self.clock()
        for table in (self.pending, self.granted):
            for k in [k for k, t in table.items() if t < now]:
                del table[k]

    def ask(self, user_id: str, repo: str) -> None:
        self._prune()
        self.pending[(user_id, repo.lower())] = self.clock() + TTL

    def heard(self, user_id: str, message: str) -> int:
        """Call with every real user message. Grants the user's waiting repositories when the message is the approval phrase."""
        if not PHRASE.match((message or "").strip()[:80]):
            return 0
        self._prune()
        mine = [k for k in self.pending if k[0] == user_id]
        for k in mine:
            del self.pending[k]
            self.granted[k] = self.clock() + TTL
        return len(mine)

    def approved(self, user_id: str, repo: str) -> bool:
        self._prune()
        return (user_id, repo.lower()) in self.granted

    def used(self, user_id: str, repo: str) -> None:
        self.granted.pop((user_id, repo.lower()), None)
