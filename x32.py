#!/usr/bin/env python3
"""
x32.py - Talk to a Behringer X32 over OSC. Standard library only.

SAFETY: dry-run by default. Reads always go to the console (if an IP is
given), but writes are only printed unless you pass --live. Every live write
is logged to x32_history.jsonl so it can be undone with `undo`.

    python3 x32.py dump
    python3 x32.py gate 1 on --thr -40 --release 120
    python3 x32.py --live gate 1 on --thr -40 --release 120
    python3 x32.py --live undo

The console IP comes from .env (copy .env.example to start). An X32_IP
environment variable or --ip on the command line overrides it.

Parameter ranges and enum numbering below come from the unofficial X32 OSC
documentation (Patrick-Gilles Maillot). Confirm them against Mixing Station
the first time you connect. The NOTES section in CLAUDE.md explains how.
"""
import argparse
import json
import math
import os
import socket
import struct
import sys
import time
from pathlib import Path


def load_env(path=None):
    """Read KEY=VALUE lines from .env into os.environ. Real environment variables win."""
    path = Path(path or os.environ.get("X32_ENV", Path(__file__).with_name(".env")))
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value


load_env()

PORT = int(os.environ.get("X32_PORT", 10023))
HISTORY_FILE = Path(os.environ.get("X32_HISTORY", Path(__file__).with_name("x32_history.jsonl")))

COLORS = ["OFF", "RD", "GN", "YE", "BL", "MG", "CY", "WH",
          "OFFi", "RDi", "GNi", "YEi", "BLi", "MGi", "CYi", "WHi"]
GATE_MODES = ["EXP2", "EXP3", "EXP4", "GATE", "DUCK"]
# FX type numbers 0-9 are the reverbs. Higher numbers are delays, mod, EQ, etc.
FX_TYPES = ["HALL", "AMBI", "RPLT", "ROOM", "CHAM", "PLAT", "VREV", "VRM", "GATE", "RVRS"]
REVERB_IDS = set(range(len(FX_TYPES)))

# Leaf names whose OSC value is an int. Names are strings, everything else float.
INT_LEAVES = {"on", "mode", "color", "icon", "insrc", "type", "l", "r", "keysrc", "st", "mono"}
STR_LEAVES = {"name"}


class X32Error(Exception):
    pass


# --------------------------------------------------------------------------- OSC

def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def encode(addr, *args):
    """Build an OSC message. With no args it's a query (address only, which the X32 accepts)."""
    msg = _pad(addr.encode())
    if not args:
        return msg
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, bool):
            a = int(a)
        if isinstance(a, int):
            tags += "i"; payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"; payload += _pad(a.encode())
        else:
            raise TypeError(f"can't OSC-encode {a!r}")
    return msg + _pad(tags.encode()) + payload


