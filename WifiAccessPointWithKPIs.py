#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import sys
import asyncio
import json
import time
import cv2
import numpy as np
import struct
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
from jetracer.nvidia_racecar import NvidiaRacecar

VIDEO_AND_CONTROL_PORT = 9002
PING_PONG_PORT = 9003

width = 1280
height = 720
fps = 30


apply_controls_delays = []
read_video_frame_delays = []
network_delays = []

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
    else:
        print("Frame read failed.")
    
    now = int(time.time() * 1000)
    read_video_frame_delay = now - time_read_start
    read_video_frame_delays.append((now, read_video_frame_delay))


def handle_controls(car, data, buffer, time_read_start):
    global apply_controls_delays

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
    return buffer


async def handle_control_and_video(reader, writer, car, stream):
    print("Video/Control client connected")

    buffer = ""
    while True:
        try:
            # Read control data and send to robot car
            time_read_start = int(time.time() * 1000)
            data = await reader.read(1024)
            if not data:
                break
            buffer = handle_controls(car, data, buffer, time_read_start)

            # Capture frame from camera and send to client
            await handle_video(stream, writer)
            
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            print("Video and control connection closed.")
            break


async def ping_loop(writer):
        while True:
            try:
                print("sending ping")
                timestamp = int(time.time() * 1000)
                ping_msg = json.dumps({"type": "ping", "timestamp": timestamp}) + '\n'
                writer.write(ping_msg.encode('utf-8'))
                await writer.drain()
                await asyncio.sleep(5)  # Ping every second
            except (ConnectionResetError, BrokenPipeError):
                print("Ping connection lost.")
                break

async def handle_ping(reader, writer):
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

                        print(f"[Ping] RTT to Oculus: {rtt} ms")
                        print(f"[Ping] Network Exponential Moving Average: {network_delay_ema} ms")
                except json.JSONDecodeError:
                    print("Invalid JSON:", line)            
        except asyncio.IncompleteReadError:
            print("Ping connection closed.")
            break

    return ping

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

    car = NvidiaRacecar()
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

def average_by_time_buckets(data, bucket_ms=5000):
    if not data:
        return [], []
    
    data.sort()  # Sort by timestamp
    start_time = data[0][0]
    buckets = {}
    
    for timestamp, value in data:
        bucket = (timestamp - start_time) // bucket_ms
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(value)

    times = []
    averages = []
    for bucket, values in sorted(buckets.items()):
        avg = sum(values) / len(values)
        averages.append(avg)
        timestamp_ms = start_time + bucket * bucket_ms
        times.append(datetime.fromtimestamp(timestamp_ms / 1000))

    return times, averages

def plot_kpis():
    print(f"Avg control delay: {np.mean([v for _, v in apply_controls_delays]):.2f} ms")
    print(f"Avg video delay: {np.mean([v for _, v in read_video_frame_delays]):.2f} ms")
    print(f"Avg network delay: {np.mean([v for _, v in network_delays]):.2f} ms")

    plt.figure(figsize=(12, 6))

    # Control delays
    times, avgs = average_by_time_buckets(apply_controls_delays)
    plt.subplot(1, 3, 1)
    plt.plot(times, avgs, label="Control Delays (avg/5s)")
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Control Delay Over Time")
    plt.grid(True)
    plt.legend()

    # Video delays
    times, avgs = average_by_time_buckets(read_video_frame_delays)
    plt.subplot(1, 3, 2)
    plt.plot(times, avgs, label="Video Delays (avg/5s)", color='orange')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Video Delay Over Time")
    plt.grid(True)
    plt.legend()

    # Network delays
    times, avgs = average_by_time_buckets(network_delays)
    plt.subplot(1, 3, 3)
    plt.plot(times, avgs, label="Network Delays (avg/5s)", color='green')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Network Delay Over Time")
    plt.grid(True)
    plt.legend()

    plt.xticks(rotation=45)

    plt.tight_layout()
    plt.savefig("robot_delays_over_time.png")


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt as e:
        plot_kpis()

        print(f"Exiting : {e}")
        exit
