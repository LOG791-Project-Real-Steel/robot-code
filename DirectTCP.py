#!/usr/bin/env python
# -*- coding: utf-8 -*-

import signal
import sys
import asyncio
import json
import time
from DummyRacecar import DummyRacecar
from KpiPlotter import KpiPlotter
import cv2
import struct
import matplotlib
matplotlib.use('Agg')
from jetracer.nvidia_racecar import NvidiaRacecar

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
import argparse

VIDEO_AND_CONTROL_PORT = 9002

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
TARGET_RESOLUTION = args.res
TARGET_FPS        = args.fps

width = 1280
height = 720
fps = 60
kpi = args.kpi

stats = KpiPlotter()
loop = asyncio.get_event_loop()

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

async def handle_video(cam: Camera, writer):
    global stats

    while True:
        t0 = time.perf_counter()
        time_read_start = int(time.time() * 1000)
        
        data = cam.grab()
        if data:
            size = struct.pack('>I', len(data))  # 4-byte size prefix
            writer.write(size + data)
            await writer.drain()
            
            if kpi:
                stats.fps_count += 1
                stats.bps_count += (len(size) + len(data)) / 1000000
                stats.calculate_local_video_delay(time_read_start)
        else:
            print("Frame read failed.")

        await asyncio.sleep(max(0.0, (1/TARGET_FPS) - (time.perf_counter() - t0)))

async def handle_controls(reader, car):
    global stats

    buffer = ""
    while True:
        # Read control data and send to robot car
        time_read_start = int(time.time() * 1000)
        data = await reader.read(1024)
        if not data:
            break

        buffer += data.decode('utf-8')
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            try:
                msg = json.loads(line)
                car.steering = float(msg.get("steering", 0.0))
                car.throttle = float(msg.get("throttle", 0.0))

                if kpi:
                    stats.calculate_local_control_delay(time_read_start)
            except json.JSONDecodeError:
                print("Invalid JSON:", line)


async def handle_control_and_video(reader, writer, car, cam: Camera):
    print("Video/Control client connected")

    try:
        await asyncio.gather(
            handle_controls(reader, car),
            handle_video(cam, writer)
        )
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        print("Video and control connection closed.")

async def start(cam: Camera):
    try:
        car = NvidiaRacecar()
    except Exception as e:
        print(f"Warning: Failed to initialize NvidiaRacecar due to I2C error: {e}")
        print("Using DummyRacecar instead.")
        car = DummyRacecar()
    car.steering = 0.0
    car.throttle = 0.0
    print("Robot is ready.")

    control_and_video_server = await asyncio.start_server(
        lambda r, w: handle_control_and_video(r, w, car, cam),
        host='0.0.0.0',
        port=VIDEO_AND_CONTROL_PORT
    )
    print(f"Control and video server listening on port {VIDEO_AND_CONTROL_PORT}")

    if kpi:
        await stats.start_kpi_servers()

    return control_and_video_server

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
    
    # Start all servers : Sending video, receiving controls and ping pong game
    await start(cam)

    # Keep the main coroutine alive
    await asyncio.Event().wait()


if __name__ == "__main__":
    Gst.init(None)
    stats = KpiPlotter()
    try:
        loop.run_until_complete(main())
    finally:
        if kpi:
            stats.plot_kpis()

        Gst.deinit()
        loop.close()
        exit()
