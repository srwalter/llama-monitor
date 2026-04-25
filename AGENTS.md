# llama-monitor

## Goal

Create a web application to monitor llama.cpp's progress by watching Docker container logs. The app should:
- Stream logs from `docker logs -f llama`
- Parse progress lines showing `progress = X.XXXX` and update progress bars
- Parse timing lines showing prompt eval t/s and eval t/s for the last run
- Display everything in a real-time web dashboard with progress bars
- Monitor GPU utilization with a live line graph

## Instructions

- Parse lines matching: `slot update_slots: id X | task Y | prompt processing progress, ... progress = Z`
- Parse timing blocks starting with `slot print_timing` followed by 3 lines (prompt eval time, eval time, total time)
- Extract tokens/second values from timing lines
- Display progress bars and timing data in a web dashboard
- Poll `/sys/class/drm/card1/device/gpu_busy_percent` for GPU utilization once per second
- Poll `/gpu` endpoint once every 0.5 seconds for live graph updates

## Discoveries

1. **Docker logs outputs to stderr, not stdout** - `docker logs -f llama` sends all output to stderr. The original code was reading from `proc.stdout` which was always empty.

2. **Asyncio subprocess doesn't work with docker logs** - `asyncio.create_subprocess_exec` with `proc.stderr.readline()` hangs indefinitely when reading from docker logs over SSH. The subprocess starts but readline never returns data.

3. **Threading with select works** - The solution requires running the docker logs subprocess in a background thread, using `select.select()` on the stderr file descriptor to poll for available data, then storing lines in a shared list protected by a lock.

4. **Jinja2 template caching bug** - The original Jinja2Templates approach failed with `TypeError: cannot use 'tuple' as a dict key` on Python 3.14. Solved by reading the HTML file directly with `Path.read_text()` instead of using Jinja2 templating.

5. **docker logs produces continuous data** - The llama.cpp container continuously outputs progress updates as it processes requests, so the app needs to handle a never-ending stream.

6. **SSE format requires `data: ` prefix and `\n\n` suffix** - The browser's `EventSource` requires proper SSE format. Without this, no events are parsed.

7. **Progress regex needed fixing** - The original regex expected `slot <number> update_slots:` but the actual log format is `slot update_slots: id <number>` with no number after `slot`. The slot number is the `id` value.

8. **Raw log events flooding the browser** - Sending every log line as an SSE event on the main `/events` endpoint flooded the browser with DOM updates, causing it to freeze before processing progress events. Solution: separate log viewer into its own `/logs` SSE endpoint.

9. **GPU busy percent file exists** - `/sys/class/drm/card1/device/gpu_busy_percent` contains integer values (0-100) representing GPU utilization.

## Accomplished

- Created a FastAPI web app with SSE (Server-Sent Events) endpoints
- Implemented a `LogReader` class using threading + select to read docker logs from stderr
- SSE endpoint (`/events`) parses progress lines and timing lines from the log stream
- Separate log viewer endpoint (`/logs`) streams raw log lines independently
- HTML dashboard with progress bars, timing display, log viewer, and GPU utilization graph
- Fixed multiple bugs: stderr vs stdout, asyncio subprocess hanging, Jinja2 caching, SSE format, regex patterns
- SSE reconnection with exponential backoff (2s-30s) and inactivity timer (60s)
- Auto-collapse completed tasks after 30 seconds with elapsed time display
- Log viewer with toggle, color-coded lines, autoscroll toggle, and buffer management
- GPU utilization monitoring: polls every second, streams via `/gpu` endpoint, displays percentage and canvas line graph showing ~60 seconds of history
- All code committed to git with descriptive commit messages

## Relevant files / directories

- `/mnt/bigdisk/home/srwalter/src/llama-monitor/main.py` - FastAPI application with LogReader, GpuMonitor classes, SSE endpoints (/events, /logs, /gpu)
- `/mnt/bigdisk/home/srwalter/src/llama-monitor/templates/index.html` - Web dashboard with progress bars, timing display, log viewer, and GPU utilization graph
- `/mnt/bigdisk/home/srwalter/src/llama-monitor/requirements.txt` - Dependencies (fastapi, uvicorn)
- `/mnt/bigdisk/home/srwalter/src/llama-monitor/` - Project root directory
- `/sys/class/drm/card1/device/gpu_busy_percent` - GPU utilization file (read by GpuMonitor)
