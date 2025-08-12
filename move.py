from jetracer.nvidia_racecar import NvidiaRacecar
import keyboard

# TODO: change these functions to receive input from another device through network
# (most likely should be in another file since that's where the complexity will be)
def go_forward():
    return keyboard.is_pressed('w')

def go_backward():
    return keyboard.is_pressed('s')

def turn_right():
    return keyboard.is_pressed('d')

def turn_left():
    return keyboard.is_pressed('a')

# Initialization of the robot and base values for steering and throttle
car = NvidiaRacecar()
car.steering_gain = -1
car.steering_offset = 0
car.steering = 0
car.throttle_gain = 0.8
print("ready to go!")

# Main loop that controls what the robot does
while True:
    try:
        if go_forward():
            car.throttle = 0.2
        elif go_backward():
            car.throttle = -0.2
        else:
            car.throttle = 0
            
        if turn_right():
            car.steering = 1
        elif turn_left():
            car.steering = -1
        else:
            car.steering = 0
    except OSError as err:
        print("Something wong :" + err)
