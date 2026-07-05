"""Verify push-to-talk only reacts to RIGHT ctrl.

Prints every key event's resolved name, and tracks right ctrl the same way
voice_agent does (by event name, not keyboard.is_pressed — is_pressed("right
ctrl") also fires for left ctrl because both keys share scan code 29 on
Windows). Type, hit left-ctrl shortcuts, then hold right ctrl: only right
ctrl should flip PTT. Press Esc to quit.
"""
import keyboard

PUSH_TO_TALK_KEY = "right ctrl"
ptt_down = False


def on_event(event):
    global ptt_down
    name = (event.name or "").lower()
    if name == PUSH_TO_TALK_KEY:
        ptt_down = event.event_type == keyboard.KEY_DOWN
    ptt = "PTT ACTIVE" if ptt_down else "          "
    print(f"[{ptt}] {event.event_type:<4} name={event.name!r} scan_code={event.scan_code}")


keyboard.hook(on_event)
print(__doc__)
keyboard.wait("esc")
