"""
wsi.py - Videojet 1000 Line "WSI" simple protocol helpers (Videojet 1710).

A scan from the QR code looks like this once STX/ETX are added:

    STX M<message name> ETX  STX U<field> LF <value> ETX  STX U<field> LF <value> ETX

e.g.  <STX>MHALG-101T LOT<ETX><STX>UUF1<LF>A3Z27<ETX><STX>UUF2<LF>66666<ETX>

The printer answers every packet with "$XX" (OK) or "!XX" (failed); XX is the
modulo-256 sum of the bytes between STX and ETX, as two hex digits.

Nothing in this file touches the GUI, so it can be tested on its own.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

STX = 0x02
ETX = 0x03
LF = 0x0A
CR = 0x0D

MAX_NAME_LEN = 30      # message name / user-field name
MAX_DATA_LEN = 50      # user-field value
DEFAULT_PORT = 3100    # WSI over TCP (3102 = head 2 on dual-head printers)
DEFAULT_ALLOWED = "MU"  # only "select message" and "update user field" are accepted from a QR

# Text that label software may print literally instead of a control character
TOKENS = {b"<STX>": b"\x02", b"<ETX>": b"\x03", b"<LF>": b"\n", b"<CR>": b"\r"}


class ProtocolError(ValueError):
    """The scanned data cannot be sent (message is shown to the operator, in Thai)."""


@dataclass
class Packet:
    body: bytes          # bytes between STX and ETX
    kind: str            # "M" or "U" (or another allowed letter)
    label: str           # what is shown in the table, e.g. "UF1" or "เลือกข้อความ"
    value: str


@dataclass
class SendResult:
    ok: bool
    message: str
    steps: List[Tuple[Packet, bool, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
def render_bytes(data: bytes) -> str:
    """Readable form of a scan: control characters shown as <STX> <ETX> <LF> <CR>."""
    out = []
    for c in data:
        if c == STX:
            out.append("<STX>")
        elif c == ETX:
            out.append("<ETX>")
        elif c == LF:
            out.append("<LF>")
        elif c == CR:
            out.append("<CR>")
        elif 0x20 <= c <= 0x7E:
            out.append(chr(c))
        else:
            out.append(f"<0x{c:02X}>")
    return "".join(out)


def checksum(body: bytes) -> str:
    return f"{sum(body) & 0xFF:02X}"


def is_text(b: bytes) -> bool:
    return all(0x20 <= c <= 0x7E for c in b)


def normalize_scan(raw: bytes) -> bytes:
    """Do what the scanner's Prefix/Suffix setting does: make sure the data is wrapped
    in STX ... ETX. Literal "<STX>"-style text is turned into the real control byte."""
    for tok, val in TOKENS.items():
        raw = raw.replace(tok, val)
    raw = raw.rstrip(bytes([CR, LF]))
    if not raw:
        raise ProtocolError("QR ว่างเปล่า")
    if raw[0] != STX:
        raw = bytes([STX]) + raw
    if raw[-1] != ETX:
        raw = raw + bytes([ETX])
    return raw


def split_packets(data: bytes) -> List[bytes]:
    """Split into packet bodies (bytes between STX and ETX). A doubled ETX is ignored."""
    packets: List[bytes] = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == ETX:
            i += 1
            continue
        if c != STX:
            raise ProtocolError(f"พบอักขระนอกกรอบคำสั่ง (ตำแหน่ง {i}): {render_bytes(data[i:i + 12])}")
        end = None
        for j in range(i + 1, n):
            if data[j] == ETX:
                end = j
                break
            if data[j] == STX:
                raise ProtocolError(
                    "ขาด <ETX> ปิดท้ายคำสั่ง ก่อนเริ่มคำสั่งถัดไป ใกล้ "
                    f"'{render_bytes(data[i:j])}'")
        if end is None:
            raise ProtocolError(f"คำสั่งไม่มี <ETX> ปิดท้าย: '{render_bytes(data[i:])}'")
        body = data[i + 1:end]
        if body:
            packets.append(body)
        i = end + 1
    if not packets:
        raise ProtocolError("ไม่พบคำสั่งใน QR")
    return packets


def validate_packet(body: bytes, allowed: str = DEFAULT_ALLOWED) -> Packet:
    kind = chr(body[0])
    if kind not in allowed:
        raise ProtocolError(f"คำสั่งชนิด '{kind}' ไม่อนุญาตให้ส่งจาก QR (อนุญาต: {allowed})")
    rest = body[1:]
    if kind == "M":
        if not (1 <= len(rest) <= MAX_NAME_LEN) or not is_text(rest):
            raise ProtocolError(f"ชื่อข้อความต้องยาว 1-{MAX_NAME_LEN} ตัวอักษร (ตัวอักษร/ตัวเลขภาษาอังกฤษ): '{render_bytes(rest)}'")
        return Packet(body, "M", "เลือกข้อความ", rest.decode("ascii"))
    if kind == "U":
        if rest.count(bytes([LF])) != 1:
            raise ProtocolError(
                "คำสั่ง U ต้องมี <LF> คั่นระหว่างชื่อ field กับค่า 1 ตัวพอดี "
                f"('{render_bytes(rest)}')")
        name, value = rest.split(bytes([LF]))
        if not (1 <= len(name) <= MAX_NAME_LEN) or not is_text(name):
            raise ProtocolError(f"ชื่อ field ต้องยาว 1-{MAX_NAME_LEN} ตัวอักษร: '{render_bytes(name)}'")
        if not (1 <= len(value) <= MAX_DATA_LEN) or not is_text(value):
            raise ProtocolError(
                f"ค่าของ field '{name.decode('ascii')}' ต้องยาว 1-{MAX_DATA_LEN} ตัวอักษร "
                f"(ภาษาอังกฤษ/ตัวเลขเท่านั้น): '{render_bytes(value)}'")
        return Packet(body, "U", name.decode("ascii"), value.decode("ascii"))
    if not is_text(rest.replace(bytes([LF]), b"")):
        raise ProtocolError(f"คำสั่งมีอักขระที่ไม่รองรับ: '{render_bytes(body)}'")
    return Packet(body, kind, f"คำสั่ง {kind}", render_bytes(rest))


def parse_scan(raw: bytes, allowed: str = DEFAULT_ALLOWED) -> Tuple[bytes, List[Packet]]:
    """Normalise and validate a whole scan. Returns (normalised bytes, packets).
    Raises ProtocolError if ANY packet is bad - nothing should be sent in that case."""
    data = normalize_scan(raw)
    packets = [validate_packet(b, allowed) for b in split_packets(data)]
    return data, packets


# --------------------------------------------------------------------------
def valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip())
        return True
    except ValueError:
        return False


def test_connection(ip: str, port: int, timeout: float = 2.0) -> Tuple[bool, str]:
    """Open and close a TCP connection. Sends nothing."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, "เชื่อมต่อเครื่อง Inkjet ได้"
    except socket.timeout:
        return False, "หมดเวลา - ไม่พบเครื่องที่ IP นี้ (ตรวจสาย LAN / IP / ตั้ง REMOTE เป็น Ethernet)"
    except ConnectionRefusedError:
        return False, "เครื่องปฏิเสธการเชื่อมต่อ (ตรวจ Port และการตั้ง REMOTE ที่เมนู CONFIGURE)"
    except OSError as e:
        return False, f"เชื่อมต่อไม่ได้: {e}"


