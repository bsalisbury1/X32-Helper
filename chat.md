I’m trying to help my drummer set up his Behringer X32 rack mixer.  a few weeks ago we moved all the channels to suit the new lineups needs but it doesn’t look like that saved on the unit itself.  but on my personal mix on my app on my phone it looks like it did a few weeks ago.  how do we get everything synced?

Here’s a read-only version. It only asks the rack questions and never changes anything. It uses just the Python standard library, so there’s nothing to install.

python
#!/usr/bin/env python3
"""Dump X32 channel names, colors, icons, and input sources. Read-only."""
import socket, struct, sys

COLORS = ["OFF","RD","GN","YE","BL","MG","CY","WH",
          "OFFi","RDi","GNi","YEi","BLi","MGi","CYi","WHi"]

def pad(b):
    return b + b"\0" * (4 - len(b) % 4)

def parse(data):
    end = data.index(b"\0")
    addr = data[:end].decode()
    pos = (end // 4 + 1) * 4
    tags = data[pos:data.index(b"\0", pos)].decode()[1:]
    pos = (pos + len(tags) + 1 + 4) // 4 * 4
    args = []
    for t in tags:
        if t == "s":
            e = data.index(b"\0", pos)
            args.append(data[pos:e].decode(errors="replace"))
            pos = (e // 4 + 1) * 4
        elif t == "i":
            args.append(struct.unpack(">i", data[pos:pos+4])[0]); pos += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[pos:pos+4])[0]); pos += 4
    return addr, args

def ask(sock, addr):
    for _ in range(3):
        try:
            sock.send(pad(addr.encode()))
            return parse(sock.recv(1024))[1][0]
        except socket.timeout:
            continue
    return None

def main(ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    sock.connect((ip, 10023))

    sock.send(pad(b"/xinfo"))
    try:
        info = parse(sock.recv(1024))[1]
        print("Connected:", info)
    except socket.timeout:
        sys.exit("No reply. Check the IP and that you're on the same network.")

    print(f"\n{'CH':<4}{'NAME':<12}{'COLOR':<7}{'ICON':<6}{'INSRC'}")
    for n in range(1, 33):
        base = f"/ch/{n:02d}/config"
        name = ask(sock, f"{base}/name")
        color = ask(sock, f"{base}/color")
        icon = ask(sock, f"{base}/icon")
        src = ask(sock, f"{base}/insrc")
        c = COLORS[color] if isinstance(color, int) and color < 16 else color
        print(f"{n:<4}{(name or ''):<12}{str(c):<7}{str(icon):<6}{src}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: x32_dump.py <rack-ip>"))

To run it: save it as x32_dump.py, then run python3 x32_dump.py 192.168.x.x. The rack’s IP is on its Setup → Network screen, and Mixing Station also shows it in its connection settings. Your laptop needs to be on the same wifi as the rack.

Reading the output:

NAME is the channel label, which is what you’d compare against your phone’s layout.
INSRC is the input source number. 0 is local In 01, 1 is In 02, and so on, with higher numbers being AES50, card, and aux inputs. This is the routing I mentioned earlier, so if names look right but the wrong instruments hit them, this column will show it.
A trailing i on a color means inverted.

Since the script is just a series of queries, it’s also easy to extend later with writes (like /ch/01/config/name plus a string) once you’re ready to make changes.

with python scripting, can something like “add gate and reverb to input 1” be executed with claude

Yes, with a caveat: the script can do the mechanical part, but “add gate and reverb” needs a few decisions that Claude would have to make with you.

Gate is the easy one. Every X32 channel has a built-in gate, so it’s just a few writes:

/ch/01/gate/on set to 1
/ch/01/gate/thr for the threshold
/ch/01/gate/range, attack, hold, and release for the behavior

The catch is that these values are normalized floats (0.0 to 1.0), not dB, so the script needs a conversion step. The threshold also depends on the source and the room, so you’d tune it by ear while the drummer plays.

Reverb takes more setup because it’s not per-channel. It lives in one of the 8 effects slots, so the script has to:

Check which FX slots are free (/fx/1/type, etc.) and load a reverb into one.
Find which mix bus feeds that slot.
Set channel 1’s send level to that bus (/ch/01/mix/NN/level).
Make sure the FX return is routed to the main mix.

If a slot or bus is already in use, it could clobber something, so the script should read first and only write to empty ones.

Where Claude fits:

In chat: I turn your sentence into a script, like the dump script above with writes added, and you run it.
With Claude Code: it can write, run, read back the results, and adjust in a loop. For example: “add a gate to the kick, now a little tighter,” with each request applied to the rack right away.

Either way, the script is deterministic once written. Claude is the translator from plain English to OSC commands and the one checking the rack’s current state first.

Since writes hit the live console, save a scene first, and try changes with the PA muted or at low level.

Want me to write the write-capable version, with a dry-run mode that prints what it would change without sending anything?

but with claude code, it can be more of an iterative agentic experience where claude executes the commands in real time?

Yes, that’s the main advantage of Claude Code. It runs on your laptop, on the same wifi as the rack, and can execute commands itself instead of handing you a script.

A typical session:

You say “add a gate and some reverb to input 1.”
Claude reads the rack’s current state (free FX slots, buses in use, existing gate settings).
It sends the changes over OSC, then reads the values back to confirm they landed.
You listen and say “tighter gate” or “less reverb,” and it adjusts and repeats.

Early on, Claude would usually write a small helper (a few functions like set_gate(ch, thr) or add_reverb_send(ch, level)). After that, each request is just a quick call to it, which keeps the loop fast.

Limits:

It can’t hear anything. You’re the ears, so “tighter” or “more room” still comes from you. It could read the rack’s meter data over OSC to see levels, but that’s no substitute for listening.
It’s not a live-performance tool. Each step involves a model response, so think soundcheck and setup, not mid-song tweaks.
Changes are instant on the live console. Claude Code asks permission before running commands by default, which is a useful checkpoint. Still, save a scene first and keep the PA muted or low while experimenting.

For a rehearsal-room setup like your drummer’s, it would work well, especially for tedious stuff like naming, coloring, and routing 16+ channels in one go.