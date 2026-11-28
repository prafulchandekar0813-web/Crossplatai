"""Proof that a copied project really started in the sandbox, kept so the build gate can ask for it (readiness.py).

The server records it itself when the assistant's background run answered (never the assistant): a fingerprint of the project's files, per user.
Any later change gives a different fingerprint, so the project has to be run again after its last edit.
"""
import time

TTL = 7 * 86400


class RunProofs:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock

    async def record(self, user_id: str, digest: str) -> None:
        await self.db.execute("INSERT INTO run_proofs(user_id, digest, at) VALUES (?,?,?) ON CONFLICT(user_id, digest) DO UPDATE SET at=excluded.at", (user_id, digest, self.clock()))
        await self.db.execute("DELETE FROM run_proofs WHERE at<?", (self.clock() - TTL,))

    async def seen(self, user_id: str, digest: str) -> bool:
        return bool(await self.db.fetchone("SELECT 1 AS x FROM run_proofs WHERE user_id=? AND digest=? AND at>=?", (user_id, digest, self.clock() - TTL)))
