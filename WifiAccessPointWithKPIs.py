#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import asyncio
import json
import time
import cv2
import numpy as np
import struct
from jetracer.nvidia_racecar import NvidiaRacecar

VIDEO_AND_CONTROL_PORT = 9002
PING_PONG_PORT = 9003

width = 1280
height = 720
fps = 30


apply_controls_delays = []

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
            apply_controls_delays.append(apply_controls_delay)
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

    ping_count = 0
    network_delay_total = 0
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
                        ping_count += 1
                        network_delay_total += rtt/2
                        print(f"[Ping] RTT to Oculus: {rtt} ms")
                        print(f"[Ping] Network delay avg: {network_delay_total/ping_count} ms")
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

if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt as e:
        print(f"Exiting : {e}")

        print(f"Average time to apply controls after receiving : {sum(apply_controls_delays)/len(apply_controls_delays)} ms")

        exit
