import pandas as pd
import matplotlib.pyplot as plt
import sys

# Load the data
file_path = sys.argv[1]
value = sys.argv[2] or "delay"
df = pd.read_csv(file_path, parse_dates=["timestamp"])

# Round timestamps to the nearest 5 seconds
df["time_bucket"] = df["timestamp"].dt.floor("5s")

# Group by 5-second intervals and calculate the average value
avg_value = df.groupby("time_bucket")[value].mean().reset_index()

# Plot the result
plt.figure(figsize=(12, 6))
plt.plot(avg_value["time_bucket"], avg_value[value], marker='o', linestyle='-')
plt.title(f"Average {value} Every 5 Seconds")
plt.xlabel("Time")
plt.ylabel(f"Average {value} (ms)")
plt.xticks(rotation=45)
plt.grid(True)
plt.tight_layout()
plt.show()
