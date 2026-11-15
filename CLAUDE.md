# X32 mixer control

This folder controls the drummer's Behringer X32 rack over OSC with [x32.py](x32.py).
The user says what they want in plain English ("add a gate to the kick", "less reverb on
vocals"). Claude turns that into x32.py commands, runs them, and reports what changed.
The user monitors from Mixing Station on their iPhone, which shows changes live.

## Safety rules

- Writes go to a **live console**, often with a PA connected. Before the first live write of a
  session, run `backup` to a dated file, such as `backups/2026-11-14.json`, and remind the user to keep the PA
  low or muted while experimenting.
- Dry-run is the default. For a new kind of change, run it without `--live` first and show
  the user the before/after lines. Small follow-up tweaks to the same parameter ("tighter",
  "a bit more") can go straight to `--live`.
- Never use `--force` without asking. It exists to overwrite FX slots or buses that look in use,
  for example a bus feeding someone's in-ear mix.
- `undo N` reverts the last N live writes from `x32_history.jsonl`. `restore FILE` puts back a
  whole backup, and it dry-runs first unless `--live` is given.
- Claude can't hear the room. Ask the user how it sounds rather than guessing.

## Usage

The IP is on the rack's Setup → Network screen or in Mixing Station's connection settings.
`export X32_IP=...` saves typing `--ip`.

```
python3 x32.py info                       # check connection
python3 x32.py dump                       # all 32 channels at a glance
python3 x32.py showgate 1
python3 x32.py --live gate 1 on --thr -40 --range 40 --attack 1 --hold 50 --release 150
python3 x32.py --live name 1 "Kick" ; python3 x32.py --live color 1 RD
python3 x32.py --live fader 1 -5 ; python3 x32.py --live mute 1
python3 x32.py fx                         # what's in FX1-8 and which bus feeds each
python3 x32.py --live reverb 4 -12        # send ch 4 to the existing reverb at -12 dB
python3 x32.py --live setup-reverb 2 14 --type PLAT   # only if no reverb exists yet
python3 x32.py get /ch/01/eq/1/g          # any raw OSC address
python3 x32.py --live set /ch/01/eq/on 1
python3 x32.py history ; python3 x32.py --live undo 2
```

Tests run against a fake console: `python3 -m unittest discover tests`

## Gate starting points (adjust by ear)

- "Tighter" gate: raise the threshold a few dB and/or shorten the release.
- "Looser": lower the threshold and lengthen the release.
- Kick: thr -35, range 40, attack 0.5, hold 40, release 100
- Snare: thr -30, range 30, attack 0.5, hold 50, release 150
- Toms: thr -40, range 40, attack 1, hold 100, release 300

## Unverified parts

x32.py was written without access to the console. These come from the unofficial OSC docs
and must be checked against Mixing Station on first connection. Update this list once
they're confirmed:

- Gate unit mappings: threshold -80 to 0 dB linear, range 3 to 60 dB linear, attack 0 to
  120 ms linear, hold and release logarithmic. Set a known value and compare it with Mixing Station.
- FX type numbers (0 = HALL ... 9 = RVRS): compare the `fx` output with Mixing Station's FX page.
- FX source numbers (0 = INS, 1 to 16 = MIX01 to 16) and FX return addresses `/fxrtn/01` to `08`.
- Fader law: compare `dump` faders with Mixing Station.
