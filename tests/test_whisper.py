from faster_whisper import WhisperModel

print("Loading Whisper model (this may take a moment on first run, it downloads the model)...")
model = WhisperModel("small", device="cpu", compute_type="int8")

print("Transcribing test_output.wav...")
segments, info = model.transcribe("test_output.wav")

print(f"Detected language: {info.language}")
for segment in segments:
    print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")