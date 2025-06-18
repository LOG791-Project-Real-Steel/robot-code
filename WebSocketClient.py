#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import asyncio
import websockets
from jetracer.nvidia_racecar import NvidiaRacecar
import cv2
import numpy as np

def __gstreamer_pipeline(
        camera_id,
        capture_width=1280,
        capture_height=720,
        display_width=1280,
        display_height=720,
        framerate=30,
        flip_method=0,
    ):
    return (
            "nvarguscamerasrc sensor-id=%d ! "
            "video/x-raw(memory:NVMM), "
            "width=(int)%d, height=(int)%d, "
            "format=(string)NV12, framerate=(fraction)%d/1 ! "
            "nvvidconv flip-method=%d ! "
            "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
            "videoconvert ! "
            "video/x-raw, format=(string)BGR ! appsink max-buffers=1 drop=True"
            % (
                    camera_id,
                    capture_width,
                    capture_height,
                    framerate,
                    flip_method,
                    display_width,
                    display_height,
            )
    )
   

async def send_frames(websocket, stream):
    while True:
        if not stream.isOpened():
            print("Error: Could not open camera.")
        else:
            ret, frame = stream.read()
            if ret:
                frame = np.ascontiguousarray(frame, dtype=np.uint8)
                _, encoded_image = cv2.imencode('.jpg', frame)
                await websocket.send(encoded_image.tobytes())
            else:
                print("Error: Could not read frame.")
        await asyncio.sleep(0.03)

async def receive_controls(websocket, car):
    while True:
        try:
            jsonCar = json.loads(await websocket.recv())
            car.steering = jsonCar.get('steering', 0.0)
            car.throttle = jsonCar.get('throttle', 0.0)
        except websockets.ConnectionClosed as e:
            print(f"[receive_controls] WebSocket closed: {e.code} - {e.reason}")



async def connect_loop():
    stream = cv2.VideoCapture(__gstreamer_pipeline(camera_id=0, flip_method=0), cv2.CAP_GSTREAMER)    
    car = NvidiaRacecar()
    car.steering_gain = -1
    car.steering_offset = 0
    car.throttle_gain = 0.8

    car.steering = 0
    car.throttle = 0
    print("ready to go!")

    while True:
        print("Attempting to connect to WebSocket server...")
        try:
            async with websockets.connect("ws://74.56.22.147:8765/robot") as websocket:
                print("WebSocket connected.")
                await asyncio.gather(
                    send_frames(websocket, stream),
                    receive_controls(websocket, car)
                )
        except (websockets.ConnectionClosedError, OSError) as e:
            print(f"[main] Connection failed: {e}")
        except Exception as e:
            print(f"[main] Unexpected error: {e}")
        
        print("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(connect_loop())
    except KeyboardInterrupt:
        print("Script stopped by user.")