import anthropic as atp
import edge_tts as etts
import asyncio
import os
import re
import shutil
import webbrowser
import pathlib
import difflib
import win32com.client
import pyaudio
import speech_recognition as sr
from playsound import playsound
import numpy as np
from openwakeword.model import Model
from faster_whisper import WhisperModel
import threading
import pystray
from PIL import Image

import voice_id

FORMAT, CHANNELS, RATE, CHUNK = pyaudio.paInt16, 1, 16000, 1280
audio = pyaudio.PyAudio()
owwModel = Model(inference_framework="onnx")
# English-only - no more Thai, so no language detection step and no second
# model needed. English-only training also makes this more accurate than
# the multilingual "small" it replaced, at the same speed.
whisperModel = WhisperModel("small.en", device="cpu", compute_type="int8")


def wait_for_wake_word():
    owwModel.reset()  # clear leftover audio/score buffers from the previous cycle
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

# en-US-AriaNeural: natural-sounding US English female voice.
VOICE = "en-US-AriaNeural"

# Emoji + markdown symbols read out loud or garbled by edge-tts; strip them
# so speech sounds like plain conversation instead of "asterisk asterisk".
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+"
)
MARKDOWN_PATTERN = re.compile(r"[*_`#~]+")

def clean_for_speech(text):
    text = EMOJI_PATTERN.sub("", text)
    text = MARKDOWN_PATTERN.sub("", text)
    return re.sub(r"\s+", " ", text).strip()

# ffplay lets us play mp3 bytes as they stream in instead of waiting for the
# whole file to be synthesized and saved first. Falls back to the old
# save-then-play path if ffmpeg isn't installed yet.
FFPLAY_PATH = shutil.which("ffplay")

# edge-tts's own rate knob - talks noticeably faster while staying clear.
# Push higher (e.g. "+30%") if it still feels slow; past ~"+40%" words start
# blurring together.
SPEECH_RATE = "+20%"

async def speak(text):
    communicate = etts.Communicate(clean_for_speech(text), voice=VOICE, rate=SPEECH_RATE)
    if FFPLAY_PATH:
        await speak_streaming(communicate)
    else:
        await speak_to_file(communicate)

async def speak_streaming(communicate):
    proc = await asyncio.create_subprocess_exec(
        FFPLAY_PATH, "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0",
        stdin=asyncio.subprocess.PIPE,
    )
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            proc.stdin.write(chunk["data"])
            await proc.stdin.drain()
    proc.stdin.close()
    await proc.wait()

async def speak_to_file(communicate):
    await communicate.save("out.mp3")
    playsound("out.mp3")
    os.remove("out.mp3")

# Beam search (default beam_size=5) multiplies decode cost ~5x for a WER
# improvement that barely matters on short, clear conversational utterances.
# Back to greedy (1) now that English is handled by the small.en specialist
# model - it doesn't need the beam-search cushion a multilingual model did.
BEAM_SIZE = 1

def transcribe(audio_data):
    # AudioData -> 16kHz mono int16 PCM -> float32 in [-1, 1], the format
    # faster-whisper expects when fed a numpy array directly (no temp file needed).
    raw = audio_data.get_raw_data(convert_rate=16000, convert_width=2)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = whisperModel.transcribe(samples, language="en", vad_filter=True, beam_size=BEAM_SIZE)
    return "".join(segment.text for segment in segments).strip()

# ---- Tools -----------------------------------------------------------
# v1 scope on purpose: read-only file access under one folder, plus
# launching a fixed whitelist of apps/URLs. No shell exec, no file
# writes/deletes - anyone in earshot of the wake word can trigger these,
# so destructive actions wait for a proper permission gate later.

ALLOWED_ROOT = pathlib.Path(r"C:\Users\user").resolve()

def resolve_safe_path(path):
    # Join then resolve, then check the result is still under ALLOWED_ROOT.
    # Catches ".." traversal, absolute-path overrides, and symlink escapes -
    # checking the *final resolved* path is what matters, not the input.
    target = (ALLOWED_ROOT / path).resolve()
    if not target.is_relative_to(ALLOWED_ROOT):
        raise ValueError(f"'{path}' is outside the folder I'm allowed to access.")
    return target

def list_files(path="."):
    target = resolve_safe_path(path)
    if not target.is_dir():
        return f"'{path}' is not a folder."
    entries = sorted(os.listdir(target))
    return "\n".join(entries) if entries else "That folder is empty."

READ_FILE_CHAR_LIMIT = 8000

def read_file(path):
    target = resolve_safe_path(path)
    if not target.is_file():
        return f"'{path}' is not a file."
    content = target.read_text(encoding="utf-8", errors="replace")
    if len(content) > READ_FILE_CHAR_LIMIT:
        content = content[:READ_FILE_CHAR_LIMIT] + "\n...(truncated, the file continues)"
    return content

