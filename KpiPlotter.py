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

        self.fps_count = 0
        self.bps_count = 0


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
            timestamp_ms = start_time + bucket * bucket_ms
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
                expanded_data.append((t, interp_delay))

        # Add the final point
        expanded_data.append((data[-1][0], data[-1][1]))

        # Print or use as needed
        for t, d in expanded_data:
            print(f"{t}: {d:.2f}")

        return expanded_data
    
    def write_csv(self, list, filename):
        with open(filename+'.csv', 'w', newline='') as csvFile:
            writer = csv.writer(csvFile)
            writer.writerow(['timestamp', 'delay'])
            for row in list:
                writer.writerow(row)
        print("wrote to csv " + filename)

    def plot_kpis(self):
        print(f"Avg control delay: {np.mean([v for _, v in self.apply_controls_delays]):.2f} ms")
        print(f"Avg video delay: {np.mean([v for _, v in self.send_video_frame_delays]):.2f} ms")
        print(f"Avg network delay: {np.mean([v for _, v in self.network_delays]):.2f} ms")

        self.write_csv(self.average_by_time_buckets(self.send_video_frame_delays), "send_video_frame_delays")
        self.write_csv(self.average_by_time_buckets(self.apply_controls_delays), "read_controls_delays")
        self.write_csv(self.expand_by_second(self.network_delays), "network_delays")
        self.write_csv(self.fps_sent_over_time, 'fps_sent_over_time')
        self.write_csv(self.average_by_time_buckets(self.MB_sent_over_time), 'MBps_sent_over_time')



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
        times, avgs = self.average_by_time_buckets(self.send_video_frame_delays)
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

        #self.plot_total_delays() NOT FUNCTIONAL

        
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
        print(self.client_video_delays[:20])
        writer.close()
    
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

    # def plot_total_delays(self):
    #     self.load_csv_delays()

    #     def combine_delays(*lists):
    #         # Assume all times are datetime
    #         combined = []
    #         for delay_list in lists:
    #             for ts, delay in delay_list:
    #                 combined.append((ts, delay))
    #         combined.sort()
    #         return combined

    #     total_video = combine_delays(
    #         [(datetime.datetime.fromtimestamp(ts / 1000), d) for ts, d in self.read_video_frame_delays],
    #         [(datetime.datetime.fromtimestamp(ts / 1000), d) for ts, d in self.network_delays],
    #         self.client_video_delays
    #     )

    #     total_control = combine_delays(
    #         [(datetime.datetime.fromtimestamp(ts / 1000), d) for ts, d in self.apply_controls_delays],
    #         [(datetime.datetime.fromtimestamp(ts / 1000), d) for ts, d in self.network_delays],
    #         self.client_control_delays
    #     )

    #     def plot_combined(title, combined, subplot_index):
    #         times, delays = zip(*combined)
    #         plt.subplot(2, 1, subplot_index)
    #         plt.plot(times, delays, label=title)
    #         plt.xlabel("Time")
    #         plt.ylabel("Delay (ms)")
    #         plt.title(title)
    #         plt.grid(True)
    #         plt.legend()
    #         plt.xticks(rotation=45)

    #     plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    #     plt.figure(figsize=(12, 8))
    #     plot_combined("Total Video Delay", total_video, 1)
    #     plot_combined("Total Control Delay", total_control, 2)
    #     plt.tight_layout()
    #     plt.savefig("combined_delays.png")