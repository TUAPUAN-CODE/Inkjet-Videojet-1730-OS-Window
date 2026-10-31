"""Quick self-test for wsi.py:  python -m unittest test_wsi -v"""
import socket, threading, unittest
import wsi

GOOD = b"MHALG-101T LOT\x03\x02UUF1\nA3Z27\x03\x02UUF2\n66666"


class Fake:
    """Tiny fake printer: answers $XX to every packet. mode: keep | close"""
    def __init__(self, mode="keep", reply=None):
        self.mode, self.reply, self.got = mode, reply, []
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        while True:
            d = c.recv(256)
            if not d:
                break
            self.got.append(d)
            xx = f"{sum(d[1:-1]) & 0xFF:02X}"
            c.sendall(self.reply(d) if self.reply else b"\x02$" + xx.encode() + b"\x03")
            if self.mode == "close":
                break
        c.close()


class T(unittest.TestCase):
    def test_good_scan(self):
        data, pk = wsi.parse_scan(GOOD)
        self.assertEqual([p.label for p in pk], ["เลือกข้อความ", "UF1", "UF2"])
        self.assertEqual([p.value for p in pk], ["HALG-101T LOT", "A3Z27", "66666"])

    def test_literal_tokens_and_double_etx(self):
        txt = b"<STX>MHALG-101T LOT<ETX><STX>UUF1<LF>A3Z27<ETX><ETX>"
        _, pk = wsi.parse_scan(txt)
        self.assertEqual(len(pk), 2)

    def test_bad_scans(self):
        for bad in (b"MTEST1\x02UUF13333\x03",            # no ETX after M
                    b"\x02MTEST1\x03\x02UUF13333\x03",      # no LF
                    b"\x02K\x03",                           # stop-jet not allowed
                    b"\x02UUF1\n" + b"9" * 51 + b"\x03",   # too long
                    "\x02UUF1\nกขค\x03".encode()):           # non-ASCII
            with self.assertRaises(wsi.ProtocolError, msg=bad):
                wsi.parse_scan(bad)

    def test_send_keep_and_close(self):
        for mode in ("keep", "close"):
            f = Fake(mode)
            _, pk = wsi.parse_scan(GOOD)
            r = wsi.send_packets("127.0.0.1", f.port, pk, timeout=1)
            self.assertTrue(r.ok, r.message)
            self.assertEqual(len(f.got), 3)

    def test_reject_stops(self):
        f = Fake("keep", reply=lambda d: b"!" + f"{sum(d[1:-1]) & 0xFF:02X}".encode())
        _, pk = wsi.parse_scan(GOOD)
        r = wsi.send_packets("127.0.0.1", f.port, pk, timeout=1)
        self.assertFalse(r.ok)
        self.assertEqual(len(f.got), 1)

    def test_connection(self):
        f = Fake()
        self.assertTrue(wsi.test_connection("127.0.0.1", f.port)[0])
        self.assertFalse(wsi.test_connection("127.0.0.1", 1)[0])


if __name__ == "__main__":
    unittest.main()
