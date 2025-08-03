#!/usr/bin/env python
# -*- coding: utf-8 -*-

from DummyRacecar import DummyRacecar
from KpiPlotter import KpiPlotter
import gi, json, asyncio, websockets, time, statistics, cv2, numpy as np
"""
Jetson Nano – JPEG over WebSocket + adaptive resolution/quality

Flags
    --mode auto   (default)  : step through PROFILES on RTT / buffer pressure
    --mode fixed  --quality  : lock to JPEG quality at first profile's size
    --logs                   : verbose console output
"""
import gi, argparse, json, asyncio, websockets, time, signal, statistics, sys
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
from datetime import datetime
from jetracer.nvidia_racecar import NvidiaRacecar

# ───────── connection + timing ────────────────────────────────────────────────────────
MIDDLEWARE_URI = "ws://74.56.22.147:8765/robot"  # Port 8764 is the WebSocket Server #2

PING_INTERVAL     = 5           # seconds – send ping this often
PING_TIMEOUT      = 5           # seconds – drop connection if no pong
RTT_WINDOW        = 4           # keep this many RTT samples
# more eager adaptation:
UPGRADE_RTT_MAX   = 50  / 1000  # seconds -  avg RTT <  50 ms → step _up_
DOWNGRADE_RTT_MIN = 120 / 1000  # seconds -  avg RTT > 120 ms → step _down_
MAX_BACKOFF       = 30          # seconds - reconnect cap
# react to smaller buffers, downshift faster:
HIGH_WATER        = 128 * 1024  # bytes
SKIP_LIMIT        = 1           # skipped frames before profile downgrade

CAPTURE_WIDTH  = 1280  # pixels  – width of capture
CAPTURE_HEIGHT = 720   # pixels  – height of capture
CAPTURE_FPS    = 60    # fps - frames per seconds of capture

TARGET_FPS = 30  # fps - target frames per seconds
# cap a single JPEG size to avoid “flash on motion” bursts:
FRAME_MAX_BYTES = 110 * 1024  # ~110 kB per frame (tune 100–180 kB)
# ───────────────────────────────────────────────────────────────────────────────────────

# ─────── SENSOR_MODE (with cheatsheet) ─────────
#   0: 3264×2464 @ 21 fps      ◄─ (full 8 MP)
#   1: 3264×1848 @ 28 fps
#   2: 1920×1080 @ 30 fps
#   3: 1640×1232 @ 30 fps
#   4: 1280×720  @ 60 fps      ◄─ (default 720p)
#   5: 1280×720  @120 fps
SENSOR_MODE = 4
# ───────────────────────────────────────────────

# Quality ladder (no resolution change)
QUALITIES = [80, 75, 70, 65]   # 0 = best

# ───────────────────────── Flags ──────────────────────────────────
p = argparse.ArgumentParser()
p.add_argument("--mode", choices=["auto", "fixed"], default="auto")
p.add_argument("--quality", type=int, default=70,
                help="JPEG quality when --mode fixed (1-100)")
p.add_argument("--logs", action="store_true")
args = p.parse_args()
MODE_AUTO     = args.mode == "auto"
FIXED_QUALITY = max(1, min(args.quality, 100))
LOG_ENABLED   = args.logs
# ──────────────────────────────────────────────────────────────────

stats = KpiPlotter()
kpi = False

Gst.init(None)
loop = asyncio.get_event_loop()
INITIAL_QUALITY_IDX = 2          # start @70 %

def log(*a): print(datetime.now().isoformat(' ', 'seconds'), *a)

def build_pipeline(target_width, target_height, quality):
    return Gst.parse_launch(
        f"nvarguscamerasrc sensor-id=0 sensor-mode={SENSOR_MODE} ! "
        f"video/x-raw(memory:NVMM),format=NV12,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},framerate={CAPTURE_FPS}/1 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw(memory:NVMM),format=NV12,width={target_width},height={target_height} ! "
        f"queue max-size-buffers=1 leaky=2 ! "
        f"nvjpegenc quality={quality} ! "
        f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )

class Camera:
    def __init__(self, width, height, quality):
        self.width, self.height, self.quality = width, height, quality
        self._start()
        
    def _start(self):
        self.pipeline = build_pipeline(self.width, self.height, self.quality)
        self.sink = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)
    
    def change_quality(self, quality):
        enc = next(e for e in self.pipeline.iterate_elements()
                   if e.get_factory().get_name() == "nvjpegenc")
        enc.set_property("quality", quality)
        self.quality = quality
         
    def grab(self):
        sample = self.sink.emit("pull-sample")
        buffer = sample.get_buffer()
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return None
        data = bytes(map_info.data)
        buffer.unmap(map_info)
        return data

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

quality_idx  = INITIAL_QUALITY_IDX
quality_lock = asyncio.Lock()
rtt_history  = []
cam          = None

