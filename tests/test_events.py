import pytest

from researchswarm import Event, EventBus


@pytest.mark.asyncio
async def test_sync_async_and_wildcard_listeners():
    bus = EventBus()
    seen = []
    bus.subscribe("task.completed", lambda event: seen.append(("sync", event.id)))

    async def async_listener(event):
        seen.append(("async", event.id))

    unsubscribe = bus.subscribe("*", async_listener)
    event = Event("run_x", "task.completed")
    await bus.publish(event)
    unsubscribe()
    await bus.publish(event)
    assert seen == [("sync", event.id), ("async", event.id), ("sync", event.id)]


@pytest.mark.asyncio
async def test_listener_failure_does_not_escape():
    bus = EventBus()

    def bad_listener(event):
        raise RuntimeError("observer broke")

    bus.subscribe("x", bad_listener)
    await bus.publish(Event("run_x", "x"))

