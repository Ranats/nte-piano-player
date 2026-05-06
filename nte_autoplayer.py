#!/usr/bin/env python3
"""Convert MIDI/MusicXML into keyboard input for the NTE in-game piano.

The default keymap is inferred from the screenshot:

    high: Q W E R T Y U
    mid:  A S D F G H J
    low:  Z X C V B N M

Chromatic notes use the game's long-hold modifiers:
    sharp degrees: Shift + 1/4/5
    flat degrees:  Ctrl  + 3/7

This tool intentionally uses normal foreground keyboard input only. It does
not inspect or modify the game process.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import os
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


NOTE_BASES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
PC_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]


@dataclass(frozen=True)
class NoteEvent:
    start: float
    duration: float
    pitch: int
    velocity: int = 64
    source: str = ""


@dataclass(frozen=True)
class KeyAction:
    note: NoteEvent
    key: str
    modifier: str | None
    octave_row: int
    degree: str


@dataclass(frozen=True)
class PlayEvent:
    at: float
    kind: str
    action: KeyAction


class ParseError(ValueError):
    pass


def parse_note_name(value: str) -> int:
    text = value.strip()
    if not text:
        raise ValueError("empty note name")
    step = text[0].upper()
    if step not in NOTE_BASES:
        raise ValueError(f"unknown note step: {value}")
    i = 1
    accidental = 0
    while i < len(text) and text[i] in "#bB":
        accidental += 1 if text[i] == "#" else -1
        i += 1
    octave_text = text[i:]
    if not octave_text or not octave_text.lstrip("-").isdigit():
        raise ValueError(f"note must include octave, for example C3: {value}")
    octave = int(octave_text)
    return (octave + 1) * 12 + NOTE_BASES[step] + accidental


def note_name(pitch: int) -> str:
    return f"{PC_NAMES[pitch % 12]}{pitch // 12 - 1}"


class MidiReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ParseError("unexpected end of MIDI data")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def read_u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def read_u32(self) -> int:
        return struct.unpack(">I", self.read(4))[0]

    def read_vlq(self) -> int:
        value = 0
        for _ in range(4):
            b = self.read(1)[0]
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value
        raise ParseError("MIDI VLQ is too long")


def _event_data_length(status: int) -> int:
    high = status & 0xF0
    if high in (0xC0, 0xD0):
        return 1
    if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
        return 2
    raise ParseError(f"unsupported MIDI status 0x{status:02X}")


def _parse_midi_track(data: bytes) -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int]]]:
    reader = MidiReader(data)
    abs_tick = 0
    running_status: int | None = None
    channel_events: list[tuple[int, int, int, int]] = []
    tempos: list[tuple[int, int]] = []

    while reader.pos < len(data):
        abs_tick += reader.read_vlq()
        first = reader.read(1)[0]

        if first == 0xFF:
            meta_type = reader.read(1)[0]
            length = reader.read_vlq()
            payload = reader.read(length)
            if meta_type == 0x2F:
                break
            if meta_type == 0x51 and length == 3:
                tempos.append((abs_tick, int.from_bytes(payload, "big")))
            running_status = None
            continue

        if first in (0xF0, 0xF7):
            length = reader.read_vlq()
            reader.read(length)
            running_status = None
            continue

        if first & 0x80:
            status = first
            running_status = status
            needed = _event_data_length(status)
            payload = list(reader.read(needed))
        else:
            if running_status is None:
                raise ParseError("MIDI running status used before status byte")
            status = running_status
            needed = _event_data_length(status)
            payload = [first] + list(reader.read(needed - 1))

        event_type = status & 0xF0
        channel = status & 0x0F
        if event_type in (0x80, 0x90):
            note = payload[0]
            velocity = payload[1]
            kind = 0x80 if event_type == 0x80 or velocity == 0 else 0x90
            channel_events.append((abs_tick, kind | channel, note, velocity))

    return channel_events, tempos


def _tick_to_seconds_fn(tempo_events: list[tuple[int, int]], ticks_per_quarter: int):
    clean = [(0, 500000)]
    clean.extend((tick, tempo) for tick, tempo in tempo_events if tick >= 0 and tempo > 0)
    by_tick: dict[int, int] = {}
    for tick, tempo in clean:
        by_tick[tick] = tempo
    tempos = sorted(by_tick.items())

    def convert(target_tick: int) -> float:
        seconds = 0.0
        last_tick = 0
        current_tempo = 500000
        for tick, tempo in tempos:
            if tick > target_tick:
                break
            seconds += (tick - last_tick) * current_tempo / 1_000_000.0 / ticks_per_quarter
            last_tick = tick
            current_tempo = tempo
        seconds += (target_tick - last_tick) * current_tempo / 1_000_000.0 / ticks_per_quarter
        return seconds

    return convert


def load_midi(path: Path) -> list[NoteEvent]:
    reader = MidiReader(path.read_bytes())
    if reader.read(4) != b"MThd":
        raise ParseError("missing MIDI header")
    header_len = reader.read_u32()
    header = MidiReader(reader.read(header_len))
    midi_format = header.read_u16()
    track_count = header.read_u16()
    division = header.read_u16()
    if division & 0x8000:
        raise ParseError("SMPTE MIDI timing is not supported")
    ticks_per_quarter = division
    if midi_format not in (0, 1):
        raise ParseError(f"unsupported MIDI format: {midi_format}")

    raw_events: list[tuple[int, int, int, int]] = []
    tempo_events: list[tuple[int, int]] = []
    for _ in range(track_count):
        chunk_type = reader.read(4)
        chunk_len = reader.read_u32()
        payload = reader.read(chunk_len)
        if chunk_type != b"MTrk":
            continue
        events, tempos = _parse_midi_track(payload)
        raw_events.extend(events)
        tempo_events.extend(tempos)

    tick_to_seconds = _tick_to_seconds_fn(tempo_events, ticks_per_quarter)
    active: dict[tuple[int, int], list[tuple[int, int]]] = collections.defaultdict(list)
    notes: list[NoteEvent] = []
    for tick, status, pitch, velocity in sorted(raw_events, key=lambda item: (item[0], item[1])):
        channel = status & 0x0F
        kind = status & 0xF0
        key = (channel, pitch)
        if kind == 0x90:
            active[key].append((tick, velocity))
        elif active[key]:
            start_tick, start_velocity = active[key].pop(0)
            start = tick_to_seconds(start_tick)
            end = tick_to_seconds(tick)
            if end > start:
                notes.append(NoteEvent(start, end - start, pitch, start_velocity, "midi"))

    return sorted(notes, key=lambda n: (n.start, n.pitch))


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _strip_ns(child.tag) == name:
            return child
    return None


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in element:
        if _strip_ns(child.tag) == name:
            yield child


def _text(element: ET.Element | None, default: str = "") -> str:
    return default if element is None or element.text is None else element.text.strip()


def _load_xml_root(path: Path) -> ET.Element:
    if path.suffix.lower() == ".mxl":
        with zipfile.ZipFile(path) as archive:
            if "META-INF/container.xml" in archive.namelist():
                with archive.open("META-INF/container.xml") as handle:
                    container = ET.parse(handle).getroot()
                for item in container.iter():
                    if _strip_ns(item.tag) == "rootfile":
                        full_path = item.attrib.get("full-path")
                        if full_path:
                            with archive.open(full_path) as score_handle:
                                return ET.parse(score_handle).getroot()
            candidates = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".xml", ".musicxml"))
                and not name.upper().startswith("META-INF/")
            ]
            if not candidates:
                raise ParseError("MXL archive does not contain MusicXML")
            with archive.open(candidates[0]) as handle:
                return ET.parse(handle).getroot()
    return ET.parse(path).getroot()


def _musicxml_pitch(note: ET.Element) -> int | None:
    pitch = _child(note, "pitch")
    if pitch is None:
        return None
    step = _text(_child(pitch, "step")).upper()
    if step not in NOTE_BASES:
        return None
    alter = int(float(_text(_child(pitch, "alter"), "0")))
    octave = int(_text(_child(pitch, "octave")))
    return (octave + 1) * 12 + NOTE_BASES[step] + alter


def _musicxml_tie_flags(note: ET.Element) -> tuple[bool, bool]:
    starts = False
    stops = False
    for item in note.iter():
        tag = _strip_ns(item.tag)
        if tag not in ("tie", "tied"):
            continue
        tie_type = item.attrib.get("type", "")
        starts = starts or tie_type in ("start", "continue")
        stops = stops or tie_type in ("stop", "continue")
    return starts, stops


def load_musicxml(path: Path, default_bpm: float = 120.0) -> list[NoteEvent]:
    root = _load_xml_root(path)
    notes: list[NoteEvent] = []
    seconds_per_quarter = 60.0 / default_bpm

    for part in _children(root, "part"):
        divisions = 1
        cursor_seconds = 0.0
        previous_start_seconds = 0.0
        part_notes: list[NoteEvent] = []
        active_ties: dict[int, NoteEvent] = {}

        for measure in _children(part, "measure"):
            for item in measure:
                tag = _strip_ns(item.tag)
                if tag == "attributes":
                    div_text = _text(_child(item, "divisions"))
                    if div_text:
                        divisions = max(1, int(float(div_text)))
                elif tag == "direction":
                    sound = _child(item, "sound")
                    tempo_text = sound.attrib.get("tempo", "") if sound is not None else ""
                    if tempo_text:
                        bpm = float(tempo_text)
                        if bpm > 0:
                            seconds_per_quarter = 60.0 / bpm
                elif tag in ("backup", "forward"):
                    duration_divs = float(_text(_child(item, "duration"), "0") or 0)
                    duration_quarters = duration_divs / divisions if divisions else 0.0
                    offset = duration_quarters * seconds_per_quarter
                    if tag == "backup":
                        cursor_seconds = max(0.0, cursor_seconds - offset)
                    else:
                        cursor_seconds += offset
                    continue
                elif tag != "note":
                    continue

                duration_divs = float(_text(_child(item, "duration"), "0") or 0)
                duration_quarters = duration_divs / divisions if divisions else 0.0
                duration_seconds = duration_quarters * seconds_per_quarter
                is_chord = _child(item, "chord") is not None
                is_rest = _child(item, "rest") is not None
                start_seconds = previous_start_seconds if is_chord else cursor_seconds
                pitch = _musicxml_pitch(item)

                if pitch is not None and not is_rest and duration_seconds > 0:
                    event = NoteEvent(
                        start_seconds,
                        duration_seconds,
                        pitch,
                        64,
                        "musicxml",
                    )
                    tie_start, tie_stop = _musicxml_tie_flags(item)
                    if tie_stop and pitch in active_ties:
                        previous = active_ties[pitch]
                        merged_end = max(
                            previous.start + previous.duration,
                            event.start + event.duration,
                        )
                        merged = NoteEvent(
                            previous.start,
                            merged_end - previous.start,
                            previous.pitch,
                            previous.velocity,
                            previous.source,
                        )
                        if tie_start:
                            active_ties[pitch] = merged
                        else:
                            part_notes.append(merged)
                            del active_ties[pitch]
                    elif tie_start:
                        active_ties[pitch] = event
                    else:
                        part_notes.append(event)

                if not is_chord:
                    previous_start_seconds = cursor_seconds
                    cursor_seconds += duration_seconds

        part_notes.extend(active_ties.values())
        notes.extend(part_notes)

    return sorted(notes, key=lambda n: (n.start, n.pitch))


class NteKeymap:
    ROW_KEYS = [
        list("zxcvbnm"),
        list("asdfghj"),
        list("qwertyu"),
    ]
    DEGREE_MAP = {
        0: (0, None, "1"),
        1: (0, "shift", "#1"),
        2: (1, None, "2"),
        3: (2, "ctrl", "b3"),
        4: (2, None, "3"),
        5: (3, None, "4"),
        6: (3, "shift", "#4"),
        7: (4, None, "5"),
        8: (4, "shift", "#5"),
        9: (5, None, "6"),
        10: (6, "ctrl", "b7"),
        11: (6, None, "7"),
    }

    def __init__(self, lowest_note: int = 48) -> None:
        self.lowest_note = lowest_note

    def map_note(self, note: NoteEvent) -> KeyAction | None:
        rel = note.pitch - self.lowest_note
        if rel < 0 or rel >= 36:
            return None
        row = rel // 12
        pc = rel % 12
        key_index, modifier, degree = self.DEGREE_MAP[pc]
        return KeyAction(note, self.ROW_KEYS[row][key_index], modifier, row, degree)


def load_score(path: Path) -> list[NoteEvent]:
    suffix = path.suffix.lower()
    if suffix in (".mid", ".midi"):
        return load_midi(path)
    if suffix in (".musicxml", ".xml", ".mxl"):
        return load_musicxml(path)
    raise ParseError(f"unsupported input type: {path.suffix}")


def transform_notes(notes: Iterable[NoteEvent], transpose: int, tempo_scale: float) -> list[NoteEvent]:
    if tempo_scale <= 0:
        raise ValueError("--tempo-scale must be greater than zero")
    transformed: list[NoteEvent] = []
    for note in notes:
        transformed.append(
            NoteEvent(
                start=note.start / tempo_scale,
                duration=max(0.03, note.duration / tempo_scale),
                pitch=note.pitch + transpose,
                velocity=note.velocity,
                source=note.source,
            )
        )
    return sorted(transformed, key=lambda n: (n.start, n.pitch))


def fit_notes_to_range(
    notes: Iterable[NoteEvent], lowest_note: int, range_mode: str
) -> tuple[list[NoteEvent], list[str]]:
    if range_mode not in ("skip", "octave-fold"):
        raise ValueError("--range-mode must be skip or octave-fold")
    note_list = list(notes)
    if range_mode == "skip":
        return note_list, []

    highest_note = lowest_note + 35
    fitted: list[NoteEvent] = []
    folded = 0
    for note in note_list:
        pitch = note.pitch
        original = pitch
        while pitch < lowest_note:
            pitch += 12
        while pitch > highest_note:
            pitch -= 12
        if pitch != original:
            folded += 1
        fitted.append(
            NoteEvent(note.start, note.duration, pitch, note.velocity, note.source)
        )

    warnings = []
    if folded:
        warnings.append(
            f"octave-folded {folded} notes into {note_name(lowest_note)}..{note_name(highest_note)}"
        )
    return sorted(fitted, key=lambda n: (n.start, n.pitch)), warnings


def map_actions(notes: Iterable[NoteEvent], keymap: NteKeymap) -> tuple[list[KeyAction], list[str]]:
    actions: list[KeyAction] = []
    warnings: list[str] = []
    skipped = 0
    for note in notes:
        action = keymap.map_note(note)
        if action is None:
            skipped += 1
            continue
        actions.append(action)
    if skipped:
        warnings.append(f"skipped {skipped} notes outside the 3-octave keymap")
    warnings.extend(_overlap_warnings(actions))
    return actions, warnings


def _overlap_warnings(actions: list[KeyAction]) -> list[str]:
    by_key: dict[str, list[KeyAction]] = collections.defaultdict(list)
    for action in actions:
        by_key[action.key].append(action)
    warnings: list[str] = []
    for key, key_actions in by_key.items():
        ordered = sorted(key_actions, key=lambda a: (a.note.start, a.note.start + a.note.duration))
        last_end = -1.0
        for action in ordered:
            start = action.note.start
            if start < last_end - 0.001:
                warnings.append(
                    f"physical key overlap on {key.upper()} near {start:.3f}s; dense chromatic chords may collapse"
                )
                break
            last_end = max(last_end, start + action.note.duration)
    return warnings


def build_play_events(actions: Iterable[KeyAction]) -> list[PlayEvent]:
    events: list[PlayEvent] = []
    for action in actions:
        events.append(PlayEvent(action.note.start, "down", action))
        events.append(PlayEvent(action.note.start + action.note.duration, "up", action))
    return sorted(events, key=lambda e: (e.at, 0 if e.kind == "up" else 1, e.action.key))


def summarize(actions: list[KeyAction], warnings: list[str]) -> str:
    if actions:
        end = max(a.note.start + a.note.duration for a in actions)
        start = min(a.note.start for a in actions)
        duration = end - start
    else:
        duration = 0.0
    lines = [
        f"mapped_notes={len(actions)}",
        f"duration={duration:.2f}s",
        "range="
        + (
            f"{note_name(min(a.note.pitch for a in actions))}..{note_name(max(a.note.pitch for a in actions))}"
            if actions
            else "empty"
        ),
    ]
    lines.extend(f"warning: {warning}" for warning in warnings)
    return "\n".join(lines)


def print_dry_run(actions: list[KeyAction], warnings: list[str], verbose: bool) -> None:
    print(summarize(actions, warnings))
    if not verbose:
        print("Use --verbose to print each mapped note.")
        return
    for action in actions:
        mod = f"{action.modifier}+" if action.modifier else ""
        print(
            f"{action.note.start:8.3f}s  {action.note.duration:7.3f}s  "
            f"{note_name(action.note.pitch):4s} -> {mod}{action.key.upper()} ({action.degree})"
        )


if os.name == "nt":
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]


class WindowsKeyboard:
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    MAPVK_VK_TO_VSC = 0
    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_ESCAPE = 0x1B
    MODIFIERS = {"shift": VK_SHIFT, "ctrl": VK_CONTROL}

    def __init__(self, modifier_hold: float = 0.02, input_mode: str = "scan") -> None:
        if os.name != "nt":
            raise RuntimeError("play mode is only supported on Windows")
        if input_mode not in ("scan", "vk"):
            raise ValueError("input_mode must be 'scan' or 'vk'")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = ctypes.c_uint
        self.user32.MapVirtualKeyW.argtypes = (ctypes.c_uint, ctypes.c_uint)
        self.user32.MapVirtualKeyW.restype = ctypes.c_uint
        self.user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
        self.user32.VkKeyScanW.restype = ctypes.c_short
        self.modifier_hold = modifier_hold
        self.input_mode = input_mode

    def vk_for_key(self, key: str) -> int:
        result = self.user32.VkKeyScanW(key.upper())
        if result == -1:
            raise RuntimeError(f"cannot map key: {key}")
        return result & 0xFF

    def _send_vk(self, vk: int, down: bool, label: str = "") -> None:
        flags = 0 if down else self.KEYEVENTF_KEYUP
        scan = 0
        if self.input_mode == "scan":
            scan = int(self.user32.MapVirtualKeyW(vk, self.MAPVK_VK_TO_VSC))
            if scan == 0:
                raise RuntimeError(f"cannot map virtual key 0x{vk:02X} to scancode")
            flags |= self.KEYEVENTF_SCANCODE
            send_vk = 0
        else:
            send_vk = vk
        event = INPUT(
            self.INPUT_KEYBOARD,
            INPUT_UNION(ki=KEYBDINPUT(send_vk, scan, flags, 0, 0)),
        )
        ctypes.set_last_error(0)
        sent = self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
        if sent != 1:
            error = ctypes.get_last_error()
            try:
                message = ctypes.FormatError(error).strip() if error else "no extended error"
            except Exception:
                message = "unknown error"
            target = label or f"vk=0x{vk:02X}"
            direction = "down" if down else "up"
            raise RuntimeError(
                "SendInput failed "
                f"({target} {direction}, mode={self.input_mode}, win_error={error}: {message}, "
                f"input_size={ctypes.sizeof(INPUT)})"
            )

    def key_down(self, action: KeyAction) -> None:
        mod_vk = self.MODIFIERS.get(action.modifier or "")
        try:
            if mod_vk:
                self._send_vk(mod_vk, True, action.modifier or "modifier")
            self._send_vk(self.vk_for_key(action.key), True, action.key.upper())
            if mod_vk:
                time.sleep(self.modifier_hold)
        finally:
            if mod_vk:
                self._send_vk(mod_vk, False, action.modifier or "modifier")

    def key_up(self, action: KeyAction) -> None:
        self._send_vk(self.vk_for_key(action.key), False, action.key.upper())

    def escape_pressed(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(self.VK_ESCAPE) & 0x8000)

    def foreground_title(self) -> str:
        hwnd = self.user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value


def play(
    actions: list[KeyAction],
    require_title: str,
    lead_in: int,
    modifier_hold: float,
    input_mode: str,
) -> None:
    keyboard = WindowsKeyboard(modifier_hold=modifier_hold, input_mode=input_mode)

    def ensure_foreground() -> None:
        if require_title:
            title = keyboard.foreground_title()
            if require_title.lower() not in title.lower():
                raise RuntimeError(
                    f"foreground window title does not contain {require_title!r}: {title!r}"
                )

    for remaining in range(lead_in, 0, -1):
        print(f"Starting in {remaining}... focus NTE piano now. Press Esc to abort.")
        time.sleep(1)
        if keyboard.escape_pressed():
            print("Aborted before playback.")
            return

    ensure_foreground()
    events = build_play_events(actions)
    start = time.perf_counter()
    active: dict[str, int] = collections.defaultdict(int)
    try:
        for event in events:
            while True:
                now = time.perf_counter() - start
                wait = event.at - now
                if wait <= 0:
                    break
                if keyboard.escape_pressed():
                    raise KeyboardInterrupt
                ensure_foreground()
                time.sleep(min(wait, 0.01))

            ensure_foreground()
            key_id = event.action.key
            if event.kind == "down":
                if active[key_id]:
                    continue
                active[key_id] += 1
                keyboard.key_down(event.action)
            else:
                if active[key_id]:
                    keyboard.key_up(event.action)
                    active[key_id] = max(0, active[key_id] - 1)
    except KeyboardInterrupt:
        print("Aborted by Esc.")
    finally:
        for key in list(active):
            if active[key]:
                try:
                    keyboard._send_vk(keyboard.vk_for_key(key), False, key.upper())
                except Exception:
                    pass
        for vk in (keyboard.VK_SHIFT, keyboard.VK_CONTROL):
            try:
                keyboard._send_vk(vk, False, "modifier")
            except Exception:
                pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert MIDI/MusicXML into NTE in-game piano keyboard input."
    )
    parser.add_argument("input", type=Path, help="MIDI (.mid/.midi) or MusicXML (.xml/.musicxml/.mxl)")
    parser.add_argument("--play", action="store_true", help="send real keyboard input; default is dry-run")
    parser.add_argument("--require-title", default="", help="required foreground window title fragment for --play")
    parser.add_argument(
        "--allow-any-window",
        action="store_true",
        help="allow --play without a foreground title guard; not recommended",
    )
    parser.add_argument("--lowest-note", default="C3", help="pitch mapped to low row degree 1, default C3")
    parser.add_argument("--transpose", type=int, default=0, help="transpose input by semitones before mapping")
    parser.add_argument(
        "--range-mode",
        choices=("skip", "octave-fold"),
        default="skip",
        help="skip out-of-range notes, or fold them by octave into the 3-octave NTE keyboard",
    )
    parser.add_argument("--tempo-scale", type=float, default=1.0, help="1.0 original, 0.5 half speed, 2.0 double speed")
    parser.add_argument("--lead-in", type=int, default=3, help="countdown seconds before --play")
    parser.add_argument("--modifier-hold-ms", type=float, default=20.0, help="how long to hold Shift/Ctrl after key-down")
    parser.add_argument(
        "--input-mode",
        choices=("scan", "vk"),
        default="scan",
        help="SendInput mode for --play; scan is usually better for games, vk is the legacy mode",
    )
    parser.add_argument("--verbose", action="store_true", help="print each mapped note in dry-run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2
    try:
        notes = load_score(args.input)
        notes = transform_notes(notes, args.transpose, args.tempo_scale)
        lowest_note = parse_note_name(args.lowest_note)
        notes, fit_warnings = fit_notes_to_range(notes, lowest_note, args.range_mode)
        actions, warnings = map_actions(notes, NteKeymap(lowest_note))
        warnings = fit_warnings + warnings
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not actions:
        print("error: no playable notes after mapping", file=sys.stderr)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 1

    if not args.play:
        print_dry_run(actions, warnings, args.verbose)
        return 0

    if not args.require_title and not args.allow_any_window:
        print(
            "error: --play requires --require-title, or explicitly pass --allow-any-window",
            file=sys.stderr,
        )
        return 2

    print(summarize(actions, warnings))
    try:
        play(
            actions,
            require_title=args.require_title,
            lead_in=max(0, args.lead_in),
            modifier_hold=max(0.0, args.modifier_hold_ms / 1000.0),
            input_mode=args.input_mode,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
