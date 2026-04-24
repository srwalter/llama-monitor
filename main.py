import os
import asyncio
import re
import logging
from pathlib import Path
from typing import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROMPT_PROGRESS_RE = re.compile(
    r"slot\s+(\d+)\s+update_slots:\s+id\s+(\d+)\s+\|\s+task\s+(\d+)\s+\|\s+prompt processing progress.*?progress\s*=\s*([\d.]+)"
)

DOCKER_CONTAINER = "llama"
DOCKER_CMD = ["docker", "logs", "-f", DOCKER_CONTAINER]

BASE_DIR = Path(__file__).parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = os.environ.copy()
    env["DOCKER_HOST"] = "ssh://filer.lan"
    proc = await asyncio.create_subprocess_exec(
        *DOCKER_CMD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    logger.info("Started docker logs subprocess")

    async def stream_logs():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                yield text

    async def parse_progress():
        async for line in stream_logs():
            m = PROMPT_PROGRESS_RE.search(line)
            if m:
                slot_id = m.group(1)
                task_id = m.group(3)
                progress = float(m.group(4))
                yield f'{{"slot": "{slot_id}", "task": "{task_id}", "progress": {progress:.6f}}}'

    app.state.events = parse_progress()
    logger.info("SSE event generator ready")

    yield

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (ProcessLookupError, asyncio.TimeoutError):
        pass
    logger.info("Stopped docker logs subprocess")


app = FastAPI(title="llama.cpp Progress Monitor", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = TEMPLATE_PATH.read_text()
    return HTMLResponse(content=html)


@app.get("/events")
async def sse_events():
    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            try:
                event = await app.state.events.__anext__()
                yield f"data: {event}\n\n"
                await asyncio.sleep(0.05)
            except StopAsyncIteration:
                break
            except Exception as e:
                logger.error(f"SSE error: {e}")
                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
