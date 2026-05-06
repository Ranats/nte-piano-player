# Scores

Put local MIDI or MusicXML scores in this directory and pass the file path at
runtime.

Only public-domain or self-authored sample files should be committed to the
repository. Commercial song exports should stay local.

Included sample:

- `twinkle-twinkle.musicxml`: a small self-authored MusicXML transcription of
  the public-domain melody "Twinkle, Twinkle, Little Star" for smoke testing.

Example:

```powershell
.\scripts\play.ps1 .\scores\twinkle-twinkle.musicxml -ShowEvents
```
