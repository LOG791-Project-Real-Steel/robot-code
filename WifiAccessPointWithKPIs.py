#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

async def handle_control(reader, car):
    print("hello control?")
    buffer = ""
    while True:
        try:
            print("Trying to read data")
            data = await reader.read(1024)
            if not data:
                print("Control connection closed.")
                break

            buffer += data.decode('utf-8')
            print(f"buffer{str(buffer)}")
            while '\n' in buffer:
                print("in while loop")
                line, buffer = buffer.split('\n', 1)
                try:
                    msg = json.loads(line)
                    print(f"msg {str(msg)}")
                    car.steering = float(msg.get("steering", 0.0))
                    car.throttle = float(msg.get("throttle", 0.0))
                    print(f"Steering: {car.steering:.2f}, Throttle: {car.throttle:.2f}")
                except json.JSONDecodeError:
                    print("Invalid JSON:", line)
        except asyncio.IncompleteReadError:
            print("Control reader closed.")
            break

async def handle_video(writer, stream):
    print("hello video?")
    while True:
        await asyncio.sleep(0.017)  # ~60 FPS
        if not stream.isOpened():
            print("Camera error.")
            continue
        ret, frame = stream.read()
        if ret:
            _, jpeg = cv2.imencode('.jpg', frame)
            data = jpeg.tobytes()
            size = struct.pack('>I', len(data))  # 4-byte size prefix
            try:
                writer.write(size + data)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                print("Video client disconnected")
                break
        else:
            print("Frame read failed.")

async def control_server(car):
    server = await asyncio.start_server(
        lambda r, w: handle_control(r, car),
        host='0.0.0.0',
        port=CONTROL_PORT
    )
    print(f"Control server listening on port {CONTROL_PORT}")
    # Just return the server, don't try to serve forever
    return server

async def video_server(stream):
    async def handler(reader, writer):
        print("Video client connected.")
        await handle_video(writer, stream)
        writer.close()

    server = await asyncio.start_server(handler, host='0.0.0.0', port=VIDEO_PORT)
    print(f"Video server listening on port {VIDEO_PORT}")
    return server


async def main():
    car = NvidiaRacecar()
    car.steering = 0.0
    car.throttle = 0.0

    stream = cv2.VideoCapture(__gstreamer_pipeline(), cv2.CAP_GSTREAMER)
    if not stream.isOpened():
        print("Failed to open camera.")
        return

    print("Robot is ready.")
    
    # Start both servers
    control = await control_server(car)
    video = await video_server(stream)

    # Keep the main coroutine alive
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
