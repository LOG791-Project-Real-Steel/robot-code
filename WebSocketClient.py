#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Jetson-Nano 640×360 JPEG streamer (constant output)

Flags
  --quality N     JPEG quality 1-100 (default 75)
  --logs          verbose logs (steering/throttle)
  --logsRtt       also log every RTT ping
"""
import gi, argparse, json, asyncio, websockets, time, signal, statistics, sys
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
from datetime import datetime
from jetracer.nvidia_racecar import NvidiaRacecar

# ─── connection + timing ──────────────────────────────────────────────
MIDDLEWARE_URI  = "ws://74.56.22.147:8765/robot"

PING_INTERVAL = 5   # seconds
PING_TIMEOUT  = 5   # seconds
MAX_BACKOFF   = 30  # seconds

RTT_WINDOW = 4

# ─── video parameters ─────────────────────────────────────────────────
CAPTURE_WIDTH   = 1280  # pixels
CAPTURE_HEIGHT  = 720   # pixels
CAPTURE_FPS     = 60  

TARGET_WIDTH  = 640  # Target -> 360p output
TARGET_HEIGHT = 360     
TARGET_FPS    = 30

# ─── CLI flags ────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--quality", type=int, default=75,
                    help="JPEG quality 1-100 (default 75)")
parser.add_argument("--logs",      action="store_true")
parser.add_argument("--logsRtt",   action="store_true")
args = parser.parse_args()

QUALITY          = max(1, min(args.quality, 100))
LOG_ENABLED      = args.logs
LOG_RTT_ENABLED  = args.logsRtt

loop = asyncio.get_event_loop()

# ─── helpers ──────────────────────────────────────────────────────────
def log(*items):
    print(datetime.now().isoformat(' ', 'seconds'), *items)

def build_pipeline(quality):
    return Gst.parse_launch(
        f"nvarguscamerasrc ! "
        f"video/x-raw(memory:NVMM),width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},"
        f"framerate={CAPTURE_FPS}/1,format=NV12 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw(memory:NVMM),width={TARGET_WIDTH},height={TARGET_HEIGHT},format=NV12 ! "
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
        data = cam.grab()
        if data:
            await ws.send(data)
        await asyncio.sleep(max(0.0, (1/TARGET_FPS) - (time.perf_counter() - t0)))

async def receive_commands(ws, car: NvidiaRacecar):
    async for msg in ws:
        try:
            data = json.loads(msg)
            if LOG_ENABLED:
                log("Controls: ", data)
            car.steering = data.get("steering", 0.0)
            car.throttle = data.get("throttle", 0.0)
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
    car = NvidiaRacecar()
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
        Gst.deinit()
        loop.close()
