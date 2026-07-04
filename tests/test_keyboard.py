import keyboard

print("Press and hold spacebar, then release. Press Esc to quit.")

while True:
    if keyboard.is_pressed('space'):
        print("Spacebar is being held...")
    if keyboard.is_pressed('esc'):
        print("Exiting.")
        break