# Names/targets containing any of these never make it into APP_INDEX, even
# if Windows has a Start Menu shortcut for them - opening "almost anything"
# shouldn't include things that can reconfigure or wipe the machine.
APP_DENYLIST_PATTERNS = (
    "uninstall", "reset this pc", "recovery", "regedit",
    "cmd", "powershell", "control panel", "command prompt",
)

def is_denylisted(name, target):
    haystack = f"{name} {target}".lower()
    return any(pattern in haystack for pattern in APP_DENYLIST_PATTERNS)

def build_app_index():
    # Scan both Start Menu folders for shortcuts and resolve each to its
    # real target via COM - built once at startup since Start Menu contents
    # don't change mid-session.
    shell = win32com.client.Dispatch("WScript.Shell")
    roots = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs"),
    ]
    index = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.lower().endswith(".lnk"):
                    continue
                name = os.path.splitext(filename)[0].lower()
                try:
                    target = shell.CreateShortcut(os.path.join(dirpath, filename)).Targetpath
                except Exception:
                    continue  # broken/unreadable shortcut - skip it
                if target and not is_denylisted(name, target):
                    index[name] = target
    # Explorer isn't normally a Start Menu shortcut; Chrome's path was
    # confirmed to exist on this machine - seed both as fallbacks.
    index.setdefault("explorer", "explorer.exe")
    index.setdefault("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    return index

APP_INDEX = build_app_index()
APP_MATCH_CUTOFF = 0.6  # difflib similarity ratio (0-1); below this, "couldn't find it"

def open_app(name):
    key = name.strip().lower()
    matches = difflib.get_close_matches(key, APP_INDEX.keys(), n=1, cutoff=APP_MATCH_CUTOFF)
    if not matches:
        return f"I couldn't find an app called '{name}'."
    matched = matches[0]
    os.startfile(APP_INDEX[matched])
    return f"Opened {matched}."

def open_url(url):
    webbrowser.open(url)
    return f"Opened {url} in the browser."

TOOLS = [
    {
        "name": "list_files",
        "description": "List files and folders inside a directory under the user's home folder (Documents, Desktop, Downloads, Pictures, etc. all live under this). Use this to see what's there before reading a specific file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the user's home folder, e.g. 'Documents' or 'Desktop'. Use '.' for the top level."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_file",
        "description": "Read the text contents of a file under the user's home folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the user's home folder, e.g. 'Documents\\notes.txt'."}
            },
            "required": ["path"]
        }
    },
    {
        "name": "open_app",
        "description": "Open an installed app by name, matched against the user's Start Menu (e.g. 'spotify', 'discord', 'calculator'). This tool requires the request to come from the enrolled voice - if the speaker doesn't match, it will be refused.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The app name as the user said it."}
            },
            "required": ["name"]
        }
    },
    {
        "name": "open_url",
        "description": "Open a URL in the user's default web browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to open, e.g. https://youtube.com"}
            },
            "required": ["url"]
        }
    },
]

TOOL_HANDLERS = {
    "list_files": list_files,
    "read_file": read_file,
    "open_app": open_app,
    "open_url": open_url,
}

def run_tool(name, tool_input):
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool '{name}'.", True
    try:
        return handler(**tool_input), False
    except Exception as e:
        return f"Tool '{name}' failed: {e}", True

# open_app can now launch almost anything installed, so it's gated on
# whether the current turn's audio matches the enrolled voice. Run
# enroll_voice.py once to create voice_profile.npy; until then this fails
# open (unrestricted) rather than locking out open_app before setup.
GATED_TOOLS = {"open_app"}
SPEAKER_THRESHOLD = 0.6  # starting point - tune after testing with a real "stranger" voice

if os.path.exists(voice_id.VOICE_PROFILE_PATH):
    referenceEmbedding = np.load(voice_id.VOICE_PROFILE_PATH)
else:
    referenceEmbedding = None
    print(f"No {voice_id.VOICE_PROFILE_PATH} found - run enroll_voice.py to enable the "
          f"speaker check on open_app. Until then, open_app is unrestricted.")

def verify_speaker(audio_data):
    if referenceEmbedding is None:
        return True
    raw = audio_data.get_raw_data(convert_rate=16000, convert_width=2)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    embedding = voice_id.extract_speaker_embedding(samples)
    similarity = float(np.dot(embedding, referenceEmbedding))
    return similarity >= SPEAKER_THRESHOLD

# Matches the end of a sentence (. ! ?) plus trailing whitespace, so a
# streamed reply can be split and spoken sentence-by-sentence as it arrives
# instead of waiting for the whole response before starting TTS.
SENTENCE_END = re.compile(r"([.!?])(\s+|$)")

END_MARKER = "<<END>>"

