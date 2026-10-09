import asyncio
from typing import Dict, List

QUEUE_MAXSIZE = 100
MAX_SUBSCRIBERS_PER_CLIENT = 10


class TooManySubscribers(Exception):
    pass


class SubscriberQueue(asyncio.Queue):
    """A bounded queue that remembers that it overflowed, so its stream can be ended."""

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize)
        self.dropped = False


class Broadcaster:
    def __init__(self):
        # Maps workspace_id -> list of (client_id, queue, owner)
        self.workspaces: Dict[str, List[tuple[str, SubscriberQueue, str | None]]] = {}

    def count_for(self, owner: str) -> int:
        return sum(1 for subs in self.workspaces.values() for _, _, o in subs if o == owner)

    def subscribe(self, workspace_id: str, client_id: str, owner: str | None = None) -> SubscriberQueue:
        if owner is not None and self.count_for(owner) >= MAX_SUBSCRIBERS_PER_CLIENT:
            raise TooManySubscribers(owner)
        q = SubscriberQueue(QUEUE_MAXSIZE)
        self.workspaces.setdefault(workspace_id, []).append((client_id, q, owner))
        return q

    def unsubscribe(self, workspace_id: str, client_id: str):
        if workspace_id in self.workspaces:
            self.workspaces[workspace_id] = [s for s in self.workspaces[workspace_id] if s[0] != client_id]
            if not self.workspaces[workspace_id]:
                del self.workspaces[workspace_id]

    async def broadcast(self, workspace_id: str, event: str, path: str, revision: int, origin_client_id: str | None = None):
        msg = {"event": event, "path": path, "revision": revision, "origin_client_id": origin_client_id}
        for cid, queue, owner in list(self.workspaces.get(workspace_id, [])):
            if origin_client_id and (owner == origin_client_id or cid == origin_client_id):
                continue
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                queue.dropped = True        # its stream ends; the client reconnects and reconciles


broadcaster = Broadcaster()
