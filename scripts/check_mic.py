import sounddevice as sd
import soundfile as sf

duration = 10  # seconds
samplerate = 16000  # good for Whisper

print(f"Recording for {duration} seconds... speak now.")
recording = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1)
sd.wait()
print("Done recording.")

sf.write("test_output.wav", recording, samplerate)
print("Saved to test_output.wav")