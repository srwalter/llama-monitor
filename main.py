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

TIMING_RE = re.compile(
    r"(prompt eval|eval|total)\s+time\s*=\s+[\d.]+\s+ms\s*/\s*\d+\s+tokens\s*\([^)]*?([\d.]+)\s+tokens\s+per\s+second\)"
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

    async def parse_events():
        current_slot = None
        current_task = None
        timing_lines = []

        async for line in stream_logs():
            # Check for progress line
            m = PROMPT_PROGRESS_RE.search(line)
            if m:
                current_slot = m.group(1)
                current_task = m.group(3)
                progress = float(m.group(4))
                yield f'{{"type": "progress", "slot": "{current_slot}", "task": "{current_task}", "progress": {progress:.6f}}}'
                continue

            # Check for timing block start
            if "print_timing" in line:
                timing_lines = []
                tm = re.search(r"slot\s+(\d+).*?task\s+(\d+)", line)
                if tm:
                    current_slot = tm.group(1)
                    current_task = tm.group(2)
                continue

            # Collect timing lines
            if current_slot and current_task and "time =" in line and "tokens" in line:
                timing_lines.append(line)
                if len(timing_lines) >= 3:
                    prompt_tps = None
                    eval_tps = None
                    for tl in timing_lines:
                        tm2 = TIMING_RE.search(tl)
                        if tm2:
                            kind = tm2.group(1)
                            tps = float(tm2.group(2))
                            if kind == "prompt eval":
                                prompt_tps = tps
                            elif kind == "eval":
                                eval_tps = tps
                    if prompt_tps is not None or eval_tps is not None:
                        yield f'{{"type": "timing", "slot": "{current_slot}", "task": "{current_task}", "prompt_tps": {prompt_tps or 0:.2f}, "eval_tps": {eval_tps or 0:.2f}}}'
                    timing_lines = []
                continue

            # Reset on any other line that looks like a new prompt processing start
            if "prompt processing" not in line and "print_timing" not in line and "update_slots" not in line:
                if current_slot and not ("prompt processing" in line or "time =" in line):
                    current_slot = None
                    current_task = None

    app.state.events = parse_events()
    logger.info("Event generator ready")

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
