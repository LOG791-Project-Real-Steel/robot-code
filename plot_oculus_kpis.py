import pandas as pd
import matplotlib.pyplot as plt
import sys

# Load the data
file_path = sys.argv[1]  # Change this to your filename
df = pd.read_csv(file_path, parse_dates=["timestamp"])

# Round timestamps to the nearest 5 seconds
df["time_bucket"] = df["timestamp"].dt.floor("5s")

# Group by 5-second intervals and calculate the average delay
avg_delay = df.groupby("time_bucket")["delay"].mean().reset_index()

# Plot the result
plt.figure(figsize=(12, 6))
plt.plot(avg_delay["time_bucket"], avg_delay["delay"], marker='o', linestyle='-')
plt.title("Average Delay Every 5 Seconds")
plt.xlabel("Time")
plt.ylabel("Average Delay (ms)")
plt.xticks(rotation=45)
plt.grid(True)
plt.tight_layout()
plt.show()
