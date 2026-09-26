"""EventBus + HookRegistry — the two smallest modules in the dispatcher, and
until now the two with no tests at all.

Both exist to be *defensive*, which is exactly why they need pinning: the bus
drops events rather than blocking the dispatcher on a stalled SSE client, and
the registry isolates hook failures so one bad subscriber can't take down the
task that fired the event. Neither behaviour is visible in normal operation —
you only find out it regressed on the day it matters.
"""

import asyncio
import unittest

from dispatcher.events import EventBus
from dispatcher.hooks import HookRegistry


class TestEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_every_subscriber_gets_every_event(self):
        bus = EventBus()
        a, b = bus.subscribe(), bus.subscribe()
        bus.publish({"event": "done"})
        self.assertEqual(a.get_nowait(), {"event": "done"})
        self.assertEqual(b.get_nowait(), {"event": "done"})

    async def test_unsubscribed_queue_stops_receiving(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish({"event": "done"})
        self.assertTrue(q.empty())

    async def test_unsubscribing_twice_is_harmless(self):
        # /events unsubscribes in a `finally`, which can run after a cancel
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.unsubscribe(q)          # discard(), not remove() — must not raise

    async def test_a_full_subscriber_is_dropped_not_awaited(self):
        """A browser tab that stopped reading must not stall the dispatcher.
        publish() is called from the task settle path, so blocking here would
        block the run that fired it."""
        bus = EventBus(max_queue=2)
        slow, fast = bus.subscribe(), bus.subscribe()
        for i in range(5):
            bus.publish({"event": "step", "i": i})
        self.assertEqual(slow.qsize(), 2)               # capped, never blocked
        self.assertEqual(fast.qsize(), 2)

    async def test_publish_is_synchronous(self):
        # it is registered as a hook and called without await; if it ever
        # became a coroutine the SSE broadcaster would silently stop working
        bus = EventBus()
        q = bus.subscribe()
        self.assertIsNone(bus.publish({"event": "queued"}))
        self.assertFalse(asyncio.iscoroutinefunction(bus.publish))
        self.assertEqual(q.get_nowait()["event"], "queued")


class TestHookRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_async_hooks_both_run(self):
        seen = []
        reg = HookRegistry()
        reg.register(lambda ev: seen.append(("sync", ev["event"])))

        async def ahook(ev):
            seen.append(("async", ev["event"]))

        reg.register(ahook)
        await reg.fire({"event": "done"})
        self.assertEqual(seen, [("sync", "done"), ("async", "done")])

    async def test_a_failing_hook_does_not_stop_the_others(self):
        """The SSE broadcaster is the FIRST hook registered. If a later hook
        could abort the chain — or propagate — a subscriber bug would break
        task settlement itself."""
        seen = []
        reg = HookRegistry()

        def boom(ev):
            raise RuntimeError("subscriber exploded")

        async def aboom(ev):
            raise RuntimeError("async subscriber exploded")

        reg.register(boom)
        reg.register(aboom)
        reg.register(lambda ev: seen.append(ev["event"]))
        with self.assertLogs("dispatcher.hooks", level="ERROR"):
            await reg.fire({"event": "failed"})     # must not raise
        self.assertEqual(seen, ["failed"])

    async def test_firing_with_no_hooks_is_a_noop(self):
        await HookRegistry().fire({"event": "queued"})

    async def test_hooks_run_in_registration_order(self):
        order = []
        reg = HookRegistry()
        for n in range(3):
            reg.register(lambda ev, n=n: order.append(n))
        await reg.fire({"event": "x"})
        self.assertEqual(order, [0, 1, 2])


class TestLifecycleProtection(unittest.IsolatedAsyncioTestCase):
    """Finding 2.1, server half: the bus dropped the NEWEST event on a full
    queue, so 300 steps + done + notify left the queue holding 256 steps and
    neither terminal event. Steps stay droppable; lifecycle evicts steps."""

    def drain(self, q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def test_done_survives_a_flood_of_steps(self):
        bus = EventBus(max_queue=8)
        q = bus.subscribe()
        for i in range(30):
            bus.publish({"event": "step", "n": i})
        bus.publish({"event": "done", "task_id": "t"})
        bus.publish({"event": "notify", "task_id": "t"})
        kinds = [e["event"] for e in self.drain(q)]
        self.assertEqual(kinds[-2:], ["done", "notify"])
        self.assertGreater(bus.dropped_steps, 0)
        self.assertGreater(bus.evicted_steps, 0)
        self.assertEqual(bus.dropped_protected, 0)

    async def test_incoming_steps_still_drop_on_a_full_queue(self):
        bus = EventBus(max_queue=4)
        q = bus.subscribe()
        for i in range(10):
            bus.publish({"event": "step", "n": i})
        self.assertEqual(q.qsize(), 4)
        self.assertGreater(bus.dropped_steps, 0)

    async def test_lifecycle_order_survives_eviction(self):
        bus = EventBus(max_queue=4)
        q = bus.subscribe()
        bus.publish({"event": "queued", "task_id": "t"})
        for i in range(10):
            bus.publish({"event": "step", "n": i})
        bus.publish({"event": "done", "task_id": "t"})
        kinds = [e["event"] for e in self.drain(q)]
        self.assertEqual(kinds[0], "queued")
        self.assertEqual(kinds[-1], "done")


if __name__ == "__main__":
    unittest.main()
