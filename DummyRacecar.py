class DummyRacecar:
    def __init__(self):
        self._steering = 0.0
        self._throttle = 0.0

    @property
    def steering(self):
        return self._steering

    @steering.setter
    def steering(self, value):
        self._steering = value
        print(f"[Dummy] Steering set to {value}")

    @property
    def throttle(self):
        return self._throttle

    @throttle.setter
    def throttle(self, value):
        self._throttle = value
        print(f"[Dummy] Throttle set to {value}")