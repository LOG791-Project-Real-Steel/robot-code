# Robot Code

![Status: No Longer Maintained](https://img.shields.io/badge/status-no--longer--maintained-red?style=for-the-badge)
![Purpose: Production](https://img.shields.io/badge/purpose-production-blue?style=for-the-badge)
![Final Solution](https://img.shields.io/badge/final--solution-YES-success?style=for-the-badge)
![Component: Robot](https://img.shields.io/badge/component-robot-blue?style=for-the-badge)

Code that runs on the robot itself. Creates a `Racecar` object with camera for it to be exploited by this code.

## Pre-requisites

- Jetson Nano AI Kit 4GB - Jetpack 4.5.1
- Python 3.6
- Git

## Installation

If not done already, install the needed python dependencies on the robot. You can do that by running the following command:

```bash
pip install -r requirements.txt
```

## Running the code

> [!NOTE]
> This repository should be on the **Jetson Nano** directly to help with file transfer and development.

- First, you need to replace the `WebScoket` server's IP in the code with the IP on which your server runs.\
    Then, run the `WebSocket` solution code, simply run this command:
    ```bash
    python3 web_socket_client.py
    ```

- To run the `TCP Tailscale` solution code, simply run this command:
    ```bash
    python3 direct_tcp.py
    ```

> ![NOTE]
> After starting the script, it might take a while for the camera and controls to fully initialize. Please be patient as this can take up to 2 minutes.
>
> There should be a message in the logs letting you know that the script is ready when its done initializing.

## KPIs (Key Performance Indicators)

The present code has telemetry that is taken during runtime to evaluate the performance of the different solutions we have.

After each run, it will generate `.csv` files with all the data used to create plots (useful for modifying data to remove outliers). 

It will also create **2** `.png` files with 3 plots each containing different data sets.

---

> Made with care by [@Funnyadd](https://github.com/Funnyadd), [@ImprovUser](https://github.com/ImprovUser), [@cjayneb](https://github.com/cjayneb) and [@RaphaelCamara](https://github.com/RaphaelCamara) ❤️