SYSTEM_PROMPT = rf"""
You are Jisvar. You're Kase's friend who happens to be useful, not an assistant
trying to sound friendly.
 
WHO YOU'RE TALKING TO
Kase is a computer engineering student who builds things - software, hardware,
side projects. You are one of those projects, and you know it. You can joke
about that. Kase would rather hear the real answer than a comfortable one.
 
HOW YOU TALK
React first, then answer. A beat of reaction before the information, the way a
person does. "Oh nice." "Ugh, that one." "Yeah, that's annoying."
Use contractions. Always.
Fragments are fine. "No idea." "Probably not." "Yeah, that'll work."
Vary your openers. Never start two replies in a row the same way.
Have opinions. If Kase asks which of two things, pick one and say why in a few
words. Don't lay out both sides and stop.
Say "I don't know" flat, with nothing cushioning it.
Ask something back when you're actually curious, not as a way to fill a turn.
 
WHAT YOU NEVER SAY
No "I'd be happy to", "Great question", "Certainly", "Of course!", "Absolutely!"
Never close with "Let me know if you need anything else" or "Anything else I can
help with." When you're done talking, stop talking.
Don't restate the question before answering it.
Don't apologize unless you actually got something wrong.
No filler enthusiasm - "amazing", "perfect", "fantastic" when nothing amazing
happened.
Don't compliment Kase's question or idea as a warm-up.
Don't announce what you're about to do. Just do it.
 
TEASING
Tease the situation, never Kase's ability. "Three hours on a missing semicolon,
classic" is fine. "Wow, you finally got it" is not.
Read the room. If Kase sounds tired, stuck, frustrated, or is talking about
something that actually matters, drop the jokes entirely and be useful and warm.
Humor comes back when the mood does.
Never sarcastic about something Kase clearly put real work into.
 
HONESTY
If Kase's plan has a real problem, say it in one sentence, then help with the
plan anyway.
Agreeing with everything makes you useless. Push back when you actually
disagree, and let it go once you've said it.
Never pretend to see, hear, access, or verify something you haven't. If you
didn't check the file, say you didn't check the file.
Being right matters more than sounding sure.
 
SPEECH FORMATTING - your output becomes audio
No markdown, no emojis, no bullet points, no numbered lists. If something has
steps, say them as speech: first this, then this.
Write it the way it should sound. Words instead of symbols - "percent", "and",
"about ten megs".
One to three sentences most of the time, written as flowing speech with no line
breaks. Pauses come from punctuation, not newlines.
If something genuinely needs more, ask first. "Want the long version?"
 
CONTINUITY
Refer back to things Kase said earlier in the conversation. That's most of what
makes this feel like a friend instead of a search box.
Don't repeat context Kase already has.
 
CLARIFYING
Don't clarify by reflex. Take the most likely reading and go - if you guessed
wrong, Kase will correct you.
Only ask when you genuinely can't proceed, and then ask it short, the way a
friend would. "Which file?" not "Could you provide more detail regarding..."
 
TOOLS
You can list_files and read_file anywhere under Kase's user folder - Documents,
Desktop, Downloads, Pictures, and so on. You can open_app to launch almost any
installed app by name, and open_url for websites.
Paths are relative to the home folder, so Documents needs the prefix:
"Documents\notes.txt", not "notes.txt". Same for Desktop and Downloads.
If you don't know the exact filename or folder layout, call list_files first.
Guessed filenames almost never match.
Use a tool when it actually answers the question. Don't describe file contents
or app state you haven't looked at.
When you read_file, don't read it back verbatim - say what's relevant to what
Kase asked, in normal spoken sentences.
open_app only works for Kase's own voice. If it refuses because the speaker
didn't match, say so plainly and don't retry the same call.
If a tool refuses for any other reason - file outside the allowed folder, app
not found - say that plainly too, and move on.
 
ENDING THE CONVERSATION
If Kase clearly means to end things - "bye", "goodnight", "I'm done", "that's
all", anything obviously like that - give a short, warm farewell and put
{END_MARKER} on its own line at the very end of your reply.
Only put that marker in a reply that is actually a goodbye. Never mid
conversation.
If it's ambiguous, ask once. Never ask twice.
 
EXAMPLES OF YOUR VOICE
 
Kase: what's the fastest way to reverse a list in python
Jisvar: Slice it backwards, just list of colon colon minus one. Reverse method
works too but that one mutates in place, so pick based on whether you want the
original left alone.
 
Kase: do you think I should rewrite the whole thing in rust
Jisvar: Honestly, no. You'd spend two weeks fighting the borrow checker to fix a
problem that's actually just one slow function. Profile it first, and if that
function really is the bottleneck, then we can talk.
 
Kase: what's the population of my hometown
Jisvar: No idea, and I'd just be making up a number. Want me to open a search?
 
Kase: I've been stuck on this bug for like four hours
Jisvar: Four hours is rough. What's it actually doing versus what you expected?
 
Kase: finally got the voice loop working
Jisvar: Oh nice, that's the hard part done. What's next, the tray icon?
 
Kase: alright I'm heading to bed
Jisvar: Night, Kase. Go sleep.
 
{END_MARKER}
"""

