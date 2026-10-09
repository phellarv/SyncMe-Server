import asyncio

import pytest

from app import events


@pytest.fixture
def b():
    return events.Broadcaster()


def test_defaults():
    assert events.QUEUE_MAXSIZE == 100
    assert events.MAX_SUBSCRIBERS_PER_CLIENT == 10


def test_a_new_queue_is_bounded_by_the_default_size(b):
    assert b.subscribe("ws1", "c1").maxsize == 100


@pytest.mark.asyncio
async def test_full_queue_marks_subscriber_dropped_and_does_not_block(b, monkeypatch):
    monkeypatch.setattr(events, "QUEUE_MAXSIZE", 3)
    slow = b.subscribe("ws1", "slow")
    fast = b.subscribe("ws1", "fast")
    for i in range(5):
        await asyncio.wait_for(b.broadcast("ws1", "file_updated", f"f{i}", i), 1)
        fast.get_nowait()                                  # the fast one keeps up
    assert slow.dropped is True and slow.qsize() == 3
    assert fast.dropped is False


@pytest.mark.asyncio
async def test_a_queue_that_just_fits_is_not_dropped(b, monkeypatch):
    monkeypatch.setattr(events, "QUEUE_MAXSIZE", 3)
    q = b.subscribe("ws1", "c1")
    for i in range(3):
        await b.broadcast("ws1", "file_updated", f"f{i}", i)
    assert q.dropped is False and q.qsize() == 3


@pytest.mark.asyncio
async def test_one_slow_subscriber_does_not_stop_delivery_to_others(b, monkeypatch):
    monkeypatch.setattr(events, "QUEUE_MAXSIZE", 1)
    b.subscribe("ws1", "slow")
    fast = b.subscribe("ws1", "fast")
    await b.broadcast("ws1", "file_updated", "a", 1)
    await b.broadcast("ws1", "file_updated", "b", 2)
    assert fast.get_nowait()["path"] == "a"


def test_limit_per_owner_counts_across_workspaces(b, monkeypatch):
    monkeypatch.setattr(events, "MAX_SUBSCRIBERS_PER_CLIENT", 2)
    b.subscribe("ws1", "c1", owner="laptop")
    b.subscribe("ws2", "c2", owner="laptop")
    with pytest.raises(events.TooManySubscribers):
        b.subscribe("ws1", "c3", owner="laptop")
    b.subscribe("ws1", "c4", owner="phone")                # other owners are unaffected


def test_a_refused_subscription_is_not_registered(b, monkeypatch):
    monkeypatch.setattr(events, "MAX_SUBSCRIBERS_PER_CLIENT", 1)
    b.subscribe("ws1", "c1", owner="laptop")
    with pytest.raises(events.TooManySubscribers):
        b.subscribe("ws1", "c2", owner="laptop")
    assert b.count_for("laptop") == 1


def test_unsubscribe_frees_a_slot(b, monkeypatch):
    monkeypatch.setattr(events, "MAX_SUBSCRIBERS_PER_CLIENT", 1)
    b.subscribe("ws1", "c1", owner="laptop")
    b.unsubscribe("ws1", "c1")
    b.subscribe("ws1", "c2", owner="laptop")


def test_subscribe_without_owner_is_unlimited_and_returns_a_queue(b):
    qs = [b.subscribe("ws1", f"c{i}") for i in range(20)]
    assert all(isinstance(q, asyncio.Queue) for q in qs)
