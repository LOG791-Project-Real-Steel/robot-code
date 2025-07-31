#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import sys
import asyncio
import json
import time
from DummyRacecar import DummyRacecar
from KpiPlotter import plot_kpis
import cv2
import numpy as np
import struct
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import subprocess
import re
from jetracer.nvidia_racecar import NvidiaRacecar

VIDEO_AND_CONTROL_PORT = 9002
PING_PONG_PORT = 9003

width = 1280
height = 720
fps = 60


apply_controls_delays = []
read_video_frame_delays = []
network_delays = []
fps_sent_over_time = []
MB_sent_over_time = []
wifi_signal_strength_over_time = [] # RSSI

fps_count = 0
bps_count = 0


network_delay_ema = None
ema_alpha = 0.1

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
        "nvarguscamerasrc sensor-id=%d ! "
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
    global read_video_frame_delays
    global fps_count
    global fps
    global bps_count

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
            fps_count += 1
            bps_count += (len(size) + len(data)) / 1000000
        else:
            print("Frame read failed.")
        
        now = int(time.time() * 1000)
        read_video_frame_delay = now - time_read_start
        read_video_frame_delays.append((now, read_video_frame_delay))

        await asyncio.sleep(1 / (fps * 2))


async def handle_controls(reader, car):
    global apply_controls_delays

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

                now = int(time.time() * 1000)
                apply_controls_delay = now - time_read_start
                apply_controls_delays.append((now, apply_controls_delay))
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


async def ping_loop(writer):
        while True:
            try:
                timestamp = int(time.time() * 1000)
                ping_msg = json.dumps({"type": "ping", "timestamp": timestamp}) + '\n'
                writer.write(ping_msg.encode('utf-8'))
                await writer.drain()
                await asyncio.sleep(5)
            except (ConnectionResetError, BrokenPipeError):
                print("Ping connection lost.")
                break

def get_wifi_signal_strength(interface="wlan0"):
    try:
        output = subprocess.check_output(["iwconfig", interface], stderr=subprocess.DEVNULL).decode()
        match = re.search(r"Signal level=(-?\d+) dBm", output)
        if match:
            return int(match.group(1))
    except Exception:
        return None
        
async def collect_fps():
    global fps_sent_over_time
    global fps_count

    while True:
        await asyncio.sleep(1)
        fps_sent_over_time.append((int(time.time() * 1000), fps_count))
        fps_count = 0

async def collect_bps():
    global MB_sent_over_time
    global bps_count

    while True:
        await asyncio.sleep(1)
        MB_sent_over_time.append((int(time.time() * 1000), bps_count))
        bps_count = 0

async def collect_network_signal():
    while True:
        await asyncio.sleep(5)
        signal_dbm = get_wifi_signal_strength()
        if signal_dbm is not None:
            now = int(time.time() * 1000)
            wifi_signal_strength_over_time.append((now, signal_dbm))

async def handle_ping(reader, writer):
    fps_collect = asyncio.ensure_future(collect_fps()) # Start collecting fps count in the background
    bps_collect = asyncio.ensure_future(collect_bps()) # Start collecting megabytes per second count in the background
    asyncio.ensure_future(collect_network_signal()) 
    ping = asyncio.ensure_future(ping_loop(writer))  # Start pinging in background

    global network_delays
    global network_delay_ema
    global ema_alpha

    buffer = ""
    while True:
        try:
            data = await reader.read(1024)
            if not data:
                break
            buffer += data.decode('utf-8')
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                try:
                    msg = json.loads(line)
                    if msg["type"] == "pong":
                        now = int(time.time() * 1000)
                        rtt = now - int(msg["timestamp"])
                        
                        delay = rtt / 2
                        network_delays.append((now, delay))

                        if network_delay_ema is None:
                            network_delay_ema = delay
                        else:
                            network_delay_ema = ema_alpha * delay + (1 - ema_alpha) * network_delay_ema
                except json.JSONDecodeError:
                    print("Invalid JSON:", line)            
        except asyncio.IncompleteReadError:
            print("Ping connection closed.")
            break

    return ping, fps_collect, bps_collect

async def server(car, stream):
    control_and_video_server = await asyncio.start_server(
        lambda r, w: handle_control_and_video(r, w, car, stream),
        host='0.0.0.0',
        port=VIDEO_AND_CONTROL_PORT
    )
    print(f"Control and video server listening on port {VIDEO_AND_CONTROL_PORT}")

    ping_pong_server = await asyncio.start_server(
        lambda r, w: handle_ping(r, w),
        host='0.0.0.0',
        port=PING_PONG_PORT
    )
    print(f"Ping pong server listening on port {PING_PONG_PORT}")

    # Just return the server, don't try to serve forever
    return control_and_video_server, ping_pong_server

def read_args():
    global width
    global height
    global fps

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
    except Exception as e:
        print(f"\nargs error : {e}\nCorrect way to pass arguments : script.py res=1920x1080 fps=30")
        exit

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
    await server(car, stream)

    # Keep the main coroutine alive
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt as e:
        plot_kpis(
            apply_controls_delays,
            read_video_frame_delays,
            network_delays,
            fps_sent_over_time,
            MB_sent_over_time,
            wifi_signal_strength_over_time
        )

        print(f"Exiting : {e}")
        exit
