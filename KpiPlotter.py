import asyncio
import datetime
import json
import re
import subprocess
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PING_PONG_PORT = 9003

class KpiPlotter:
    def __init__(self):
        self.apply_controls_delays = []
        self.read_video_frame_delays = []
        self.network_delays = []
        self.fps_sent_over_time = []
        self.MB_sent_over_time = []
        self.wifi_signal_strength_over_time = []  # RSSI

        self.fps_count = 0
        self.bps_count = 0


    def calculate_delay(self, time_read_start, list):
        now = int(time.time() * 1000)
        delay = now - time_read_start
        list.append((now, delay))

    def calculate_local_control_delay(self, time_read_start):
        self.calculate_delay(time_read_start, self.apply_controls_delays)

    def calculate_local_video_delay(self, time_read_start):
        self.calculate_delay(time_read_start, self.read_video_frame_delays)

    def calculate_network_delay(self, time_read_start):
        now = int(time.time() * 1000)
        rtt = now - int(time_read_start)
        delay = rtt / 2
        self.network_delays.append((now, delay))


    async def collect_fps(self):
        while True:
            await asyncio.sleep(1)
            self.fps_sent_over_time.append((int(time.time() * 1000), self.fps_count))
            self.fps_count = 0

    async def collect_bps(self):
        while True:
            await asyncio.sleep(1)
            self.MB_sent_over_time.append((int(time.time() * 1000), self.bps_count))
            self.bps_count = 0


    def get_wifi_signal_strength(self, interface="wlan0"):
        try:
            output = subprocess.check_output(["iwconfig", interface], stderr=subprocess.DEVNULL).decode()
            match = re.search(r"Signal level=(-?\d+) dBm", output)
            if match:
                return int(match.group(1))
        except Exception:
            return None
    
    async def collect_network_signal(self):
        while True:
            await asyncio.sleep(5)
            signal_dbm = self.get_wifi_signal_strength()
            if signal_dbm is not None:
                now = int(time.time() * 1000)
                self.wifi_signal_strength_over_time.append((now, signal_dbm))

    def average_by_time_buckets(self, data, bucket_ms=5000):
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
            times.append(datetime.datetime.fromtimestamp(timestamp_ms / 1000))

        return times, averages
    
    async def send_ping(self, writer):
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

    async def read_pong(self, reader):
        global stats

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
                            self.calculate_network_delay(msg["timestamp"])
                    except json.JSONDecodeError:
                        print("Invalid JSON:", line)            
            except asyncio.IncompleteReadError:
                print("Ping connection closed.")
                break

    async def handle_stats(self, reader, writer):
        print('Ping Pong client connected')
        try:
            await asyncio.gather(
                self.collect_fps(),
                self.collect_bps(),
                self.collect_network_signal(),
                self.send_ping(writer),
                self.read_pong(reader)
            )
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            print("Ping pong connection closed.")
    
    async def start_kpi_server(self):
        ping_pong_server = await asyncio.start_server(
            lambda r, w: self.handle_stats(r, w),
            host='0.0.0.0',
            port=PING_PONG_PORT
        )
        print(f"Ping pong server listening on port {PING_PONG_PORT}")

        return ping_pong_server

    def plot_kpis(self):
        print(f"Avg control delay: {np.mean([v for _, v in self.apply_controls_delays]):.2f} ms")
        print(f"Avg video delay: {np.mean([v for _, v in self.read_video_frame_delays]):.2f} ms")
        print(f"Avg network delay: {np.mean([v for _, v in self.network_delays]):.2f} ms")

        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        plt.figure(figsize=(12, 12))

        # Control delays
        times, avgs = self.average_by_time_buckets(self.apply_controls_delays)
        plt.subplot(6, 1, 1)
        plt.plot(times, avgs, label="Control Delays (avg/5s)")
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("Delay (ms)")
        plt.title("Control Delay Over Time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # Video delays
        times, avgs = self.average_by_time_buckets(self.read_video_frame_delays)
        plt.subplot(6, 1, 2)
        plt.plot(times, avgs, label="Video Delays (avg/5s)", color='orange')
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("Delay (ms)")
        plt.title("Video Delay Over Time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # Network delays
        times, avgs = self.average_by_time_buckets(self.network_delays)
        plt.subplot(6, 1, 3)
        plt.plot(times, avgs, label="Network Delays (avg/5s)", color='green')
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("Delay (ms)")
        plt.title("Network Delay Over Time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # FPS
        times, avgs = self.average_by_time_buckets(self.fps_sent_over_time)
        plt.subplot(6, 1, 4)
        plt.plot(times, avgs, label="FPS (avg/5s)", color='red')
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("FPS")
        plt.title("FPS sent Over Time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # MegaBytes per second
        times, avgs = self.average_by_time_buckets(self.MB_sent_over_time)
        plt.subplot(6, 1, 5)
        plt.plot(times, avgs, label="MB sent (avg/5s)", color='blue')
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("MB sent per second")
        plt.title("MB per second sent Over Time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # Network Signal quality (RSSI) over time
        times, avgs = self.average_by_time_buckets(self.wifi_signal_strength_over_time)
        plt.subplot(6, 1, 6)
        plt.plot(times, avgs, label="RSSI (avg/5s)", color='orange')
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("RSSI")
        plt.title("Network Signal quality (RSSI) over time")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        plt.tight_layout()
        plt.savefig("robot_delays_over_time.png")