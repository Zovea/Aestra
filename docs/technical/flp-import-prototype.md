# Experimental FLP note import

This is a source-preparation prototype, not a complete FL Studio project reader.
It creates Aestra JSON from supported note and playlist fields without rewriting
the FLP, loading plugins, or running either DAW. Audio is accessed only through
an explicit local media map.

Generated files have been loaded by the real `AestraHeadless` reader. One
partial arrangement with explicitly mapped audio rendered successfully and
repeated with the same render hash. MIDI-only FL21 and FL24 controls loaded but
correctly failed the silent-render check because their instruments were not
recreated. Optional empty instrument slots also loaded and remained silent.
The 41 Python tests check synthetic fixtures and converter behavior; private
FL21/24 measurements provide additional layout evidence. These checks do not
establish GUI acceptance, FL Studio musical fidelity, or blanket version
compatibility. No private projects or audio are included in this contribution.

Current export work is limited to **FL21–24**. FL12–20 and FL2025 backfill is
deferred; their containers can still be measured without exporting. Accepting a
version number is not proof of compatibility with a real project.

## Use

Inspect a copy of a project:

```sh
python scripts/import_flp.py input.flp --structure-only
```

The structure report lists event counts, payload lengths/hashes, available
arrangement IDs and playlist record-size divisibility. It contains no song names,
notes, media paths or plugin state. Divisibility is **not layout verification**.
It can inspect multiple arrangements even when conversion cannot proceed.

Compare an original and a rebuilt copy without writing either file:

```sh
python scripts/import_flp.py original.flp --compare-structure rebuilt.flp
```

The comparison checks note224, playlist233 and track238 payloads in stream order.
Identical payload hashes prove identical bytes, not successful musical playback.
An event absent in both files is marked absent, not counted as verified coverage.

After reviewing the unsupported-feature report, explicitly request an incomplete
editable note export:

```sh
python scripts/import_flp.py input.flp --output imported.aes --allow-partial
```

The output must be new: existing files and symlinks are not overwritten. The
original must be retained. Unknown events are identified by offset, size and
hash, not silently called imported. The report is printed and embedded as
`flpImportReport`; Aestra's own serializer does not preserve that extra field on
resave, so retain the original import output if the diagnostics are needed later.

Imported notes have unresolved nonzero instrument IDs. **They are silent until
instruments are mapped; no default synth is substituted.** Plugin state,
mixer routing, automation, note expression and most track/clip flags
are not faithfully converted. Clip mute is mapped. Trim windows require the
measured invariant described below. Every affected record is reported. No unrelated
audio is loaded. Multiple arrangements require `--arrangement-id N`; the default error lists
available IDs, and `--structure-only` gives their associated payloads. The ID is
from the file, not its list index. Unselected playlists are explicitly reported
as omitted. All decoded patterns remain available, including unused patterns.
Duplicate arrangement IDs, missing/ambiguous playlist ownership, or more than one
playlist in the selected arrangement fail instead of guessing. Association uses
event99 boundaries and stops at event100, as documented by the referenced PyFLP
arrangement source; ordering is checked in the private fixtures but not every
possible save.

## Implemented fields

The binary-to-project mapping is intentionally limited:

| FL field | Interpretation | Aestra output |
|---|---|---|
| Header PPQ; event156 | Ticks per quarter note; tempo in milli-BPM | Beat conversion; project tempo |
| Event224, 24-byte note | Start u32@0; channel u16@6; length u32@8; key u16@12 | MIDI note start/duration in beats, pitch, channel+1 unit ID |
| Note bytes20/21 | Pan/velocity in 0..128 | Pan `(value-64)/64`; velocity `value/128` |
| Event233 basic record | Start u32@0; pattern base u16@4; item u16@6; duration u32@8 | Selected-arrangement clip position/duration; pattern ID = item-base |
| Pattern offsets@24/28 | Signed tick offsets, including -1 sentinel | Explicit source window only when end-start equals clip duration |
| Audio offsets@24/28 | Float offsets, not pattern tick fields | Only untrimmed -1/-1 accepted; measured local media at rate1 |
| Playlist flags u16@18 | Mute bit0x2000 | Clip mute; other bits reported unsupported |


- Strict FLhd/FLdt framing, bounded unsigned event lengths and fixed-size events.
- Source version, PPQ, tempo event156, pattern identity65, name193 and
  note payload224 with24-byte records.