def decode(data):
    end = data.index(b"\0")
    addr = data[:end].decode()
    pos = (end // 4 + 1) * 4
    if pos >= len(data) or data[pos:pos + 1] != b",":
        return addr, []
    tend = data.index(b"\0", pos)
    tags = data[pos + 1:tend].decode()
    pos = (tend // 4 + 1) * 4
    args = []
    for t in tags:
        if t == "s":
            e = data.index(b"\0", pos)
            args.append(data[pos:e].decode(errors="replace"))
            pos = (e // 4 + 1) * 4
        elif t == "i":
            args.append(struct.unpack(">i", data[pos:pos + 4])[0]); pos += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[pos:pos + 4])[0]); pos += 4
        elif t == "b":
            n = struct.unpack(">i", data[pos:pos + 4])[0]
            args.append(data[pos + 4:pos + 4 + n]); pos += 4 + (n + 3) // 4 * 4
        else:
            raise ValueError(f"unsupported OSC type tag {t!r}")
    return addr, args


# ------------------------------------------------------------------ conversions
# The X32 sends most values as 0.0-1.0 floats. These map them to real units.

def db_to_fader(db):
    """Fader/send level in dB (-inf to +10) -> X32 float, using the console's 4-segment law."""
    if db <= -90:
        return 0.0
    if db < -60:
        f = (db + 90) / 480
    elif db < -30:
        f = (db + 70) / 160
    elif db < -10:
        f = (db + 50) / 80
    else:
        f = (db + 30) / 40
    return round(min(max(f, 0.0), 1.0) * 1023) / 1023


def fader_to_db(f):
    if f >= 0.5:
        return f * 40 - 30
    if f >= 0.25:
        return f * 80 - 50
    if f >= 0.0625:
        return f * 160 - 70
    if f > 0:
        return f * 480 - 90
    return -math.inf


def _lin(v, lo, hi):
    return (min(max(v, lo), hi) - lo) / (hi - lo)


def _unlin(f, lo, hi):
    return lo + f * (hi - lo)


def _log(v, lo, hi):
    v = min(max(v, lo), hi)
    return math.log(v / lo) / math.log(hi / lo)


def _unlog(f, lo, hi):
    return lo * (hi / lo) ** f


# name: (to_float, from_float, lo, hi, unit)
GATE_PARAMS = {
    "thr":     (_lin, _unlin, -80.0, 0.0, "dB"),
    "range":   (_lin, _unlin, 3.0, 60.0, "dB"),
    "attack":  (_lin, _unlin, 0.0, 120.0, "ms"),
    "hold":    (_log, _unlog, 0.02, 2000.0, "ms"),
    "release": (_log, _unlog, 5.0, 4000.0, "ms"),
}


def fmt_db(f):
    db = fader_to_db(f)
    return "-inf dB" if db == -math.inf else f"{db:+.1f} dB"


def _gate_fmt(key):
    _, back, lo, hi, unit = GATE_PARAMS[key]
    return lambda f: f"{back(f, lo, hi):.1f} {unit}"


def ch(n):
    n = int(n)
    if not 1 <= n <= 32:
        raise X32Error(f"channel must be 1-32, got {n}")
    return f"/ch/{n:02d}"


def fx_name(t):
    return FX_TYPES[t] if isinstance(t, int) and 0 <= t < len(FX_TYPES) else str(t)


def source_name(s):
    if s == 0:
        return "INS"
    if isinstance(s, int) and 1 <= s <= 16:
        return f"MIX{s:02d}"
    return str(s)


def coerce(addr, value):
    """Give a value the OSC type the X32 expects for this address."""
    leaf = addr.rstrip("/").rsplit("/", 1)[-1]
    if leaf in STR_LEAVES:
        return str(value)
    if leaf in INT_LEAVES:
        return int(float(value))
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value  # unknown string parameter; send as-is
    return float(value)


# ---------------------------------------------------------------------- console

class X32:
    def __init__(self, ip=None, live=False, timeout=0.5, port=PORT, log=print, history=HISTORY_FILE):
        self.ip = ip or os.environ.get("X32_IP")
        self.live = live
        self.timeout = timeout
        self.log = log
        self.history = Path(history)
        self.sock = None
        if self.ip:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.connect((self.ip, port))
        elif live:
            raise X32Error("--live needs the console IP: set X32_IP in .env (see .env.example) or pass --ip")

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    # ---- raw read/write

    def get(self, addr):
        """Query one address. Returns the value, a list for multi-value replies, or None."""
        if not self.sock:
            return None
        msg = encode(addr)
        for _ in range(3):
            self.sock.send(msg)
            deadline = time.monotonic() + self.timeout
            while (remaining := deadline - time.monotonic()) > 0:
                self.sock.settimeout(remaining)
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    break
                except ConnectionRefusedError:
                    raise X32Error(f"nothing listening at {self.ip}:{PORT}")
                try:
                    raddr, args = decode(data)
                except (ValueError, IndexError, struct.error):
                    continue
                if raddr == addr:  # ignore late replies to earlier queries
                    if not args:
                        return None
                    return args[0] if len(args) == 1 else args
        return None

    def set(self, addr, value, fmt=None, verify=True):
        """Write one value. In dry-run mode, prints what would change and sends nothing."""
        value = coerce(addr, value)
        old = self.get(addr)
        show = (lambda v: "?" if v is None else (fmt(v) if fmt and isinstance(v, float) else repr(v)))
        if not self.live:
            self.log(f"  [dry-run] {addr}: {show(old)} -> {show(value)}")
            return value
        self.sock.send(encode(addr, value))
        self._record(addr, old, value)
        if not verify:
            self.log(f"  [live] {addr}: {show(old)} -> {show(value)}")
            return value
        new = self.get(addr)
        note = ""
        if new is None:
            note = "  (no read-back; check the console)"
        elif isinstance(value, float) and isinstance(new, float) and abs(new - value) > 0.002:
            note = f"  (console rounded to {show(new)})"
        elif not isinstance(value, float) and new != value:
            note = f"  (WARNING: console reports {new!r})"
        self.log(f"  [live] {addr}: {show(old)} -> {show(new if new is not None else value)}{note}")
        return new

    # ---- undo log

    def _record(self, addr, old, new):
        with self.history.open("a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "addr": addr,
                                "old": old, "new": new}) + "\n")

    def read_history(self):
        if not self.history.exists():
            return []
        return [json.loads(line) for line in self.history.read_text().splitlines() if line.strip()]

    def undo(self, steps=1):
        entries = self.read_history()
        if not entries:
            self.log("  nothing to undo")
            return
        todo, keep = entries[-steps:], entries[:-steps]
        for e in reversed(todo):
            if e["old"] is None:
                self.log(f"  can't undo {e['addr']}: previous value was never read")
                continue
            if not self.live:
                self.log(f"  [dry-run] undo {e['addr']}: {e['new']!r} -> {e['old']!r}")
                continue
            self.sock.send(encode(e["addr"], coerce(e["addr"], e["old"])))
            self.log(f"  [live] undo {e['addr']}: {e['new']!r} -> {e['old']!r}")
        if self.live:
            self.history.write_text("".join(json.dumps(e) + "\n" for e in keep))

    # ---- channel helpers

    def info(self):
        return self.get("/xinfo")

    def set_name(self, n, name):
        if len(name) > 12:
            self.log(f"  note: X32 names max out at 12 characters; using {name[:12]!r}")
        return self.set(f"{ch(n)}/config/name", name[:12])

    def set_color(self, n, color):
        by_upper = {c.upper(): i for i, c in enumerate(COLORS)}  # "rdi" -> RDi (inverted red)
        if color.upper() not in by_upper:
            raise X32Error(f"color must be one of {', '.join(COLORS)}")
        return self.set(f"{ch(n)}/config/color", by_upper[color.upper()])

    def set_source(self, n, insrc):
        return self.set(f"{ch(n)}/config/insrc", int(insrc))

    def set_fader(self, n, db):
        return self.set(f"{ch(n)}/mix/fader", db_to_fader(db), fmt=fmt_db)

    def mute(self, n, muted=True):
        # X32 stores "on" (1 = unmuted), so mute means on = 0
        return self.set(f"{ch(n)}/mix/on", 0 if muted else 1)

    def gate(self, n, on=None, mode=None, **params):
        base = f"{ch(n)}/gate"
        if mode is not None:
            if mode.upper() not in GATE_MODES:
                raise X32Error(f"gate mode must be one of {', '.join(GATE_MODES)}")
            self.set(f"{base}/mode", GATE_MODES.index(mode.upper()))
        for key, val in params.items():
            if val is None:
                continue
            to_f, _, lo, hi, unit = GATE_PARAMS[key]
            if not lo <= val <= hi:
                self.log(f"  note: gate {key} range is {lo}-{hi} {unit}; clamping {val}")
            self.set(f"{base}/{key}", to_f(val, lo, hi), fmt=_gate_fmt(key))
        if on is not None:
            self.set(f"{base}/on", int(on))

    def read_gate(self, n):
        base = f"{ch(n)}/gate"
        out = {"on": self.get(f"{base}/on")}
        m = self.get(f"{base}/mode")
        out["mode"] = GATE_MODES[m] if isinstance(m, int) and m < len(GATE_MODES) else m
        for key, (_, back, lo, hi, unit) in GATE_PARAMS.items():
            f = self.get(f"{base}/{key}")
            out[key] = f"{back(f, lo, hi):.1f} {unit}" if isinstance(f, float) else f
        return out

    def send(self, n, bus, db, on=True):
        """Channel n's send to mix bus 1-16."""
        if not 1 <= int(bus) <= 16:
            raise X32Error(f"bus must be 1-16, got {bus}")
        base = f"{ch(n)}/mix/{int(bus):02d}"
        self.set(f"{base}/level", db_to_fader(db), fmt=fmt_db)
        if on is not None:
            self.set(f"{base}/on", int(on))

    # ---- effects

    def fx_slots(self):
        slots = []
        for s in range(1, 9):
            t = self.get(f"/fx/{s}/type")
            if s <= 4:  # only FX1-4 can be fed from a bus; 5-8 are insert-only
                l, r = self.get(f"/fx/{s}/source/l"), self.get(f"/fx/{s}/source/r")
            else:
                l = r = 0
            slots.append({"slot": s, "type": t, "type_name": fx_name(t), "source_l": l, "source_r": r})
        return slots

    def find_reverbs(self):
        return [s for s in self.fx_slots()[:4]
                if s["type"] in REVERB_IDS and isinstance(s["source_l"], int) and 1 <= s["source_l"] <= 16]

    def reverb(self, n, db, slot=None):
        """Send channel n to an existing reverb (one fed from a mix bus)."""
        if not self.sock:
            raise X32Error("need a connection to find which bus feeds the reverb")
        revs = self.find_reverbs()
        if slot:
            revs = [r for r in revs if r["slot"] == int(slot)]
        if not revs:
            raise X32Error("no reverb on FX1-4 is fed from a mix bus. Run `fx` to see the slots, "
                           "then `setup-reverb SLOT BUS` to configure one.")
        r = revs[0]
        self.log(f"  using FX{r['slot']} ({r['type_name']}) fed from {source_name(r['source_l'])}")
        self.send(n, r["source_l"], db)

    def setup_reverb(self, slot, bus, fx_type="HALL", force=False):
        """Load a reverb into FX1-4, feed it from a mix bus, and open its return."""
        slot, bus = int(slot), int(bus)
        if not 1 <= slot <= 4:
            raise X32Error("only FX1-4 can be fed from a bus")
        if not 1 <= bus <= 16:
            raise X32Error("bus must be 1-16")
        fx_type = fx_type.upper()
        if fx_type not in FX_TYPES:
            raise X32Error(f"reverb type must be one of {', '.join(FX_TYPES)}")
        cur_t, cur_l = self.get(f"/fx/{slot}/type"), self.get(f"/fx/{slot}/source/l")
        if cur_t is not None and cur_t not in REVERB_IDS and cur_l not in (0, None) and not force:
            raise X32Error(f"FX{slot} is currently {fx_name(cur_t)} fed from {source_name(cur_l)}, "
                           f"which looks in use. Pick another slot or pass --force.")
        # Any channel already sending to this bus will now go to the reverb.
        users = [n for n in range(1, 33) if (self.get(f"{ch(n)}/mix/{bus:02d}/on") == 1
                 and (lv := self.get(f"{ch(n)}/mix/{bus:02d}/level")) and lv > 0)] if self.sock else []
        if users and not force:
            raise X32Error(f"bus {bus} already has sends from channels {users} (maybe a monitor mix). "
                           f"Pick an unused bus or pass --force.")
        self.set(f"/fx/{slot}/type", FX_TYPES.index(fx_type))
        self.set(f"/fx/{slot}/source/l", bus)
        self.set(f"/fx/{slot}/source/r", bus)
        self.set(f"/bus/{bus:02d}/mix/on", 1)
        bf = self.get(f"/bus/{bus:02d}/mix/fader")
        if bf is None or bf == 0.0:
            self.set(f"/bus/{bus:02d}/mix/fader", db_to_fader(0), fmt=fmt_db)
        for rtn in (2 * slot - 1, 2 * slot):  # FX1 returns on fxrtn 01/02, FX2 on 03/04, ...
            self.set(f"/fxrtn/{rtn:02d}/mix/on", 1)
            rf = self.get(f"/fxrtn/{rtn:02d}/mix/fader")
            if rf is None or rf == 0.0:
                self.set(f"/fxrtn/{rtn:02d}/mix/fader", db_to_fader(0), fmt=fmt_db)
        self.log(f"  check in Mixing Station that bus {bus} is NOT assigned to the main LR "
                 f"(you'd hear it dry) and that the FX{slot} return is.")

    # ---- backup / restore

    @staticmethod
    def backup_addresses():
        a = []
        for n in range(1, 33):
            c = ch(n)
            a += [f"{c}/config/{k}" for k in ("name", "color", "icon", "insrc")]
            a += [f"{c}/mix/on", f"{c}/mix/fader"]
            a += [f"{c}/gate/{k}" for k in ("on", "mode", "thr", "range", "attack", "hold", "release")]
            for b in range(1, 17):
                a += [f"{c}/mix/{b:02d}/on", f"{c}/mix/{b:02d}/level"]
        a += [f"/fx/{s}/type" for s in range(1, 9)]
        for s in range(1, 5):
            a += [f"/fx/{s}/source/l", f"/fx/{s}/source/r"]
        for r in range(1, 9):
            a += [f"/fxrtn/{r:02d}/mix/on", f"/fxrtn/{r:02d}/mix/fader"]
        for b in range(1, 17):
            a += [f"/bus/{b:02d}/mix/on", f"/bus/{b:02d}/mix/fader"]
        return a

    def backup(self, path):
        if not self.sock:
            raise X32Error("backup needs a connection")
        data, missing = {}, []
        for addr in self.backup_addresses():
            v = self.get(addr)
            if v is None:
                missing.append(addr)
            else:
                data[addr] = v
        Path(path).write_text(json.dumps({"saved": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "values": data}, indent=1))
        self.log(f"  saved {len(data)} values to {path}" +
                 (f" ({len(missing)} didn't answer, e.g. {missing[0]})" if missing else ""))

    def restore(self, path):
        if not self.sock:
            raise X32Error("restore needs a connection")
        values = json.loads(Path(path).read_text())["values"]
        changed = 0
        for addr, v in values.items():
            cur = self.get(addr)
            same = (abs(cur - v) < 1e-4) if isinstance(v, float) and isinstance(cur, float) else cur == v
            if not same:
                fmt = fmt_db if addr.endswith(("/fader", "/level")) else None
                self.set(addr, v, fmt=fmt)
                changed += 1
        self.log(f"  {changed} value(s) differ from the backup" + ("" if self.live else " (dry-run)"))

    # ---- overview

    def dump(self):
        rows = [f"{'CH':<4}{'NAME':<13}{'COLOR':<6}{'ICON':<5}{'INSRC':<6}{'MUTE':<5}{'FADER':<10}GATE"]
        for n in range(1, 33):
            c = ch(n)
            name = self.get(f"{c}/config/name")
            color = self.get(f"{c}/config/color")
            icon = self.get(f"{c}/config/icon")
            src = self.get(f"{c}/config/insrc")
            on = self.get(f"{c}/mix/on")
            fader = self.get(f"{c}/mix/fader")
            gate = self.get(f"{c}/gate/on")
            colr = COLORS[color] if isinstance(color, int) and 0 <= color < 16 else color
            rows.append(f"{n:<4}{(name or ''):<13}{str(colr):<6}{str(icon):<5}{str(src):<6}"
                        f"{('yes' if on == 0 else 'no' if on == 1 else '?'):<5}"
                        f"{(fmt_db(fader) if isinstance(fader, float) else '?'):<10}"
                        f"{'on' if gate == 1 else 'off' if gate == 0 else '?'}")
        return "\n".join(rows)


