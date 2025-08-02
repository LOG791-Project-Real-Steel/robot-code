#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

VIDEO_AND_CONTROL_PORT = 9002

width = 1280
height = 720
fps = 60
kpi = False

stats = KpiPlotter()

def __gstreamer_pipeline(
        camera_id=0,
        capture_width=width,
        capture_height=height,
        display_width=width,
        display_height=height,
        framerate=fps,
        flip_method=0,
    ):
    return (
        "nvarguscamerasrc sensor-id=%d sensor-mode=4! "
        "video/x-raw(memory:NVMM), width=(int)%d, height=(int)%d, "
        "format=(string)NV12, framerate=(fraction)%d/1 ! "
        "nvvidconv flip-method=%d ! "
        "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
        "videoconvert ! video/x-raw, format=(string)BGR ! appsink max-buffers=1 drop=True"
        % (
            camera_id, capture_width, capture_height,
            framerate, flip_method, display_width, display_height
        )
    )

async def handle_video(stream, writer):
    global stats
    global fps

    while True:
        time_read_start = int(time.time() * 1000)

        if not stream.isOpened():
            print("Camera error.")
            return
        
        ret, frame = stream.read()
        if ret:
            _, jpeg = cv2.imencode('.jpg', frame)
            data = jpeg.tobytes()

            size = struct.pack('>I', len(data))  # 4-byte size prefix
            writer.write(size + data)
            await writer.drain()
            
            if kpi:
                stats.fps_count += 1
                stats.bps_count += (len(size) + len(data)) / 1000000
        else:
            print("Frame read failed.")

        if kpi:
            stats.calculate_local_video_delay(time_read_start)

        await asyncio.sleep(1 / (fps))


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


async def handle_control_and_video(reader, writer, car, stream):
    print("Video/Control client connected")

    try:
        await asyncio.gather(
            handle_controls(reader, car),
            handle_video(stream, writer)
        )
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        print("Video and control connection closed.")

async def servers(car, stream):
    control_and_video_server = await asyncio.start_server(
        lambda r, w: handle_control_and_video(r, w, car, stream),
        host='0.0.0.0',
        port=VIDEO_AND_CONTROL_PORT
    )
    print(f"Control and video server listening on port {VIDEO_AND_CONTROL_PORT}")

    if kpi:
        await stats.start_kpi_servers()

    return control_and_video_server

def read_args():
    global width
    global height
    global fps
    global kpi

    n = len(sys.argv)
    print("\nArguments passed:", end = " ")
    try:
        for i in range(1, n):
            arg = sys.argv[i]
            print(arg, end = " ")
            if arg.startswith("res"):
                res = arg[3:].split('=')[1].split('x')
                width = int(res[0])
                height = int(res[1])
            if arg.startswith("fps"):
                fps = int(arg[3:].split('=')[1])
            if arg.startswith("kpi"):
                kpi = True
    except Exception as e:
        print(f"\nargs error : {e}\nCorrect way to pass arguments : script.py res=1920x1080 fps=30")
        exit

    if fps >= 60:
        fps = 1000
    else:
        fps += 15

async def main():
    read_args()

    try:
        car = NvidiaRacecar()
    except Exception as e:
        print(f"Warning: Failed to initialize NvidiaRacecar due to I2C error: {e}")
        print("Using DummyRacecar instead.")
        car = DummyRacecar()

    car.steering = 0.0
    car.throttle = 0.0

    stream = cv2.VideoCapture(
        __gstreamer_pipeline(),
        cv2.CAP_GSTREAMER
    )
    if not stream.isOpened():
        print("Failed to open camera.")
        return

    print("Robot is ready.")

    # Start all servers : Sending video, receiving controls and ping pong game
    await servers(car, stream)

    # Keep the main coroutine alive
    await asyncio.Event().wait()


if __name__ == "__main__":
    stats = KpiPlotter()
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt as e:
        if kpi:
            stats.plot_kpis()

        print(f"Exiting : {e}")
        exit()
