import anthropic as atp
import edge_tts as etts
import asyncio
import os
import speech_recognition as sr
from playsound import playsound

async def speak(text):
    communicate = etts.Communicate(text, voice="en-IN-PrabhatNeural")
    await communicate.save("out.mp3")
    playsound("out.mp3")
    os.remove("out.mp3")

client = atp.Anthropic()
messages = []
recognizer = sr.Recognizer()
recognizer.pause_threshold = 1.5
while True:
    with sr.Microphone() as source:
        print("Start")
        audio = recognizer.listen(source)
    inputText = recognizer.recognize_google(audio, language="en-US")
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