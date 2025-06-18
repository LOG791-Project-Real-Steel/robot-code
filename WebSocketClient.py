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
            print(f"{jsonCar}")
            car.steering = jsonCar.get('steering', 0.0)
            car.throttle = jsonCar.get('throttle', 0.0)
        except websockets.ConnectionClosed as e:
            print(f"[receive_controls] WebSocket closed: {e.code} - {e.reason}")



async def main():
    stream = cv2.VideoCapture(__gstreamer_pipeline(camera_id=0, flip_method=0), cv2.CAP_GSTREAMER)    
    car = NvidiaRacecar()
    car.steering_gain = -1
    car.steering_offset = 0
    car.throttle_gain = 0.8

    car.steering = 0
    car.throttle = 0
    print("ready to go!")

    async with websockets.connect("ws://74.56.22.147:8765/robot") as websocket:
        await asyncio.gather(
            send_frames(websocket, stream),
            receive_controls(websocket, car)
        )

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())