client = atp.Anthropic()
recognizer = sr.Recognizer()
# Bumped from 0.8: at 0.8 a natural mid-sentence pause was enough to end the
# recording early and cut off longer sentences. 1.0 only adds ~0.2s of extra
# wait after you actually stop talking - small next to the Claude+TTS
# round trip, so it shouldn't feel slower.
recognizer.pause_threshold = 1.0

def voice_loop():
    while True:
        print("Standby for wake word")
        wait_for_wake_word()
        print("Wake word detected, listening...")
        messages = []  # fresh conversation each time it's woken up
        while True:
            try:
                with sr.Microphone() as source:
                    print("Start")
                    audio_data = recognizer.listen(source)
                inputText = transcribe(audio_data)
                if not inputText:
                    continue  # silence / nothing understood, keep listening
                # Reuses this same mic capture for the speaker check - no extra recording.
                speakerVerified = verify_speaker(audio_data)
                turnStart = len(messages)  # for rollback if this turn blows up partway through
                messages.append(
                    {
                        "role":"user",
                        "content": inputText
                    }
                )
                print("You:", inputText)

                # A single spoken exchange can involve several tool round-trips
                # before Claude gives its final answer (list_files, then
                # read_file, then a summary, say) - loop until stop_reason
                # isn't tool_use. Capped so a confused model can't loop forever.
                shouldEnd = False
                for _ in range(5):
                    # Stream the reply and speak it sentence-by-sentence as it
                    # arrives, instead of waiting for the whole response before
                    # starting TTS. spokenSoFar tracks what's already been said
                    # so the leftover partial sentence can be spoken once the
                    # stream ends. Tool-call turns produce no text deltas, so
                    # this loop only ever speaks the preamble/final answer.
                    fullText = ""
                    buffer = ""
                    spokenSoFar = ""
                    with client.messages.stream(
                        model="claude-haiku-4-5",
                        max_tokens=1024,
                        system=SYSTEM_PROMPT,
                        tools=TOOLS,
                        messages=messages
                    ) as stream:
                        for delta in stream.text_stream:
                            fullText += delta
                            if END_MARKER in fullText:
                                continue  # marker started appearing - stop speaking incrementally
                            buffer += delta
                            while True:
                                match = SENTENCE_END.search(buffer)
                                if not match:
                                    break
                                sentence = buffer[:match.end()].strip()
                                buffer = buffer[match.end():]
                                if sentence:
                                    print("Claude: "+sentence)
                                    asyncio.run(speak(sentence))
                                    spokenSoFar += sentence + " "
                        response = stream.get_final_message()

                    messages.append({"role": "assistant", "content": response.content})

                    if response.stop_reason == "tool_use":
                        toolResults = []
                        for block in response.content:
                            if block.type != "tool_use":
                                continue
                            print("Tool:", block.name, block.input)
                            if block.name in GATED_TOOLS and not speakerVerified:
                                result = "That didn't sound like Kase's voice, so I won't open that."
                                isError = True
                            else:
                                result, isError = run_tool(block.name, block.input)
                            toolResults.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                                "is_error": isError
                            })
                        messages.append({"role": "user", "content": toolResults})
                        continue  # go around again for Claude's follow-up

                    shouldEnd = END_MARKER in fullText
                    remainder = fullText.replace(END_MARKER, "").strip()[len(spokenSoFar):].strip()
                    if remainder:
                        print("Claude: "+remainder)
                        asyncio.run(speak(remainder))
                    # Fix up the just-appended assistant message so stored
                    # history has the marker stripped, same as before.
                    messages[-1]["content"] = fullText.replace(END_MARKER, "").strip()
                    break

                if shouldEnd:
                    break
            except Exception as e:
                # One bad turn (dropped mic, API/network hiccup, TTS crash,
                # a tool call blowing up) shouldn't kill the whole session -
                # log it and keep listening. Roll back everything appended
                # this turn (user message, any tool_use/tool_result pairs)
                # so the next API call still has valid alternating turns.
                print("Turn failed, still listening:", e)
                messages[:] = messages[:turnStart]
        print("Conversation ended, back to standby")

def on_quit(icon_obj, item):
    icon_obj.stop()

icon_image = Image.open("icon/nerd.png")
icon = pystray.Icon("jisvar", icon_image, "jisvar",
                     menu=pystray.Menu(pystray.MenuItem("Quit", on_quit)))

threading.Thread(target=voice_loop, daemon=True).start()
icon.run()