# -------------------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(description="Control a Behringer X32 over OSC (dry-run unless --live).")
    p.add_argument("--ip", help="console IP (default: X32_IP from .env)")
    p.add_argument("--live", action="store_true", help="actually send writes to the console")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="console model and firmware")
    sub.add_parser("dump", help="names, colors, sources, mutes, faders, gates for ch 1-32")
    s = sub.add_parser("get", help="read any OSC address"); s.add_argument("addr")
    s = sub.add_parser("set", help="write any OSC address"); s.add_argument("addr"); s.add_argument("value")

    s = sub.add_parser("name", help="set channel name"); s.add_argument("ch", type=int); s.add_argument("text")
    s = sub.add_parser("color", help=f"set channel color ({' '.join(COLORS)})")
    s.add_argument("ch", type=int); s.add_argument("color")
    s = sub.add_parser("source", help="set channel input source (0 = Local In 01)")
    s.add_argument("ch", type=int); s.add_argument("insrc", type=int)
    s = sub.add_parser("fader", help="set channel fader in dB"); s.add_argument("ch", type=int)
    s.add_argument("db", type=float)
    s = sub.add_parser("mute", help="mute a channel"); s.add_argument("ch", type=int)
    s = sub.add_parser("unmute", help="unmute a channel"); s.add_argument("ch", type=int)

    s = sub.add_parser("gate", help="turn on/adjust a channel's gate")
    s.add_argument("ch", type=int)
    s.add_argument("state", nargs="?", choices=["on", "off"])
    s.add_argument("--mode", help=f"one of {' '.join(GATE_MODES)}")
    s.add_argument("--thr", type=float, help="threshold, -80 to 0 dB")
    s.add_argument("--range", type=float, help="range, 3 to 60 dB")
    s.add_argument("--attack", type=float, help="attack, 0 to 120 ms")
    s.add_argument("--hold", type=float, help="hold, 0.02 to 2000 ms")
    s.add_argument("--release", type=float, help="release, 5 to 4000 ms")
    s = sub.add_parser("showgate", help="read a channel's gate settings"); s.add_argument("ch", type=int)

    s = sub.add_parser("send", help="set a channel's send to mix bus 1-16 in dB")
    s.add_argument("ch", type=int); s.add_argument("bus", type=int); s.add_argument("db", type=float)
    sub.add_parser("fx", help="list FX slots 1-8 and what feeds them")
    s = sub.add_parser("reverb", help="send a channel to the existing reverb, level in dB")
    s.add_argument("ch", type=int); s.add_argument("db", type=float)
    s.add_argument("--slot", type=int, help="pick a specific FX slot")
    s = sub.add_parser("setup-reverb", help="load a reverb into FX1-4, fed from a mix bus")
    s.add_argument("slot", type=int); s.add_argument("bus", type=int)
    s.add_argument("--type", default="HALL", help=f"one of {' '.join(FX_TYPES)}")
    s.add_argument("--force", action="store_true", help="overwrite a slot/bus that looks in use")

    s = sub.add_parser("backup", help="save current settings to a JSON file"); s.add_argument("file")
    s = sub.add_parser("restore", help="write back a backup (only values that differ)"); s.add_argument("file")
    s = sub.add_parser("undo", help="revert the last N live writes"); s.add_argument("n", type=int, nargs="?", default=1)
    sub.add_parser("history", help="show the live-write log")
    return p