async def send_frames(ws):
    global quality_idx
    global stats
    global kpi

    skipped = 0
    while True:
        t0 = time.perf_counter()
        time_read_start = int(time.time() * 1000)

        data = cam.grab()
        if data is None:
            await asyncio.sleep(0.01)
            continue

        if MODE_AUTO and len(data) > FRAME_MAX_BYTES and quality_idx < len(QUALITIES) - 1:
            step = 1
            async with quality_lock:
                quality_idx += step
                cam.change_quality(QUALITIES[quality_idx])
                log(f"size {len(data)//1024} kB – quality → {QUALITIES[quality_idx]}%")
            await asyncio.sleep(0)
            continue
            
        buffer_size = ws.transport.get_write_buffer_size()
        if MODE_AUTO and buffer_size > HIGH_WATER:
            skipped += 1
            step = 2 if buffer_size > HIGH_WATER*2 else 1
            if skipped >= SKIP_LIMIT and quality_idx + step < len(QUALITIES):
                async with quality_lock:
                    quality_idx += step
                    cam.change_quality(QUALITIES[quality_idx])
                    log(f"congestion – quality→{QUALITIES[quality_idx]}% (buffer {buffer_size//1024} kB)")
                    skipped = 0
            await asyncio.sleep(0)
            continue
        skipped = 0

        await ws.send(data)
        
        if kpi:
            stats.fps_count += 1
            stats.bps_count += (len(data)) / 1000000
            stats.calculate_local_video_delay(time_read_start)


        await asyncio.sleep(max(0, (1/TARGET_FPS) - (time.perf_counter() - t0)))

async def receive_commands(websocket, car: NvidiaRacecar):
    async for msg in websocket:
        time_read_start = int(time.time() * 1000)
        try:
            data = json.loads(msg)
            if LOG_ENABLED:
                log("Received message:", data) # LOGS (Remove for better performance)
            car.steering = data.get('steering', 0.0)
            car.throttle = data.get('throttle', 0.0)

            if kpi:
                stats.calculate_local_control_delay(time_read_start)
        except json.JSONDecodeError:
            log("Error: Bad JSON from client")

async def keep_alive(ws):
    global quality_idx
    if not MODE_AUTO:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            
    while True:
        await asyncio.sleep(PING_INTERVAL)
        t0 = time.perf_counter()
        pong = await ws.ping()
        await asyncio.wait_for(pong, timeout=PING_TIMEOUT)
        rtt = time.perf_counter() - t0

        rtt_history.append(rtt)
        rtt_history[:] = rtt_history[-RTT_WINDOW:]
        avg = statistics.mean(rtt_history)

        async with quality_lock:
            if avg > DOWNGRADE_RTT_MIN and quality_idx < len(QUALITIES) - 1:
                quality_idx += 1
                cam.change_quality(QUALITIES[quality_idx])
                log(f"RTT {avg*1e3:.0f} ms – downgrade quality → {QUALITIES[quality_idx]}%")
            elif avg < UPGRADE_RTT_MAX and quality_idx > 0:
                quality_idx -= 1
                cam.change_quality(QUALITIES[quality_idx])
                log(f"RTT {avg*1e3:.0f} ms – upgrade quality → {QUALITIES[quality_idx]}%")

async def run_once(uri: str, car: NvidiaRacecar):
    global stats
    global kpi

    async with websockets.connect(
        uri,
        ping_interval=None,
        max_queue=None,
        max_size=None
    ) as ws:
        log("WebSocket connected")
        
        sender   = loop.create_task(send_frames(ws))
        receiver = loop.create_task(receive_commands(ws, car))
        pinger   = loop.create_task(keep_alive(ws))
        watcher  = loop.create_task(ws.wait_closed())

        if kpi:
            await stats.start_kpi_servers()

        done, pending = await asyncio.wait(
            [sender, receiver, pinger, watcher],
            return_when=asyncio.FIRST_EXCEPTION
        )
        for task in pending:
            task.cancel()
    car.steering = car.throttle = 0.0
    log("WebSocket closed")
    
async def main():
    global cam
    cam = Camera(CAPTURE_WIDTH, CAPTURE_HEIGHT, FIXED_QUALITY if not MODE_AUTO else QUALITIES[quality_idx])


    try:
        car = NvidiaRacecar()
    except Exception as e:
        print(f"Warning: Failed to initialize NvidiaRacecar due to I2C error: {e}")
        print("Using DummyRacecar instead.")
        car = DummyRacecar()
    car.steering = 0
    car.throttle = 0

    print("Robot is ready")
    
    backoff = 0.5
    while True:
        try:
            await run_once(MIDDLEWARE_URI, car)
            backoff = 0.5
        except Exception as e:
            log(f"Error: {e} – reconnecting in {backoff}s")
            car.steering = car.throttle = 0.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

# ────────────────── Graceful Ctrl-C ───────────────────────
def shutdown(loop):
    for task in asyncio.Task.all_tasks(loop):
        task.cancel()
    if cam:
        cam.stop()
    loop.stop()

if __name__ == "__main__":
    Gst.init(None)
    loop = asyncio.get_event_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, loop)
    try:
        loop.run_until_complete(main())
    finally:
        Gst.deinit()
        loop.close()
        print("Exited cleanly")
