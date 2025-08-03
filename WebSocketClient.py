#!/usr/bin/env python
# -*- coding: utf-8 -*-

import gi, json, asyncio, websockets, time, statistics, cv2, numpy as np
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp, GLib
from datetime import datetime
from jetracer.nvidia_racecar import NvidiaRacecar

MIDDLEWARE_URI = "ws://74.56.22.147:8765/robot"
PING_INTERVAL     = 5           # seconds – send ping this often
PING_TIMEOUT      = 5           # seconds – drop connection if no pong
RTT_WINDOW        = 4           # keep this many RTT samples
UPGRADE_RTT_MAX   = 120 / 1000  # seconds -  avg RTT < 120 ms  → step _up_
DOWNGRADE_RTT_MIN = 250 / 1000  # seconds - avg RTT > 250 ms  → step _down_
MAX_BACKOFF       = 30          # seconds - reconnect cap
HIGH_WATER        = 256 * 1024  # bytes
SKIP_LIMIT        = 8           # skipped frames before profile downgrade

CAPTURE_WIDTH  = 1280      # pixels  – width of capture
CAPTURE_HEIGHT = 720       # pixels  – height of capture
CAPTURE_FPS    = 60        # fps - frames per seconds or capture

STREAM_WIDTH  = 640        # pixels  – width of stream
STREAM_HEIGHT = 360        # pixels  – height of stream
STREAM_FPS    = 30         # fps - actual frames per seconds

# ----------- SENSOR_MODE cheat-sheet -----------
#   0: 3264×2464 @ 21 fps      ◄─ (full 8 MP)
#   1: 3264×1848 @ 28 fps
#   2: 1920×1080 @ 30 fps
#   3: 1640×1232 @ 30 fps
#   4: 1280×720  @ 60 fps      ◄─ (default 720p)
#   5: 1280×720  @120 fps
# -----------------------------------------------
SENSOR_MODE = 4

QUALITIES = [ # index 0 = “best”
    80,   # 0 | ≈ 60–90 kB
    75,   # 1 | ≈ 40–60 kB
    70,   # 2 | ≈ 25–40 kB   ▶ default
    65,   # 3 | ≈ 15–25 kB
]
INITIAL_QUALITY = 2

Gst.init(None)

def log(msg, *extra):
    print(datetime.now().isoformat(sep=' ', timespec='seconds'), msg, *extra)

def build_pipeline(quality):
    pipe_str = (
        f"nvarguscamerasrc sensor-id=0 sensor-mode={SENSOR_MODE} ! "
        f"video/x-raw(memory:NVMM),format=NV12,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},framerate={CAPTURE_FPS}/1 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw(memory:NVMM),format=NV12 ! "
        f"nvjpegenc quality={quality} ! "
        f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )
    pipeline = Gst.parse_launch(pipe_str)
    sink = pipeline.get_by_name("sink")
    return pipeline, sink
   
loop = asyncio.get_event_loop()
create_task = loop.create_task

q_idx = INITIAL_QUALITY
q_lock = asyncio.Lock()
rtt_history  = []

class Camera:
    def __init__(self, quality):
        self.pipeline, self.sink = build_pipeline(quality)
        self.pipeline.set_state(Gst.State.PLAYING)

    def change_quality(self, q):
        enc = next(e for e in self.pipeline.iterate_elements() if e.name.startswith('nvjpegenc'))
        enc.set_property("quality", q)

    def fetch_jpeg(self):
        sample = self.sink.emit("pull-sample")
        buf = sample.get_buffer()
        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return None
        data = map_info.data
        buf.unmap(map_info)
        return data

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

async def send_frames(ws, cam: Camera):
    global q_idx
    skipped = 0
    while True:
        t0 = time.perf_counter()
        data = cam.fetch_jpeg()
        if data is None:
            await asyncio.sleep(0.01)
            continue

        if ws.transport.get_write_buffer_size() > HIGH_WATER:
            skipped += 1
            if skipped >= SKIP_LIMIT and q_idx < len(QUALITIES) - 1:
                async with q_lock:
                    q_idx += 1
                    new_profile = QUALITIES[q_idx]
                    cam.change_quality(new_profile)
                    log(f"congestion – quality → {new_profile}")
                    skipped = 0
            await asyncio.sleep(0)
            continue
        skipped = 0

        await ws.send(data)
        await asyncio.sleep(max(0, (1/STREAM_FPS) - (time.perf_counter() - t0)))

async def receive_commands(websocket, car: NvidiaRacecar):
    async for msg in websocket:
        try:
            data = json.loads(msg)
            log("Received message:", data) # LOGS (Remove for better performance)
            car.steering = data.get('steering', 0.0)
            car.throttle = data.get('throttle', 0.0)
        except json.JSONDecodeError:
            log("Error: Bad JSON from client")

async def keep_alive(ws, cam: Camera):
    global q_idx
    while True:
        await asyncio.sleep(PING_INTERVAL)
        t0 = time.perf_counter()
        pong_waiter = await ws.ping()
        await asyncio.wait_for(pong_waiter, timeout=PING_TIMEOUT)
        rtt = time.perf_counter() - t0

        rtt_history.append(rtt)
        if len(rtt_history) > RTT_WINDOW:
            rtt_history.pop(0)
        avg = statistics.mean(rtt_history)

        async with q_lock:
            if avg > DOWNGRADE_RTT_MIN and q_idx < len(QUALITIES) - 1:
                q_idx += 1
                cam.change_quality(QUALITIES[q_idx])
                log(f"RTT {avg*1e3:.0f} ms – downgrade → {QUALITIES[q_idx]}")
            elif avg < UPGRADE_RTT_MAX and q_idx > 0:
                q_idx -= 1
                cam.change_quality(QUALITIES[q_idx])
                log(f"RTT {avg*1e3:.0f} ms – upgrade → {QUALITIES[q_idx]}")

async def run_once(uri: str, car: NvidiaRacecar, cam: Camera):
    async with websockets.connect(
        uri,
        ping_interval=None,
        max_queue=None,
        max_size=None
    ) as ws:
        log("WebSocket connected")
        
        sender   = loop.create_task(send_frames(ws, cam))
        receiver = loop.create_task(receive_commands(ws, car))
        pinger   = loop.create_task(keep_alive(ws, cam))
        watcher  = loop.create_task(ws.wait_closed())

        done, pending = await asyncio.wait(
            [sender, receiver, pinger, watcher],
            return_when=asyncio.FIRST_EXCEPTION
        )
        for t in pending:
            t.cancel()
    car.steering = car.throttle = 0.0
    log("WebSocket closed")
    
async def main():
    cam = Camera(QUALITIES[INITIAL_QUALITY])

    car = NvidiaRacecar()
    car.steering_gain = -1
    car.steering_offset = 0
    car.steering = 0
    car.throttle_gain = 0.8
    
    backoff = 0.5
    while True:
        try:
            await run_once(MIDDLEWARE_URI, car, cam)
            backoff = 0.5
        except Exception as e:
            log(f"Error: {e} – reconnecting in {backoff}s")
            car.steering = car.throttle = 0.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    finally:
        Gst.deinit()
