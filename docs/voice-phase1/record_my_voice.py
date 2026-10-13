"""Records you reading 12 short lines, for comparing speech recognisers on your real voice.
The recordings stay on this laptop, in the 'myvoice' folder next to this script."""
import os, sys, time, wave
import numpy as np, sounddevice as sd

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "myvoice")
LINES = [
    ("w1", "Hey Jarvis"), ("w2", "Hello Jarvis"), ("w3", "Wake up Jarvis"), ("w4", "Jarvis wake up"),
    ("c1", "What is on my calendar tomorrow morning"), ("c2", "Remind me to call the dentist at three"),
    ("c3", "Turn the volume down a little"), ("c4", "Summarise the last email from my accountant"),
    ("c5", "Open the content manager and show me what is ready to post"),
    ("c6", "How much did I spend on models this week"),
    ("n1", "I was reading about the Jarvis project yesterday"), ("n2", "Hey, did you wake up early today"),
]
SECONDS, RATE = 5, 16000

os.makedirs(OUT, exist_ok=True)
print("\nYou'll see 12 short lines. For each one: press Enter, wait for 'Speak now', then read")
print(f"it out normally. Each recording lasts {SECONDS} seconds. Type q then Enter to stop early.\n")
with open(os.path.join(OUT, "lines.txt"), "w", encoding="utf-8") as f:
    f.writelines(f"{k}|{t}\n" for k, t in LINES)
for i, (key, text) in enumerate(LINES, 1):
    if input(f"[{i}/12]  \"{text}\"   -- press Enter when ready ").strip().lower() == "q":
        break
    print("   Speak now...", flush=True)
    audio = sd.rec(SECONDS * RATE, samplerate=RATE, channels=1, dtype="int16"); sd.wait()
    level = int(np.abs(audio).max())
    with wave.open(os.path.join(OUT, f"me_{key}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(audio.tobytes())
    print("   Saved." if level > 800 else "   Saved, but it sounded very quiet. Check the microphone if this keeps happening.")
print(f"\nDone. Recordings are in: {OUT}\nYou can tell Claude they're ready.")
