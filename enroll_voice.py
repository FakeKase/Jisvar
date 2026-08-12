import numpy as np
import speech_recognition as sr

from voice_id import extract_speaker_embedding, VOICE_PROFILE_PATH

recognizer = sr.Recognizer()
recognizer.pause_threshold = 1.0

print("Let's enroll your voice for the open_app speaker check.")
print("You'll speak 3 short phrases (a few seconds each, say anything).\n")

embeddings = []
for i in range(3):
    input(f"Press Enter, then speak phrase {i + 1}/3...")
    with sr.Microphone() as source:
        print("Listening...")
        audio_data = recognizer.listen(source)
    raw = audio_data.get_raw_data(convert_rate=16000, convert_width=2)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    embeddings.append(extract_speaker_embedding(samples))
    print("Captured.\n")

reference = np.mean(embeddings, axis=0)
reference /= np.linalg.norm(reference)
np.save(VOICE_PROFILE_PATH, reference)
print(f"Saved voice profile to {VOICE_PROFILE_PATH}. Restart main.py to use it.")
