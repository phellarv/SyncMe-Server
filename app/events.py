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
        # Maps workspace_id -> list of (client_id, queue, owner, instance_id)
        self.workspaces: Dict[str, List[tuple[str, SubscriberQueue, str | None, str | None]]] = {}

    def count_for(self, owner: str) -> int:
        return sum(1 for subs in self.workspaces.values() for _, _, o, *_ in subs if o == owner)

    def subscribe(
        self,
        workspace_id: str,
        client_id: str,
        owner: str | None = None,
        instance_id: str | None = None,
    ) -> SubscriberQueue:
        if owner is not None and self.count_for(owner) >= MAX_SUBSCRIBERS_PER_CLIENT:
            raise TooManySubscribers(owner)
        q = SubscriberQueue(QUEUE_MAXSIZE)
        self.workspaces.setdefault(workspace_id, []).append((client_id, q, owner, instance_id))
        return q

    def unsubscribe(self, workspace_id: str, client_id: str):
        if workspace_id in self.workspaces:
            self.workspaces[workspace_id] = [s for s in self.workspaces[workspace_id] if s[0] != client_id]
            if not self.workspaces[workspace_id]:
                del self.workspaces[workspace_id]

    async def broadcast(
        self,
        workspace_id: str,
        event: str,
        path: str,
        revision: int,
        origin_client_id: str | None = None,
        origin_instance_id: str | None = None,
    ):
        msg = {"event": event, "path": path, "revision": revision, "origin_client_id": origin_client_id}
        for sub in list(self.workspaces.get(workspace_id, [])):
            cid, queue, owner = sub[0], sub[1], sub[2]
            sub_instance_id = sub[3] if len(sub) > 3 else None
            if origin_client_id:
                if cid == origin_client_id:
                    continue
                if owner == origin_client_id:
                    if origin_instance_id and sub_instance_id:
                        if sub_instance_id == origin_instance_id:
                            continue
                    else:
                        continue
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                queue.dropped = True        # its stream ends; the client reconnects and reconciles


broadcaster = Broadcaster()