def _read_reply(sock: socket.socket, timeout: float) -> Tuple[bytes, bool]:
    reply, closed = b"", False
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(256)
            if not chunk:
                closed = True
                break
            reply += chunk
            k = max(reply.find(b"$"), reply.find(b"!"))
            if k != -1 and len(reply) - k >= 3:     # marker + two hex digits
                break
    except socket.timeout:
        pass
    return reply, closed


def _interpret(reply: bytes, body: bytes) -> Tuple[Optional[bool], str]:
    text = bytes(c for c in reply if c not in (STX, ETX)).decode("ascii", "replace").strip()
    if not text:
        return None, "เครื่องไม่ตอบกลับ"
    expected = checksum(body)
    if text.startswith("$"):
        note = "" if text[1:3].upper() == expected else f" (checksum {text[1:3]} ไม่ตรงกับที่คาด {expected})"
        return True, f"สำเร็จ {text}{note}"
    if text.startswith("!"):
        return False, f"เครื่องปฏิเสธคำสั่ง {text}"
    return None, f"คำตอบที่ไม่คาดคิด: {text!r}"


def send_packets(ip: str, port: int, packets: List[Packet], timeout: float = 3.0) -> SendResult:
    """Send packets in order; stop at the first one that is not accepted.
    Reconnects when the printer closes the connection after answering."""
    steps: List[Tuple[Packet, bool, str]] = []
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        return SendResult(False, f"เชื่อมต่อเครื่องไม่ได้ ({ip}:{port}): {e}", steps)
    try:
        for pkt in packets:
            frame = bytes([STX]) + pkt.body + bytes([ETX])
            for attempt in (1, 2):
                try:
                    sock.sendall(frame)
                    reply, closed = _read_reply(sock, timeout)
                    dropped = False
                except (ConnectionResetError, BrokenPipeError):
                    reply, closed, dropped = b"", True, True    # printer had already closed the link
                if reply or (not closed and not dropped) or attempt == 2:
                    break
                sock.close()      # no answer because the link was closed: reconnect, send once more
                sock = socket.create_connection((ip, port), timeout=timeout)
            if dropped and not reply:
                raise ConnectionResetError("เครื่องตัดการเชื่อมต่อระหว่างส่ง")
            ok, text = _interpret(reply, pkt.body)
            steps.append((pkt, ok is True, text))
            if ok is not True:
                done = sum(1 for _, o, _ in steps if o)
                return SendResult(
                    False,
                    f"{pkt.label}: {text}  (ส่งสำเร็จ {done} จาก {len(packets)} คำสั่ง "
                    "คำสั่งที่เหลือไม่ได้ส่ง - ตรวจเครื่องก่อนสแกนใหม่)",
                    steps)
            if closed:
                sock.close()
                sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        return SendResult(False, f"การส่งข้อมูลผิดพลาด: {e}", steps)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return SendResult(True, f"ส่งสำเร็จ {len(packets)} คำสั่ง", steps)