- Note key, start/duration in beats, velocity, pan and source-channel identity.
- Playlist event233 basic pattern reference, placement and duration for selected
  layouts. Non-pattern clips are omitted with audio/automation/unknown-channel
  categories based on channel facts. An explicit local media map can import the
  bounded neutral-audio case below; otherwise assets are not accessed.
- Pattern trim offsets are signed32-bit ticks; the observed `-1` sentinel means
  no explicit trim. A nonzero trim maps only when `end - start == duration`
  in ticks. Its source offset is converted to beats, using a derived pattern
  extent covering the entire window so Aestra cannot wrap it accidentally.
  Other endpoint relationships are omitted with diagnostics.
- FL pattern loop periods are not inferred from last-note-end. When an untrimmed
  clip exceeds that known extent, a derived extent preserves one pass plus
  silence and reports `LOOP_PERIOD_UNVERIFIED`; true FL repetitions are not
  claimed. Original patterns remain editable; derived IDs/extent decisions are
  listed in the report. Generated note copies are bounded.
- The known playlist mute bit is mapped to Aestra clip edits. Other clip flags
  and track mute/routing behavior remain unverified.
- Notes and clips beyond Aestra's million-beat range are rejected, not clamped.
- Explicit caller-selected32/60/80-byte playlist records are possible with
  `--playlist-record-size`; this is for independently measured layouts, not an
  automatic fix for any newer version. Extension bytes remain unsupported.

## Explicit local audio mapping

A media map is private JSON, not a repository fixture:

```json
{"C:\\Samples\\example.wav": "/absolute/local/media/example.wav"}
```

```sh
python scripts/import_flp.py input.flp --arrangement-id 2 \
  --media-map private-map.json --output imported.aes --allow-partial
```

Source keys use Windows case/slash normalization. There is no basename search,
automatic download, fallback path, or network probing. Local values must be
absolute existing files. Missing media stays `AUDIO_MEDIA_UNRESOLVED`.
PCM WAV uses frame-count metadata; other supported formats and float WAV use an
already-installed `ffprobe` with local-file protocols only and a15-second limit.
Nothing is installed. Container-derived frame counts are flagged as estimates.

Audio conversion currently requires measured duration/sample rate, a neutral
channel stretch block, and untrimmed `(-1.0, -1.0)` playlist offsets. Clip time
is ticks/PPQ at project tempo. It must not exceed source duration beyond one
source tick; no speed or repetition is inferred. The export uses audio source,
pattern sample slices, seconds-based clip duration/offset and rate1. Channel/
mixer effects are explicitly unconverted. Non-neutral E3/other stretch settings
and unknown audio trim units remain errors in the partial-import report.

Mapped local paths are necessarily present in the private output and its media
report; do not publish them or the original music. A different machine needs its
own mapping and regenerated output. This is not native asset installation.

## FL12–2025 evidence matrix

| Source version | Evidence and uncertainty | Prototype behavior |
|---|---|---|
| FL12.x | Existing event framing and upstream note schema provide a starting point, not a release-by-release specification. No real12.x fixture checked. | Export deferred; structure inspection only. |
| FL20.x | PyFLP documents32-byte older playlist records and66-byte track metadata as of20.9.1. No exhaustive20.x corpus checked. | Export deferred; structure inspection only. |
| FL21.0/21.1 | PyFLP source documents a28-byte extension, total60 bytes. Extension meaning not mapped here. | Basic playlist fields only, extension diagnostic. |
| FL21.2 and FL2024 | Real private21.2.1.3859 and24.2.2.4649 fixtures have consistent60-byte records across15 playlists. This contradicts the broad21.2+ claim in open PyFLP PR205. It does not certify every save or feature. | Those two exact versions use60-byte basic records; other21.2+/24 versions need measured override. Extensions remain unmapped. Never downgrade/spoof source version. |
| FL2025 | Issue200 suggested an8-byte addition; PR205 later says20-byte addition to60 and mentions variable-length edited clips. These are conflicting contributor claims, not official specifications. | Export deferred; structure inspection only. No layout inferred from divisibility. |

Sources:

