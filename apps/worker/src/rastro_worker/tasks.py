from rastro_worker.app import app


@app.task(name="ping")
async def ping(message: str = "pong") -> str:
    """Smoke-test task proving the queue and the worker are wired up."""
    return message