WRITE_CMDS = {"set", "name", "color", "source", "fader", "mute", "unmute", "gate", "send",
              "reverb", "setup-reverb", "restore", "undo"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        x = X32(args.ip, live=args.live)
        if args.cmd in WRITE_CMDS:
            if not args.live:
                print("DRY RUN: nothing is sent. Add --live to apply.")
                if not x.sock:
                    print("No IP given, so current values show as '?'.")
            else:
                print(f"LIVE: writing to {x.ip}")
        need_conn = args.cmd in {"info", "dump", "get", "showgate", "fx", "backup"}
        if need_conn and not x.sock:
            raise X32Error("this command needs the console IP: set X32_IP in .env (see .env.example) or pass --ip")

        c = args.cmd
        if c == "info":
            info = x.info()
            if info is None:
                raise X32Error(f"no reply from {x.ip}. Check the IP and that you're on the same network.")
            print("Connected:", info)
        elif c == "dump":
            if x.info() is None:
                raise X32Error(f"no reply from {x.ip}. Check the IP and that you're on the same network.")
            print(x.dump())
        elif c == "get":
            print(f"{args.addr} = {x.get(args.addr)!r}")
        elif c == "set":
            x.set(args.addr, args.value)
        elif c == "name":
            x.set_name(args.ch, args.text)
        elif c == "color":
            x.set_color(args.ch, args.color)
        elif c == "source":
            x.set_source(args.ch, args.insrc)
        elif c == "fader":
            x.set_fader(args.ch, args.db)
        elif c in ("mute", "unmute"):
            x.mute(args.ch, c == "mute")
        elif c == "gate":
            on = None if args.state is None else args.state == "on"
            x.gate(args.ch, on=on, mode=args.mode, thr=args.thr, range=args.range,
                   attack=args.attack, hold=args.hold, release=args.release)
        elif c == "showgate":
            for k, v in x.read_gate(args.ch).items():
                print(f"  {k:<8}{v}")
        elif c == "send":
            x.send(args.ch, args.bus, args.db)
        elif c == "fx":
            for s in x.fx_slots():
                src = "insert only" if s["slot"] > 4 else f"{source_name(s['source_l'])}/{source_name(s['source_r'])}"
                print(f"  FX{s['slot']}  {s['type_name']:<6} source {src}")
        elif c == "reverb":
            x.reverb(args.ch, args.db, args.slot)
        elif c == "setup-reverb":
            x.setup_reverb(args.slot, args.bus, args.type, args.force)
        elif c == "backup":
            x.backup(args.file)
        elif c == "restore":
            x.restore(args.file)
        elif c == "undo":
            x.undo(args.n)
        elif c == "history":
            for e in x.read_history()[-30:]:
                print(f"  {e['t']}  {e['addr']}: {e['old']!r} -> {e['new']!r}")
    except X32Error as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
