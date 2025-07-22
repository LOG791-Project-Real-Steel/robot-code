#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import asyncio
import json
import cv2
import numpy as np
import struct
from jetracer.nvidia_racecar import NvidiaRacecar

CONTROL_PORT = 9002
VIDEO_PORT = 9001

def __gstreamer_pipeline(
        camera_id=0,
        capture_width=1280,
        capture_height=720,
        display_width=1280,
        display_height=720,
        framerate=60,
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

def handle_controls(car, data, buffer):
    buffer += data.decode('utf-8')
    while '\n' in buffer:
        line, buffer = buffer.split('\n', 1)
        try:
            msg = json.loads(line)
            car.steering = float(msg.get("steering", 0.0))
            car.throttle = float(msg.get("throttle", 0.0))
        except json.JSONDecodeError:
            print("Invalid JSON:", line)


async def handle(reader, writer, car, stream):
    print("Client connected")
    buffer = ""

    while True:
        try:
            # Read control data and send to robot car
            data = await reader.read(1024)
            if not data:
                print("Client connection closed.")
                break
            handle_controls(car, data, buffer)

            # Capture frame from camera and send to client
            try:
                await handle_video(stream, writer)
            except (ConnectionResetError, BrokenPipeError):
                print("Video client disconnected")
                break
            
        except asyncio.IncompleteReadError:
            print("Control reader closed.")
            break

async def server(car, stream):
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, car, stream),
        host='0.0.0.0',
        port=CONTROL_PORT
    )
    print(f"Control server listening on port {CONTROL_PORT}")
    # Just return the server, don't try to serve forever
    return server

async def main():
    
    width = 1280
    height = 720
    fps = 30

    n = len(sys.argv)
    print("\nArguments passed:", end = " ")
    for i in range(1, n):
        arg = sys.argv[i]
        print(arg, end = " ")
        try:
            if arg.startswith("res"):
                res = arg[3:].split('=')[1].split('x')
                width = res[0]
                height = res[1]
            if arg.startswith("fps"):
                fps = arg[3:].split('=')[1]
            
            if not width.isnumeric() or  not height.isnumeric() or not fps.isnumeric():
                raise ValueError()
        except Exception as e:
            print(f"args error : {e}\nCorrect way to pass arguments : script.py res=1920x1080 fps=30")
            exit

    # car = NvidiaRacecar()
    # car.steering = 0.0
    # car.throttle = 0.0

    # stream = cv2.VideoCapture(__gstreamer_pipeline(), cv2.CAP_GSTREAMER)
    # if not stream.isOpened():
    #     print("Failed to open camera.")
    #     return

    # print("Robot is ready.")
    
    # # Start both servers
    # await server(car, stream)

    # # Keep the main coroutine alive
    # await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
