import datetime
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

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
        times.append(datetime.datetime.fromtimestamp(timestamp_ms / 1000))

    return times, averages

def plot_kpis(apply_controls_delays, read_video_frame_delays, network_delays, fps_sent_over_time, MB_sent_over_time, wifi_signal_strength_over_time):
    print(f"Avg control delay: {np.mean([v for _, v in apply_controls_delays]):.2f} ms")
    print(f"Avg video delay: {np.mean([v for _, v in read_video_frame_delays]):.2f} ms")
    print(f"Avg network delay: {np.mean([v for _, v in network_delays]):.2f} ms")

    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    plt.figure(figsize=(12, 12))

    # Control delays
    times, avgs = average_by_time_buckets(apply_controls_delays)
    plt.subplot(6, 1, 1)
    plt.plot(times, avgs, label="Control Delays (avg/5s)")
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Control Delay Over Time")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)

    # Video delays
    times, avgs = average_by_time_buckets(read_video_frame_delays)
    plt.subplot(6, 1, 2)
    plt.plot(times, avgs, label="Video Delays (avg/5s)", color='orange')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Video Delay Over Time")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)

    # Network delays
    times, avgs = average_by_time_buckets(network_delays)
    plt.subplot(6, 1, 3)
    plt.plot(times, avgs, label="Network Delays (avg/5s)", color='green')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("Delay (ms)")
    plt.title("Network Delay Over Time")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)

    # FPS
    times, avgs = average_by_time_buckets(fps_sent_over_time)
    plt.subplot(6, 1, 4)
    plt.plot(times, avgs, label="FPS (avg/5s)", color='red')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("FPS")
    plt.title("FPS sent Over Time")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)

    # MegaBytes per second
    times, avgs = average_by_time_buckets(MB_sent_over_time)
    plt.subplot(6, 1, 5)
    plt.plot(times, avgs, label="MB sent (avg/5s)", color='blue')
    plt.xlabel("Timestamp (ms)")
    plt.ylabel("MB sent per second")
    plt.title("MB per second sent Over Time")
    plt.grid(True)
    plt.legend()
    plt.xticks(rotation=45)

    # Network Signal quality (RSSI) over time
    times, avgs = average_by_time_buckets(wifi_signal_strength_over_time)
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