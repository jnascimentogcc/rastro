from procrastinate import testing

from rastro_worker.app import app
from rastro_worker.tasks import ping


async def test_ping_runs_through_the_queue() -> None:
    with app.replace_connector(testing.InMemoryConnector()) as test_app:
        async with test_app.open_async():
            await ping.defer_async(message="hello")
            await test_app.run_worker_async(wait=False)

            jobs = list(test_app.connector.jobs.values())  # type: ignore[attr-defined]

    assert len(jobs) == 1
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["task_name"] == "ping"