- [Image-Line project format manual](https://www.image-line.com/fl-studio-learning-content/fl-studio-online-manual/html/fformats_project.htm): FLP does not include sample files; ZIP packages do not capture every third-party sampler dependency.
- [Image-Line version compatibility](https://support.image-line.com/action/knowledgebase/?ans=591&id=78): newer projects are not generally backward-compatible.
- [PyFLP pattern record documentation](https://pyflp.readthedocs.io/en/latest/_modules/pyflp/pattern.html).
- [PyFLP arrangement record documentation](https://pyflp.readthedocs.io/en/latest/_modules/pyflp/arrangement.html).
- [FLParser pattern-vs-audio offset reads](https://github.com/monadgroup/FLParser/blob/master/ProjectParser.cs#L463-L490): integer pattern offsets versus floating-point audio offsets. Field facts were cross-checked, not code copied.
- [PyFLP issue200](https://github.com/demberto/PyFLP/issues/200).
- [PyFLP open PR205](https://github.com/demberto/PyFLP/pull/205).

No PyFLP implementation is vendored or imported. Its current source is GPL3-or-
later; it is evidence about field layout, not code to paste into this repository.
The importer and fixtures are independently written using Python's standard
library; optional audio metadata probing uses the installed ffprobe executable. No decompiled FL Studio code or derived pseudocode was used. Layout references are cited above; no third-party parser implementation is copied.

## Checks and remaining acceptance

```sh
python -m unittest discover -s tests -p test_import_flp.py -v
```

Synthetic cases cover malformed chunks/varints/notes, PPQ96/480/960 timing,
instrument warnings, unknown records, preservation/no-overwrite, version and
layout ambiguity, explicit multi-arrangement selection and rejection of ambiguous
ownership, structural pair comparison, deferred-version rejection and timing bounds.

Private fixture checks currently produced incomplete note/clip JSON for15
arrangements; no private files or content are included in this repository.
All622 pattern placements in those fixtures were emitted, including447
explicit single-window trims;53 placements carry unknown-loop warnings.
The 53 uncertain placements match bar-rounded note extents in the measured 4/4 files, consistent with the [Channel Rack Auto-length description](https://www.image-line.com/fl-studio-learning-content/fl-studio-online-manual/html/channelrack.htm), but actual repetition semantics remain unverified. These counts do not prove playback or full fidelity. Automation and
all instruments remain unmapped. Two neutral audio placements in one fixture
now map to an explicitly supplied local MP3. Six E3-stretched vocal placements
remain unsupported; varispeed would change pitch and is not a faithful substitute.
A successful conversion does not imply full musical playback.

Still required: real FL-authored redistributable fixtures for each claimed
version; independently verified playlist extensions and trims; matching channel
instruments and audio assets; broader `.aes` load coverage and save-roundtrip; and user
listening against an authorized reference. No CI, Windows runtime, native
placement, public release or musical equivalence is claimed.


### Explicit instrument identity and optional silent slots

`instrumentMappings` records every encountered FL channel ID, its Aestra note
unit ID (`channel + 1`), source note count, channel name/type and first plugin
internal name. Sample references expose basename plus a source-path SHA256, not
private directory paths. These are identification aids, not plugin conversions.
FL names use ChannelID192 / PluginID203 and internal name201: PyFLP
[channel.py](https://github.com/demberto/PyFLP/blob/master/pyflp/channel.py)
and [plugin.py](https://github.com/demberto/PyFLP/blob/master/pyflp/plugin.py).
Later mixer plugins do not replace the first channel identity.

`--empty-instrument-slots` optionally emits disabled Arsenal Instrument slots
with matching IDs and empty plugin IDs. No synth, sampler, audio path or state is
invented. `UnitManager.cpp:800–978` restores empty IDs without plugin creation;
`AestraContent.cpp:4364–4400` supports dropping an instrument onto an existing
unit and enables it. The user must choose appropriate instruments and restore
settings; creating slots is not recreating FL sound. GUI interaction is untested.
The report still says instrument-unmapped and completeImport=false. No slots are
created for channels with no source MIDI notes.

Actual cloud headless checks have now loaded private FL21/24-derived projects:
two MIDI-only controls stayed silent, while a partial audio arrangement rendered
and repeated deterministically. This verifies reader/render viability, not
full FL fidelity. The checker does not expose loaded note/clip counts and its
missingPlugins array excludes unresolved note unit references; retain importer
diagnostics. Save-roundtrip and user/listening acceptance remain unverified.
