#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json, asyncio, websockets, time, statistics, cv2, numpy as np
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

PROFILES = [           #  index 0 = “best”
    (1280, 720, 80),   # 0 | ≈ 60–90 kB
    ( 960, 540, 75),   # 1 | ≈ 40–60 kB
    ( 640, 360, 70),   # 2 | ≈ 25–40 kB   ▶ default
    ( 480, 270, 65),   # 3 | ≈ 15–25 kB
]
INITIAL_PROFILE = 2

def log(msg, *extra):
    print(datetime.now().isoformat(sep=' ', timespec='seconds'), msg, *extra)

def gst_pipeline():
    return (
        f"nvarguscamerasrc sensor-id=0 sensor-mode={SENSOR_MODE} ! "
        f"video/x-raw(memory:NVMM),format=NV12,width={CAPTURE_WIDTH},height={CAPTURE_HEIGHT},framerate={CAPTURE_FPS}/1 ! "
        f"nvvidconv flip-method=0 ! "
        f"video/x-raw,format=BGRx,width={STREAM_WIDTH},height={STREAM_HEIGHT} ! "
        f"videoconvert ! "
        f"video/x-raw,format=YUY2 ! jpegenc ! "
        f"appsink max-buffers=1 drop=True"
    )
   
loop = asyncio.get_event_loop()
create_task = loop.create_task

profile_idx = INITIAL_PROFILE
profile_lock = asyncio.Lock()
rtt_samples  = []

def encode_frame(frame, w, h, quality):
    if frame.shape[1] != w or frame.shape[0] != h:
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(
        '.jpg', frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), quality,
         int(cv2.IMWRITE_JPEG_PROGRESSIVE), 1]
    )
    return buf.tobytes() if ok else None

async def send_frames(ws, stream):
    global profile_idx
    skipped = 0
    try:
        while True:
            t0 = time.perf_counter()
            ret, frame = stream.read()
            if not ret:
                log("Error: camera read failed")
                await asyncio.sleep(0.05)
                continue

            async with profile_lock:
                w, h, q = PROFILES[profile_idx]

            data = encode_frame(frame, w, h, q)
            if data is None:
                continue
            
            if ws.transport.get_write_buffer_size() > HIGH_WATER:
                skipped += 1
                if skipped >= SKIP_LIMIT and profile_idx < len(PROFILES) - 1:
                    async with profile_lock:
                        profile_idx += 1
                        log(f"congestion – auto-downgrade to profile {profile_idx} {PROFILES[profile_idx]}")
                        skipped = 0
                await asyncio.sleep(0)
                continue
            skipped = 0

            await ws.send(data)
            
            await asyncio.sleep(max(0, (1/STREAM_FPS) - (time.perf_counter() - t0)))
    except asyncio.CancelledError:
        pass

async def receive_commands(websocket, car: NvidiaRacecar):
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
                log("Received message:", data) # LOGS (Remove for better performance)
                car.steering = data.get('steering', 0.0)
                car.throttle = data.get('throttle', 0.0)
            except json.JSONDecodeError:
                log("Error: Bad JSON from client")
    except asyncio.CancelledError:
        pass

async def keep_alive(ws):
    global profile_idx
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            t0 = time.perf_counter()
            pong_waiter = await ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=PING_TIMEOUT)
            rtt = time.perf_counter() - t0

            rtt_samples.append(rtt)
            if len(rtt_samples) > RTT_WINDOW:
                rtt_samples.pop(0)
            avg = statistics.mean(rtt_samples)

            async with profile_lock:
                if avg > DOWNGRADE_RTT_MIN and profile_idx < len(PROFILES) - 1:
                    profile_idx += 1
                    log(f"RTT {avg*1e3:.0f} ms – downgrade → {PROFILES[profile_idx]}")
                elif avg < UPGRADE_RTT_MAX and profile_idx > 0:
                    profile_idx -= 1
                    log(f"RTT {avg*1e3:.0f} ms – upgrade → {PROFILES[profile_idx]}")
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        log("ping timeout – close")
        await ws.close()
    except asyncio.CancelledError:
        pass

async def run_session(uri: str, car: NvidiaRacecar, stream):
    async with websockets.connect(
        uri,
        ping_interval=None,
        max_queue=None,
        max_size=None
    ) as ws:
        log("WebSocket connected")
        
        sender   = loop.create_task(send_frames(ws, stream))
        receiver = loop.create_task(receive_commands(ws, car))
        pinger   = loop.create_task(keep_alive(ws))
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
    stream = cv2.VideoCapture(gst_pipeline(), cv2.CAP_GSTREAMER)
    if not stream.isOpened():
        raise RuntimeError("camera open failed")

    car = NvidiaRacecar()
    car.steering_gain = -1
    car.steering_offset = 0
    car.steering = 0
    car.throttle_gain = 0.8
    
    backoff = 0.5
    while True:
        try:
            await run_session(MIDDLEWARE_URI, car, stream)
            backoff = 0.5
        except Exception as e:
            log(f"Error: {e} – reconnecting in {backoff}s")
            car.steering = car.throttle = 0.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

if __name__ == "__main__":
    loop.run_until_complete(main())
