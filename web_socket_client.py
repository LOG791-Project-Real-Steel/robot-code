#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Jetson-Nano 640×360 JPEG streamer (constant output)

Flags
  --quality N     JPEG quality 1-100 (default 75)
  --logs          verbose logs (steering/throttle)
  --logsRtt       also log every RTT ping
"""
from dummy_racecar import DummyRacecar
from kpi_plotter_ws import KpiPlotter
import gi, argparse, json, asyncio, websockets, time, signal, statistics, sys
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
from datetime import datetime
from jetracer.nvidia_racecar import NvidiaRacecar

# ─── connection + timing ──────────────────────────────────────────────
MIDDLEWARE_URI  = "ws://0.0.0.0:8764/robot" # Put your server's IP here

PING_INTERVAL = 5   # seconds
PING_TIMEOUT  = 5   # seconds
MAX_BACKOFF   = 30  # seconds

RTT_WINDOW = 4

# ─── video parameters ─────────────────────────────────────────────────
CAPTURE_WIDTH   = 1280  # pixels
CAPTURE_HEIGHT  = 720   # pixels
CAPTURE_FPS     = 60  

DEFAULT_RESOLUTION = 360     
DEFAULT_FPS        = 30

# ─────── SENSOR_MODE (with cheatsheet) ─────────
#   0: 3264×2464 @ 21 fps      ◄─ (full 8 MP)
#   1: 3264×1848 @ 28 fps
#   2: 1920×1080 @ 30 fps
#   3: 1640×1232 @ 30 fps
#   4: 1280×720  @ 60 fps      ◄─ (default 720p)
#   5: 1280×720  @120 fps
SENSOR_MODE = 4
# ───────────────────────────────────────────────

POSSIBLE_RESOLUTIONS = [144, 240, 360, 480, 720]
POSSIBLE_FPS = range(25, 61, 1)

# ─── CLI flags ────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--quality", type=int, default=75, help="JPEG quality 1-100 (default 75)")
parser.add_argument("--logs", action="store_true")
parser.add_argument("--logsRtt", action="store_true")
parser.add_argument("--kpi", action="store_true", default=False)
parser.add_argument("--res", choices=POSSIBLE_RESOLUTIONS, type=int, default=DEFAULT_RESOLUTION)
parser.add_argument("--fps", choices=POSSIBLE_FPS, type=int, default=DEFAULT_FPS)
args = parser.parse_args()

QUALITY           = max(1, min(args.quality, 100))
LOG_ENABLED       = args.logs
LOG_RTT_ENABLED   = args.logsRtt
TARGET_RESOLUTION = args.res
TARGET_FPS        = args.fps
kpi               = args.kpi

stats = KpiPlotter()
loop = asyncio.get_event_loop()

# ─── helpers ──────────────────────────────────────────────────────────
def log(*items):
    print(datetime.now().isoformat(' ', 'seconds'), *items)

def build_pipeline(quality):
    return Gst.parse_launch(
        f"nvarguscamerasrc sensor-id=0 sensor-mode={SENSOR_MODE} ! "
        f"video/x-raw(memory:NVMM),format=NV12,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},framerate={CAPTURE_FPS}/1 !"
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw(memory:NVMM),width={int(TARGET_RESOLUTION * 16/9)},height={TARGET_RESOLUTION},format=NV12 ! "
        f"nvjpegenc quality={quality} ! "
        f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )

class Camera:
    def __init__(self, q):
        self.pipeline = build_pipeline(q)
        self.sink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)

    def grab(self):
        sample = self.sink.emit("pull-sample")
        if sample:
            buffer = sample.get_buffer()
            ok, info = buffer.map(Gst.MapFlags.READ)
            if ok:
                data = bytes(info.data)
                buffer.unmap(info)
                return data
        return None

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

async def send_frames(ws, cam: Camera):
    while True:
        t0 = time.perf_counter()
        time_read_start = int(time.time() * 1000)

        data = cam.grab()
        if data:
            await ws.send(data)

            if kpi:
                stats.fps_count += 1
                stats.bps_count += (len(data)) / 1000000
                stats.calculate_local_video_delay(time_read_start)
        await asyncio.sleep(max(0.0, (1/TARGET_FPS) - (time.perf_counter() - t0)))

async def receive_commands(ws, car: NvidiaRacecar):
    async for msg in ws:
        time_read_start = int(time.time() * 1000)
        try:
            data = json.loads(msg)
            if LOG_ENABLED:
                log("Controls: ", data)
            car.steering = data.get("steering", 0.0)
            car.throttle = data.get("throttle", 0.0)
            
            if kpi:
                stats.calculate_local_control_delay(time_read_start)
        except json.JSONDecodeError:
            log("Bad JSON")

async def keep_alive(ws):
    rtt_history = []
    while True:
        await asyncio.sleep(PING_INTERVAL)
        t0 = time.perf_counter()
        pong = await ws.ping()
        await asyncio.wait_for(pong, timeout=PING_TIMEOUT)
        rtt = time.perf_counter() - t0
        
        rtt_history.append(rtt)
        rtt_history[:] = rtt_history[-RTT_WINDOW:]
        
        if LOG_RTT_ENABLED:
            avg = statistics.mean(rtt_history)
            log(f"RTT {rtt*1e3:.0f} ms (avg {avg*1e3:.0f} ms)")

async def run_session(uri, cam: Camera):
    try:
        car = NvidiaRacecar()
    except Exception as e:
        print(f"Warning: Failed to initialize NvidiaRacecar due to I2C error: {e}")
        print("Using DummyRacecar instead.")
        car = DummyRacecar()
    car.steering_gain  = -1
    car.throttle_gain  = 0.8

    backoff = 0.5
    while True:
        try:
            async with websockets.connect(uri, ping_interval=None) as ws:
                log("WebSocket connected")
                tasks = [
                    loop.create_task(send_frames(ws, cam)),
                    loop.create_task(receive_commands(ws, car)),
                    loop.create_task(keep_alive(ws)),
                    loop.create_task(ws.wait_closed())
                ]

                if kpi:
                    await stats.start_kpi_servers()

                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in pending:
                    task.cancel()
                log("WebSocket closed")
        except Exception as e:
            log("Error:", e, f"– reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        finally:
            car.steering = car.throttle = 0.0

# ─── graceful shutdown ────────────────────────────────────────────────
def shutdown(loop, cam: Camera):
    for t in asyncio.Task.all_tasks(loop):
        t.cancel()
    cam.stop()
    loop.stop()

async def main():
    cam = Camera(QUALITY)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, loop, cam)
    await run_session(MIDDLEWARE_URI, cam)

# ─── entry ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    Gst.init(None)
    try:
        loop.run_until_complete(main())
    finally:
        if kpi:
            stats.plot_kpis()

        Gst.deinit()
        loop.close()
