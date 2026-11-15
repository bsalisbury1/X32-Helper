"""Tests for x32.py against a fake console on localhost. Run: python3 -m unittest discover tests"""
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import x32  # noqa: E402


class FakeX32:
    """Answers queries and stores writes like the console does (floats quantized to 1024 steps)."""

    def __init__(self):
        self.state = {"/fx/1/type": 0, "/fx/1/source/l": 13, "/fx/1/source/r": 13,
                      "/ch/01/config/name": "Kick", "/ch/01/mix/on": 1}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def default(self, addr):
        leaf = addr.rsplit("/", 1)[-1]
        return "" if leaf in x32.STR_LEAVES else 0 if leaf in x32.INT_LEAVES else 0.0

    def _serve(self):
        while self.running:
            try:
                data, peer = self.sock.recvfrom(4096)
            except OSError:
                return
            addr, args = x32.decode(data)
            if addr == "/xinfo":
                self.sock.sendto(x32.encode("/xinfo", "127.0.0.1", "FAKE", "X32RACK", "4.06"), peer)
            elif args:
                v = args[0]
                self.state[addr] = round(v * 1023) / 1023 if isinstance(v, float) else v
            else:
                self.sock.sendto(x32.encode(addr, self.state.get(addr, self.default(addr))), peer)

    def close(self):
        self.running = False
        self.sock.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeX32()
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self.tmp.name) / "hist.jsonl"
        self.out = []

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def console(self, live):
        c = x32.X32("127.0.0.1", live=live, port=self.fake.port, timeout=0.2,
                    log=self.out.append, history=self.hist)
        self.addCleanup(c.close)
        return c


class TestEncoding(unittest.TestCase):
    def test_roundtrip(self):
        for args in [(), (1,), (0.25,), ("Kick",), ("a", 3, 0.5)]:
            addr, back = x32.decode(x32.encode("/ch/01/config/name", *args))
            self.assertEqual(addr, "/ch/01/config/name")
            self.assertEqual(tuple(back), args)

    def test_fader_law(self):
        for db in [10, 0, -5, -10, -20, -30, -45, -60, -75]:
            self.assertAlmostEqual(x32.fader_to_db(x32.db_to_fader(db)), db, delta=0.3)
        self.assertEqual(x32.db_to_fader(-200), 0.0)
        self.assertEqual(x32.db_to_fader(0), round(0.75 * 1023) / 1023)

    def test_gate_maps(self):
        for key, val in [("thr", -40), ("range", 30), ("attack", 10), ("hold", 50), ("release", 200)]:
            to_f, back, lo, hi, _ = x32.GATE_PARAMS[key]
            self.assertAlmostEqual(back(to_f(val, lo, hi), lo, hi), val, places=6)

    def test_coerce(self):
        self.assertEqual(x32.coerce("/ch/01/gate/on", "1"), 1)
        self.assertEqual(x32.coerce("/ch/01/gate/thr", "0.5"), 0.5)
        self.assertEqual(x32.coerce("/ch/01/config/name", 12), "12")


class TestConsole(Base):
    def test_dry_run_sends_nothing(self):
        c = self.console(live=False)
        c.gate(1, on=True, thr=-40)
        self.assertNotIn("/ch/01/gate/on", self.fake.state)
        self.assertFalse(self.hist.exists())
        self.assertTrue(any("dry-run" in line and "-40.0 dB" in line for line in self.out))

    def test_live_write_readback_and_undo(self):
        c = self.console(live=True)
        c.gate(1, on=True, thr=-40, release=120)
        self.assertEqual(self.fake.state["/ch/01/gate/on"], 1)
        self.assertAlmostEqual(self.fake.state["/ch/01/gate/thr"], 0.5, delta=0.001)
        self.assertEqual(len(c.read_history()), 3)
        c.undo(3)
        self.assertEqual(self.fake.state["/ch/01/gate/on"], 0)
        self.assertEqual(self.fake.state["/ch/01/gate/thr"], 0.0)
        self.assertEqual(c.read_history(), [])

    def test_name_and_mute(self):
        c = self.console(live=True)
        c.set_name(2, "Snare Top")
        c.set_color(2, "rdi")
        c.mute(2)
        self.assertEqual(self.fake.state["/ch/02/config/name"], "Snare Top")
        self.assertEqual(self.fake.state["/ch/02/config/color"], x32.COLORS.index("RDi"))
        self.assertEqual(self.fake.state["/ch/02/mix/on"], 0)

    def test_reverb_uses_existing_slot(self):
        c = self.console(live=True)
        c.reverb(1, -10)
        self.assertAlmostEqual(self.fake.state["/ch/01/mix/13/level"], x32.db_to_fader(-10), delta=0.001)
        self.assertEqual(self.fake.state["/ch/01/mix/13/on"], 1)

    def test_setup_reverb_refuses_busy_bus(self):
        self.fake.state.update({"/ch/05/mix/07/on": 1, "/ch/05/mix/07/level": 0.6})
        c = self.console(live=True)
        with self.assertRaises(x32.X32Error):
            c.setup_reverb(2, 7)
        self.assertNotIn("/fx/2/source/l", self.fake.state)

    def test_setup_reverb(self):
        c = self.console(live=True)
        c.setup_reverb(2, 14, "PLAT")
        self.assertEqual(self.fake.state["/fx/2/type"], x32.FX_TYPES.index("PLAT"))
        self.assertEqual(self.fake.state["/fx/2/source/l"], 14)
        self.assertEqual(self.fake.state["/fxrtn/03/mix/on"], 1)
        self.assertEqual(self.fake.state["/fxrtn/04/mix/on"], 1)

    def test_backup_restore(self):
        path = Path(self.tmp.name) / "b.json"
        c = self.console(live=True)
        c.backup(path)
        self.assertEqual(json.loads(path.read_text())["values"]["/ch/01/config/name"], "Kick")
        c.set_name(1, "Oops")
        c.set_fader(1, -5)
        c.restore(path)
        self.assertEqual(self.fake.state["/ch/01/config/name"], "Kick")
        self.assertEqual(self.fake.state["/ch/01/mix/fader"], 0.0)

    def test_dump(self):
        table = self.console(live=False).dump()
        self.assertIn("Kick", table)
        self.assertEqual(len(table.splitlines()), 33)


class TestCLI(unittest.TestCase):
    def test_dry_run_without_ip(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            x32.main(["gate", "1", "on", "--thr", "-40"])
        self.assertIn("DRY RUN", buf.getvalue())
        self.assertIn("/ch/01/gate/thr: ? -> -40.0 dB", buf.getvalue())

    def test_live_without_ip_errors(self):
        with self.assertRaises(SystemExit):
            x32.main(["--live", "mute", "1"])


if __name__ == "__main__":
    unittest.main()
