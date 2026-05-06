# NTE Piano Player

Unofficial local Windows helper for the NTE: Neverness to Everness in-game
piano. It converts MIDI or MusicXML scores into normal foreground keyboard
input for the piano UI.

This project is not affiliated with, endorsed by, or sponsored by the NTE
developers or publishers.

![NTE Piano Player demo](assets/nte-piano-player-demo.gif)

This project is for the piano entertainment content only. It does not automate
movement, combat, rewards, collection, multiplayer behavior, or any other
gameplay progression.

## Required In-Game Setting

Before using play mode, open the NTE piano content and switch the in-game piano
setting to `32-key mode`.

The default piano mode does not expose semitone keys, so songs with sharps or
flats will not play correctly unless `32-key mode` is enabled.

## Keyboard Layout

The tool maps scores to the keyboard layout shown in the piano UI:

| Row | Natural notes | Keyboard |
| --- | --- | --- |
| High | 1 2 3 4 5 6 7 | `Q W E R T Y U` |
| Middle | 1 2 3 4 5 6 7 | `A S D F G H J` |
| Low | 1 2 3 4 5 6 7 | `Z X C V B N M` |

Chromatic notes are sent using the in-game modifier behavior:

| Degree | Input |
| --- | --- |
| `#1` | `Shift + 1-key` |
| `b3` | `Ctrl + 3-key` |
| `#4` | `Shift + 4-key` |
| `#5` | `Shift + 5-key` |
| `b7` | `Ctrl + 7-key` |

The tool sends ordinary foreground keyboard input only. It does not read game
memory, inject code, inspect network traffic, or modify the game process.

## Quick start

Requirements:

- Windows
- Python 3.10 or newer
- NTE piano content opened in `32-key mode`

Dry-run first:

```powershell
.\scripts\play.ps1 .\scores\twinkle-twinkle.musicxml -ShowEvents
```

Play into the game:

```powershell
.\scripts\play.ps1 .\scores\twinkle-twinkle.musicxml -Play -RequireTitle "NTE"
```

If NTE does not accept the default scancode input, retry the legacy virtual-key
mode:

```powershell
.\scripts\play.ps1 .\scores\twinkle-twinkle.musicxml -Play -RequireTitle "NTE" -InputMode vk
```

The wrapper waits three seconds before playback. Put the NTE piano window in
focus during the countdown. Press `Esc` to abort.

## Scores

Put MIDI or MusicXML files in `scores/` and pass the file path at runtime:

```powershell
.\scripts\play.ps1 .\scores\your-song.mxl -FitRange -TempoScale 0.5
```

The repository should only commit public-domain or self-authored sample scores.
Commercial song exports should stay local and uncommitted.

Supported input:

- `.mid` / `.midi`: Standard MIDI files, format 0/1, PPQ timing, tempo events,
  note on/off.
- `.musicxml` / `.xml` / `.mxl`: Basic MusicXML notes, rests, chords,
  divisions, and simple tempo from `sound tempo`.

Not supported yet:

- MIDI SMPTE timing.
- MusicXML repeats, ornaments, pedal notation, tuplets with advanced playback
  semantics, or expression mapping.
- Automatic reading of PDF sheet music. Export PDF/score data to MIDI or
  MusicXML first, for example from MuseScore.

## Mapping range

Default mapping treats the low row as `C3..B3`, middle as `C4..B4`, and high as
`C5..B5`.

Useful options:

```powershell
python .\nte_autoplayer.py song.mid --lowest-note C3 --transpose 0 --range-mode octave-fold --tempo-scale 0.75 --verbose
```

- `--lowest-note C3`: pitch mapped to the low row degree `1`.
- `--transpose N`: shift input by semitones before mapping.
- `--range-mode octave-fold`: move out-of-range notes by octaves into the NTE keyboard.
- `--tempo-scale 0.5`: half speed. `2.0` is double speed.
- `--play`: send real input. Without it, the command is dry-run only.
- `--require-title Neverness`: stop unless the foreground window title matches.
- `--allow-any-window`: bypass the title guard. This is not recommended.
- `--input-mode scan`: send scancodes by default. Use `vk` only as a fallback.

## Validation flow

1. Run dry-run with `--verbose`.
2. Check warnings for out-of-range notes or physical key overlaps.
3. Make sure NTE is in `32-key mode`.
4. Try `scores/twinkle-twinkle.musicxml` in the game at normal speed.
5. Try the target song with `--range-mode octave-fold --tempo-scale 0.5`.
6. Increase speed only after the game stops dropping inputs.

PowerShell wrapper dry-run with event details:

```powershell
.\scripts\play.ps1 .\scores\twinkle-twinkle.musicxml -ShowEvents
```

For a score that exceeds the in-game range:

```powershell
.\scripts\play.ps1 .\scores\your-song.mxl -FitRange -TempoScale 0.5
```

Dense chords and notes that require the same physical key at the same time can
collapse, because the keyboard layout cannot hold two versions of the same key
simultaneously.

## Project Scope

In scope:

- MIDI / MusicXML to NTE piano keyboard input.
- Local dry-run and foreground-only playback.
- Score files loaded from `scores/`.
- Range fitting for the in-game piano keyboard.

Out of scope:

- Reward, combat, movement, farming, or progression automation.
- Memory reading/writing, DLL injection, reverse engineering, or network hooks.
- Bundling copyrighted song exports in the repository.

## Support

If this helps you enjoy the NTE piano content, optional support is welcome:
[Ko-fi](https://ko-fi.com/ranats).

## License

MIT. See [LICENSE](LICENSE).
