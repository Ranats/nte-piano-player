import importlib.util
import contextlib
import io
import ctypes
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nte_autoplayer", ROOT / "nte_autoplayer.py")
nte = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = nte
SPEC.loader.exec_module(nte)


def vlq(value):
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(parts))


def midi_file(track_payload, division=480):
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, division)
    track = b"MTrk" + struct.pack(">I", len(track_payload)) + track_payload
    return header + track


class NteAutoplayerTests(unittest.TestCase):
    def test_keymap_uses_modifiers_for_chromatic_degrees(self):
        keymap = nte.NteKeymap(nte.parse_note_name("C3"))
        cases = {
            "C3": ("z", None, "1"),
            "C#3": ("z", "shift", "#1"),
            "Eb4": ("d", "ctrl", "b3"),
            "F#5": ("r", "shift", "#4"),
            "Bb5": ("u", "ctrl", "b7"),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                action = keymap.map_note(nte.NoteEvent(0.0, 0.5, nte.parse_note_name(name)))
                self.assertIsNotNone(action)
                self.assertEqual((action.key, action.modifier, action.degree), expected)

    def test_midi_loader_reads_tempo_and_note_duration(self):
        payload = b"".join(
            [
                vlq(0),
                b"\xff\x51\x03\x07\xa1\x20",  # 120 bpm
                vlq(0),
                bytes([0x90, 60, 100]),
                vlq(480),
                bytes([0x80, 60, 0]),
                vlq(0),
                b"\xff\x2f\x00",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "one.mid"
            path.write_bytes(midi_file(payload))
            notes = nte.load_midi(path)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pitch, 60)
        self.assertAlmostEqual(notes[0].start, 0.0, places=3)
        self.assertAlmostEqual(notes[0].duration, 0.5, places=3)

    def test_musicxml_loader_reads_chord_at_same_start(self):
        xml = """<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <direction><sound tempo="120"/></direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    <note><chord/><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration></note>
    <note><rest/><duration>1</duration></note>
    <note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chord.musicxml"
            path.write_text(xml, encoding="utf-8")
            notes = nte.load_musicxml(path)
        self.assertEqual([n.pitch for n in notes], [60, 64, 67])
        self.assertAlmostEqual(notes[0].start, notes[1].start, places=3)
        self.assertAlmostEqual(notes[2].start, 1.0, places=3)

    def test_musicxml_tempo_change_does_not_retime_previous_notes(self):
        xml = """<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <direction><sound tempo="120"/></direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
    <direction><sound tempo="60"/></direction>
    <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration></note>
    <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tempo.musicxml"
            path.write_text(xml, encoding="utf-8")
            notes = nte.load_musicxml(path)
        self.assertAlmostEqual(notes[0].start, 0.0, places=3)
        self.assertAlmostEqual(notes[1].start, 0.5, places=3)
        self.assertAlmostEqual(notes[2].start, 1.5, places=3)

    def test_musicxml_backup_allows_parallel_voice(self):
        xml = """<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <direction><sound tempo="120"/></direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>
    <backup><duration>2</duration></backup>
    <note><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration></note>
  </measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "backup.musicxml"
            path.write_text(xml, encoding="utf-8")
            notes = nte.load_musicxml(path)
        self.assertEqual([n.pitch for n in notes], [60, 64])
        self.assertAlmostEqual(notes[0].start, 0.0, places=3)
        self.assertAlmostEqual(notes[1].start, 0.0, places=3)

    def test_mxl_uses_container_rootfile(self):
        container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="score/main.musicxml"/></rootfiles>
</container>"""
        score = """<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "score.mxl"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("META-INF/container.xml", container)
                archive.writestr("score/main.musicxml", score)
            notes = nte.load_musicxml(path)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pitch, 60)

    def test_musicxml_tied_notes_merge_into_single_note(self):
        xml = """<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions></attributes>
    <direction><sound tempo="120"/></direction>
    <note>
      <pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <tie type="start"/><notations><tied type="start"/></notations>
    </note>
    <note>
      <pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <tie type="stop"/><notations><tied type="stop"/></notations>
    </note>
  </measure></part>
</score-partwise>"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tie.musicxml"
            path.write_text(xml, encoding="utf-8")
            notes = nte.load_musicxml(path)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pitch, 60)
        self.assertAlmostEqual(notes[0].start, 0.0, places=3)
        self.assertAlmostEqual(notes[0].duration, 1.0, places=3)

    def test_octave_fold_fits_notes_into_three_octaves(self):
        notes = [
            nte.NoteEvent(0.0, 0.2, nte.parse_note_name("G1")),
            nte.NoteEvent(0.1, 0.2, nte.parse_note_name("C4")),
            nte.NoteEvent(0.2, 0.2, nte.parse_note_name("Eb6")),
        ]
        fitted, warnings = nte.fit_notes_to_range(
            notes, nte.parse_note_name("C3"), "octave-fold"
        )
        self.assertEqual([n.pitch for n in fitted], [55, 60, 75])
        self.assertTrue(any("octave-folded 2 notes" in warning for warning in warnings))

    def test_play_requires_title_guard_by_default(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = nte.main(
                [
                    str(ROOT / "scores" / "twinkle-twinkle.musicxml"),
                    "--play",
                    "--lead-in",
                    "0",
                ]
            )
        self.assertEqual(result, 2)

    @unittest.skipUnless(os.name == "nt", "Windows ctypes structures only exist on Windows")
    def test_windows_input_structure_matches_sendinput_size(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(nte.INPUT), expected)

    def test_out_of_range_notes_are_warned_and_skipped(self):
        notes = [
            nte.NoteEvent(0.0, 0.2, nte.parse_note_name("B2")),
            nte.NoteEvent(0.1, 0.2, nte.parse_note_name("C3")),
            nte.NoteEvent(0.2, 0.2, nte.parse_note_name("C6")),
        ]
        actions, warnings = nte.map_actions(notes, nte.NteKeymap(nte.parse_note_name("C3")))
        self.assertEqual(len(actions), 1)
        self.assertTrue(any("outside" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
