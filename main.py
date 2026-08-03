import anthropic as atp
import edge_tts as etts
import asyncio
import os
import pyaudio
import speech_recognition as sr
from playsound import playsound
import numpy as np
from openwakeword.model import Model
import threading
import pystray
from PIL import Image

FORMAT, CHANNELS, RATE, CHUNK = pyaudio.paInt16, 1, 16000, 1280
audio = pyaudio.PyAudio()
owwModel = Model(inference_framework="onnx")


def wait_for_wake_word():
    stream = audio.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK
    )
    try:
        while True:
            chunk = np.frombuffer(stream.read(CHUNK), dtype=np.int16)
            prediction = owwModel.predict(chunk)
            if prediction["hey_jarvis"] > 0.5:
                return
    finally:
        stream.stop_stream()
        stream.close()

async def speak(text):
    communicate = etts.Communicate(text, voice="en-IN-PrabhatNeural")
    await communicate.save("out.mp3")
    playsound("out.mp3")
    os.remove("out.mp3")

client = atp.Anthropic()
messages = []
recognizer = sr.Recognizer()
recognizer.pause_threshold = 1.5
print("Standby for wake word")

def voice_loop():
    wait_for_wake_word()
    while True:
        with sr.Microphone() as source:
            print("Start")
            audio_data = recognizer.listen(source)
        inputText = recognizer.recognize_google(audio_data, language="en-US")
        if 'quit' in inputText.lower(): break
        messages.append(
            {
                "role":"user",
                "content": inputText
            }
        )
        print("You:", inputText)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=messages
        )
        outputText = response.content[0].text
        messages.append(
            {
                "role":"assistant",
                "content": outputText
            }
        )
        print("Claude: "+response.content[0].text)
        asyncio.run(speak(outputText))
    print("Conversation end")
    icon.stop()

def on_quit(icon_obj, item):
    icon_obj.stop()

icon_image = Image.open("icon/nerd.png")
icon = pystray.Icon("jisvar", icon_image, "jisvar",
                     menu=pystray.Menu(pystray.MenuItem("Quit", on_quit)))

threading.Thread(target=voice_loop, daemon=True).start()
icon.run()