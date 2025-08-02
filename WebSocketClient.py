#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import asyncio
import websockets
from datetime import datetime
from jetracer.nvidia_racecar import NvidiaRacecar
import cv2
import numpy as np

MIDDLEWARE_URI = "ws://74.56.22.147:8765/robot"
PING_INTERVAL = 5          # s – send ping this often
PING_TIMEOUT  = 5          # s – drop connection if no pong
MAX_BACKOFF   = 30         # s – cap for exponential back-off

def log(msg, data=None):
    if data is None:
        print(f"{datetime.now().isoformat()}  {msg}")
    else:
        print(f"{datetime.now().isoformat()}  {msg} - {data}")

def gst_pipeline(
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
            "video/x-raw(memory:NVMM), "
            "width=(int)%d, height=(int)%d, "
            "format=(string)NV12, framerate=(fraction)%d/1 ! "
            "nvvidconv flip-method=%d ! "
            "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
            "videoconvert ! "
            "video/x-raw, format=YUY2 ! jpegenc ! appsink max-buffers=1 drop=True"
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
   
loop = asyncio.get_event_loop()
create_task = loop.create_task

async def send_frames(websocket, stream):
    try:
        while True:
            await asyncio.sleep(0.017)
            ret, frame = stream.read()
            if not ret:
                log("camera read failed")
                continue
            await websocket.send(frame.tobytes())
    except asyncio.CancelledError:
        pass

async def receive_commands(websocket, car: NvidiaRacecar):
    try:
        async for msg in websocket:
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                log("Bad JSON")
                continue
            log("Received message:", data) # LOGS
            car.steering = data.get('steering', 0.0)
            car.throttle = data.get('throttle', 0.0)
    except asyncio.CancelledError:
        pass

async def run_session(uri: str, car: NvidiaRacecar, stream):
    async with websockets.connect(
        uri,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
    ) as websocket:
        log("websocket connected")
        
        sender = create_task(send_frames(websocket, stream))
        receiver = create_task(receive_commands(websocket, car))
        watcher  = create_task(websocket.wait_closed())
        
        done, pending = await asyncio.wait(
            [sender, receiver, watcher],
            return_when=asyncio.FIRST_EXCEPTION
        )
        for task in pending:
            task.cancel()
    
    car.steering = car.throttle = 0.0
    log("websocket closed")
    
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
            log(f"Error: {e} – reconnecting in {backoff:.1f}s")
            car.steering = car.throttle = 0.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

if __name__ == "__main__":
    loop.run_until_complete(main())
