import os
import re
import json
import logging
import threading
import select
from pathlib import Path
from typing import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PROMPT_PROGRESS_RE = re.compile(
    r"slot\s+update_slots:\s+id\s+(\d+)\s+\|\s+task\s+(\d+)\s+\|\s+prompt processing progress.*?progress\s*=\s*([\d.]+)"
)

TIMING_RE = re.compile(
    r"(prompt eval|eval|total)\s+time\s*=\s+[\d.]+\s+ms\s*/\s*\d+\s+tokens\s*\([^)]*?([\d.]+)\s+tokens\s+per\s+second\)"
)

DOCKER_CONTAINER = "llama"
DOCKER_CMD = ["docker", "logs", "-f", DOCKER_CONTAINER]

BASE_DIR = Path(__file__).parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"


class LogReader:
    def __init__(self):
        self._lines = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._line_count = 0

    def start(self):
        env = os.environ.copy()
        env["DOCKER_HOST"] = "ssh://filer.lan"
        self._thread = threading.Thread(
            target=self._stream_logs,
            args=(env,),
            daemon=True,
        )
        self._thread.start()
        logger.info("Started docker logs thread")

    def _stream_logs(self, env):
        import subprocess

        proc = subprocess.Popen(
            DOCKER_CMD,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        logger.info("Started docker logs subprocess")

        try:
            while not self._stop_event.is_set():
                ready, _, _ = select.select([proc.stderr], [], [], 0.5)
                if ready:
                    line = proc.stderr.readline()
                    if line:
                        stripped = line.strip()
                        if stripped:
                            with self._lock:
                                self._lines.append(stripped)
                                self._line_count += 1
                            if self._line_count % 100 == 0:
                                logger.info(f"Collected {self._line_count} log lines")
        finally:
            proc.terminate()
            proc.wait()
            logger.info("Stopped docker logs subprocess")

    def get_lines(self):
        with self._lock:
            lines = list(self._lines)
            self._lines.clear()
            return lines

    def get_line_count(self):
        with self._lock:
            return self._line_count

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


log_reader = LogReader()
GPU_BUSY_PATH = "/sys/class/drm/card1/device/gpu_busy_percent"


class GpuMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._utilization = 0
        self._history = []
        self._max_history = 60
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._poll,
            daemon=True,
        )
        self._thread.start()
        logger.info("Started GPU monitor")

    def _poll(self):
        while not self._stop_event.is_set():
            try:
                with open(GPU_BUSY_PATH) as f:
                    value = int(f.read().strip())
            except Exception:
                value = 0
            with self._lock:
                self._utilization = value
                self._history.append(value)
                if len(self._history) > self._max_history:
                    self._history = self._history[-self._max_history:]
            self._stop_event.wait(1)

    def get_utilization(self):
        with self._lock:
            return self._utilization

    def get_history(self):
        with self._lock:
            return list(self._history)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


gpu_monitor = GpuMonitor()


class TempMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._temps = {}
        self._stop_event = threading.Event()
        self._thread = None
        self._sensors = self._discover_sensors()

    @staticmethod
    def _discover_sensors():
        sensors = []
        hwmon_paths = sorted(Path("/sys/class/hwmon").glob("hwmon*"))
        for hwmon_path in hwmon_paths:
            name = ""
            name_path = hwmon_path / "name"
            if name_path.exists():
                name = name_path.read_text().strip()
            for temp_input in sorted(hwmon_path.glob("temp*_input")):
                try:
                    value = int(temp_input.read_text().strip())
                    if value > 0:
                        label = ""
                        label_path = Path(str(temp_input)[:-len("_input")] + "label")
                        if label_path.exists():
                            label = label_path.read_text().strip()
                        if not label:
                            temp_num = temp_input.name.replace("temp_", "").replace("_input", "")
                            label = f"{name} temp{temp_num}" if name else temp_num
                        sensors.append({
                            "path": str(temp_input),
                            "name": label,
                            "hwmon": name,
                        })
                except (ValueError, OSError):
                    pass
        return sensors

    def start(self):
        self._thread = threading.Thread(
            target=self._poll,
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Started temperature monitor ({len(self._sensors)} sensors)")

    def _poll(self):
        while not self._stop_event.is_set():
            new_temps = {}
            for sensor in self._sensors:
                try:
                    with open(sensor["path"]) as f:
                        value = int(f.read().strip()) / 1000.0
                    new_temps[sensor["name"]] = round(value, 1)
                except Exception:
                    pass
            with self._lock:
                self._temps = new_temps
            self._stop_event.wait(2)

    def get_temps(self):
        with self._lock:
            return dict(self._temps)

    def get_sensor_names(self):
        return [s["name"] for s in self._sensors]

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


temp_monitor = TempMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_reader.start()
    gpu_monitor.start()
    temp_monitor.start()
    logger.info("Event generator ready")

    yield

    log_reader.stop()
    gpu_monitor.stop()
    temp_monitor.stop()


app = FastAPI(title="llama.cpp Progress Monitor", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = TEMPLATE_PATH.read_text()
    return HTMLResponse(content=html)


@app.get("/events")
async def sse_events():
    async def event_generator() -> AsyncGenerator[str, None]:
        import asyncio

        current_slot = None
        current_task = None
        timing_lines = []

        while True:
            lines = await asyncio.to_thread(log_reader.get_lines)
            for line in lines:
                # Check for progress line
                m = PROMPT_PROGRESS_RE.search(line)
                if m:
                    current_slot = m.group(1)
                    current_task = m.group(2)
                    progress = float(m.group(3))
                    yield f'data: {{"type": "progress", "slot": "{current_slot}", "task": "{current_task}", "progress": {progress:.6f}}}\n\n'
                    continue

                # Check for timing block start
                if "print_timing" in line:
                    timing_lines = []
                    tm = re.search(r"slot\s+print_timing:\s+id\s+(\d+).*?task\s+(\d+)", line)
                    if tm:
                        current_slot = tm.group(1)
                        current_task = tm.group(2)
                    continue

                # Collect timing lines
                if current_slot and current_task and "time =" in line and "tokens" in line:
                    timing_lines.append(line)
                    if len(timing_lines) >= 2:
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
                            yield f'data: {{"type": "timing", "slot": "{current_slot}", "task": "{current_task}", "prompt_tps": {prompt_tps or 0:.2f}, "eval_tps": {eval_tps or 0:.2f}}}\n\n'
                        timing_lines = []
                    continue

                # Reset on any other line that looks like a new prompt processing start
                if "prompt processing" not in line and "print_timing" not in line and "update_slots" not in line:
                    if current_slot and not ("prompt processing" in line or "time =" in line):
                        current_slot = None
                        current_task = None

            await asyncio.sleep(0.2)
            yield ':\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/logs")
async def sse_logs():
    async def log_generator() -> AsyncGenerator[str, None]:
        import asyncio

        while True:
            lines = await asyncio.to_thread(log_reader.get_lines)
            for line in lines:
                yield f'data: {json.dumps({"type": "log", "message": line})}\n\n'
            await asyncio.sleep(0.1)
            yield ':\n\n'

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/gpu")
async def sse_gpu():
    async def gpu_generator() -> AsyncGenerator[str, None]:
        import asyncio

        current_history = []
        while True:
            util = await asyncio.to_thread(gpu_monitor.get_utilization)
            history = await asyncio.to_thread(gpu_monitor.get_history)
            if history != current_history:
                current_history = history
                yield f'data: {json.dumps({"type": "gpu", "utilization": util, "history": history})}\n\n'
            await asyncio.sleep(0.5)
            yield ':\n\n'

    return StreamingResponse(
        gpu_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/temp")
async def sse_temp():
    async def temp_generator() -> AsyncGenerator[str, None]:
        import asyncio

        current_temps = {}
        while True:
            temps = await asyncio.to_thread(temp_monitor.get_temps)
            if temps != current_temps:
                current_temps = temps
                yield f'data: {json.dumps({"type": "temp", "temps": temps})}\n\n'
            await asyncio.sleep(0.5)
            yield ':\n\n'

    return StreamingResponse(
        temp_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
