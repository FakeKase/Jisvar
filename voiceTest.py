import edge_tts as etts
import asyncio
from playsound import playsound
import speech_recognition as sr

async def speak(text):
    communicate = etts.Communicate(text, voice="th-TH-NiwatNeural")
    await communicate.save("out.mp3")
    playsound("out.mp3")

recognizer = sr.Recognizer()
recognizer.pause_threshold = 1.5
with sr.Microphone() as source:
    print("Start")
    audio = recognizer.listen(source)

text = recognizer.recognize_google(audio, language="th-TH")
print("Yousaid: "+ text)
asyncio.run(speak(text))