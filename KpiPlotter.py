import asyncio
import csv
import aiofiles
import pandas as pd
import datetime
import json
import re
import struct
import subprocess
import time
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PING_PONG_PORT = 9003
OCULUS_FILES_PORT = 9004

class KpiPlotter:
    def __init__(self):
        self.apply_controls_delays = []
        self.send_video_frame_delays = []
        self.network_delays = []
        self.fps_sent_over_time = []
        self.MB_sent_over_time = []
        self.wifi_signal_strength_over_time = []  # RSSI
        self.client_video_delays = []
        self.client_control_delays = []
        self.client_received_fps = []

        self.fps_count = 0
        self.bps_count = 0

    async def start_kpi_servers(self):
            ping_pong_server = await asyncio.start_server(
                lambda r, w: self.handle_stats(r, w),
                host='0.0.0.0',
                port=PING_PONG_PORT
            )
            print(f"Ping pong server listening on port {PING_PONG_PORT}")

            oculus_files_server = await asyncio.start_server(
                lambda r, w: self.handle_csv_upload(r, w),
                host='0.0.0.0',
                port=OCULUS_FILES_PORT
            )
            print(f"Oculus files server listening on port {OCULUS_FILES_PORT}")

            return ping_pong_server, oculus_files_server
    
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

    async def handle_csv_upload(self, reader, writer):
        print("CSV upload client connected.")
        for filename in ['frame_delays.csv', 'control_delays.csv', 'fps_over_time.csv']:
            # Read 4-byte filename length
            size_data = await reader.readexactly(4)
            size = struct.unpack('>I', size_data)[0]

            # Read filename
            fname = (await reader.readexactly(size)).decode()

            # Read 4-byte file length
            file_size_data = await reader.readexactly(4)
            file_size = struct.unpack('>I', file_size_data)[0]

            print(f"Receiving file: {fname} ({file_size} bytes)")
            async with aiofiles.open(f"received_{fname}", "wb") as f:
                remaining = file_size
                while remaining:
                    chunk = await reader.read(min(4096, remaining))
                    if not chunk:
                        break
                    await f.write(chunk)
                    remaining -= len(chunk)

        print("CSV upload completed.")
        self.load_csv_delays()
        writer.close()

    def calculate_delay(self, time_read_start, list):
        now = int(time.time() * 1000)
        delay = now - time_read_start
        list.append((now, delay))

    def calculate_local_control_delay(self, time_read_start):
        self.calculate_delay(time_read_start, self.apply_controls_delays)

    def calculate_local_video_delay(self, time_read_start):
        self.calculate_delay(time_read_start, self.send_video_frame_delays)

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

    def total_delay(self, robot, network, oculus, filename):
        robot_delay_per_second = self.average_by_time_buckets(robot)
        network_delay_per_second = self.expand_by_second(network)
        oculus_delay_per_second = self.average_by_time_buckets(oculus)

        robot_dict = dict(robot_delay_per_second)
        network_dict = dict(network_delay_per_second)
        oculus_dict = dict(oculus_delay_per_second)

        common_timestamps = set(robot_dict) & set(network_dict) & set(oculus_dict)

        total_delay = [(datetime.datetime.fromtimestamp(ts/1000), robot_dict[ts] + network_dict[ts] + oculus_dict[ts]) for ts in sorted(common_timestamps)]
        self.write_csv(total_delay, filename)
        return total_delay, [datetime.datetime.fromtimestamp(ts/1000) for ts in sorted(common_timestamps)]

    def plot_kpis(self):
        print(f"Avg control delay: {np.mean([v for _, v in self.apply_controls_delays]):.2f} ms")
        print(f"Avg video delay: {np.mean([v for _, v in self.send_video_frame_delays]):.2f} ms")
        print(f"Avg network delay: {np.mean([v for _, v in self.network_delays]):.2f} ms")

        total_video_delay, common_video_ts = self.total_delay(self.send_video_frame_delays, self.network_delays, self.client_video_delays, "total_video_delay")
        total_control_delay = self.total_delay(self.apply_controls_delays, self.network_delays, self.client_control_delays, "total_control_delay")

        avg_send_video_delay = self.average_by_time_buckets(self.send_video_frame_delays)
        self.write_csv(avg_send_video_delay, "robot_send_video_frame_delays")
        avg_read_controls_delay = self.average_by_time_buckets(self.apply_controls_delays)
        self.write_csv(avg_read_controls_delay, "robot_read_controls_delays")
        avg_network_delay = self.expand_by_second(self.network_delays)
        self.write_csv(avg_network_delay, "network_delays")
        self.write_csv(self.expand_by_second(self.fps_sent_over_time), 'robot_fps_sent_over_time')
        self.write_csv(self.average_by_time_buckets(self.MB_sent_over_time), 'robot_MBps_sent_over_time')

        self.write_csv(self.expand_by_second(self.client_received_fps), "oculus_received_fps")
        self.write_csv(self.average_by_time_buckets(self.client_control_delays), "oculus_control_delays")
        avg_read_video_delay = self.average_by_time_buckets(self.client_video_delays)
        self.write_csv(avg_read_video_delay, "oculus_client_video_delays")

        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        plt.figure(figsize=(12, 12))

        # Total Video delay
        times, avgs = zip(*total_video_delay)
        total_avgs = list(avgs)

        times, avgs = zip(*avg_send_video_delay)
        robot = list(avgs)

        times, avgs = zip(*avg_network_delay)
        net = list(avgs)

        times, avgs = zip(*avg_read_video_delay)
        oculus = list(avgs)

        plt.subplot(2, 1, 1)
        plt.plot(common_video_ts, total_avgs, label="Total video delays")
        plt.plot(common_video_ts, robot, label="Capturing and sending video delays")
        plt.plot(common_video_ts, net, label="Network delays")
        plt.plot(common_video_ts, oculus, label="Reading and displaying video delays")
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("Delay (ms)")
        plt.title("Video Delay Over Time (avg/1s)")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        # Total Control delay
        times, avgs = zip(*total_control_delay)
        times = list(times)
        avgs = list(avgs)
        plt.subplot(2, 1, 2)
        plt.plot(times, avgs, label="Total control delays")
        plt.xlabel("Timestamp (ms)")
        plt.ylabel("Delay (ms)")
        plt.title("Control Delay Over Time (avg/1s)")
        plt.grid(True)
        plt.legend()
        plt.xticks(rotation=45)

        plt.tight_layout()
        plt.savefig("robot_delays_over_time.png")
    
    def load_csv_delays(self):
        def load_csv(name):
            try:
                if isinstance(name, bytes):
                    name = name.decode()
                df = pd.read_csv(f"received_{name}")
                df["timestamp"] = df["timestamp"]
                return list(zip(df["timestamp"], df["delay" if "delay" in df.columns else "fps"]))
            except Exception as e:
                print(f"Error loading {name}: {e}")
                return []

        self.client_video_delays = load_csv("frame_delays.csv")
        self.client_control_delays = load_csv("control_delays.csv")
        self.client_received_fps = load_csv("fps_over_time.csv")

    
    def average_by_time_buckets(self, data, bucket_ms=1000):
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
        for bucket, values in sorted(buckets.items()):
            avg = sum(values) / len(values)
            timestamp_ms = ((start_time + bucket * bucket_ms) // 1000) * 1000
            times.append((timestamp_ms, avg))

        return times
    
    def expand_by_second(self, data):
        # Convert to seconds for easier interpolation
        data = [(int(ts / 1000), delay) for ts, delay in data]
        data.sort()  # Ensure sorted by timestamp

        # Interpolated result
        expanded_data = []

        for i in range(len(data) - 1):
            t_start, d_start = data[i]
            t_end, d_end = data[i + 1]

            for t in range(t_start, t_end):
                # Linear interpolation
                interp_delay = d_start + (d_end - d_start) * (t - t_start) / (t_end - t_start)
                expanded_data.append((t * 1000, interp_delay))

        # Add the final point
        expanded_data.append((data[-1][0]*1000, data[-1][1]))

        return expanded_data
    
    def write_csv(self, list, filename):
        with open(filename+'.csv', 'w', newline='') as csvFile:
            writer = csv.writer(csvFile)
            writer.writerow(['timestamp', 'delay'])
            for row in list:
                writer.writerow(row)
        print("wrote to csv " + filename)