#!/usr/bin/env python3
"""Read-only experimental FLP -> Aestra MIDI import. Never loads plugins.

The default is inspection. Export requires --output and explicit --allow-partial
when unsupported data exists. Input projects and media are never modified.
"""
import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import ntpath
import subprocess
import wave
from pathlib import Path
import re
import struct
import sys
import uuid

MAX_FILE = 64 * 1024 * 1024
MAX_EVENTS = 1000000
MAX_NOTES = 1000000
MAX_BEATS = 1000000
# Measured real-file layouts, not a claim about every save of these versions.
MEASURED_PLAYLIST_SIZES = {"21.2.1.3859": 60, "24.2.2.4649": 60}


class ImportError(ValueError):
    """A corrupt or unsupported container cannot be imported safely."""


@dataclass(frozen=True)
class Event:
    identifier: int
    offset: int
    payload: bytes


def read_events(data):
    if len(data) > MAX_FILE:
        raise ImportError('FLP exceeds 64 MiB limit')
    if len(data) < 22 or data[:4] != b'FLhd':
        raise ImportError('Missing complete FLhd header')
    if struct.unpack_from('<I', data, 4)[0] != 6:
        raise ImportError('Unsupported FLhd length')
    kind, channels, ppq = struct.unpack_from('<HHH', data, 8)
    if kind != 0 or ppq == 0:
        raise ImportError('Expected project format and positive PPQ')
    if data[14:18] != b'FLdt':
        raise ImportError('Missing FLdt chunk')
    end = 22 + struct.unpack_from('<I', data, 18)[0]
    if end != len(data):
        raise ImportError('FLdt length does not match file size')
    events = []
    cursor = 22
    while cursor < end:
        if len(events) >= MAX_EVENTS:
            raise ImportError('Too many events')
        offset = cursor
        identifier = data[cursor]
        cursor += 1
        if identifier < 192:
            size = (1, 2, 4)[identifier // 64]
        else:
            size = 0
            for shift in range(0, 35, 7):
                if cursor >= end:
                    raise ImportError('Truncated event length')
                byte = data[cursor]
                cursor += 1
                if shift == 28 and byte > 15:
                    raise ImportError('Event length exceeds uint32')
                size |= (byte & 127) << shift
                if byte < 128:
                    break
            else:
                raise ImportError('Unterminated event length')
        if size > end - cursor:
            raise ImportError('Event payload crosses FLdt boundary')
        events.append(Event(identifier, offset, data[cursor:cursor + size]))
        cursor += size
    return channels, ppq, events


def read_text(payload):
    if len(payload) % 2:
        raise ImportError('Invalid UTF-16 text event')
    try:
        return payload.decode('utf-16-le').rstrip('\0')
    except UnicodeError as error:
        raise ImportError('Invalid UTF-16 text event') from error


def inspect_structure(data):
    """Measure containers without decoding or exposing song names, notes or paths.

    Divisibility is only a measurement, never proof of a playlist schema.
    This remains usable when conversion rejects multiple arrangements.
    """
    channels, ppq, events = read_events(data)
    counts = Counter(e.identifier for e in events)
    records = []
    arrangement = None
    for event in events:
        if event.identifier == 99:
            arrangement = int.from_bytes(event.payload, 'little')
        elif event.identifier == 100:
            arrangement = None
        if event.identifier not in (224, 233, 238):
            continue
        row = {'id': event.identifier, 'offset': event.offset,
               'bytes': len(event.payload),
               'sha256': hashlib.sha256(event.payload).hexdigest()}
        if event.identifier == 233:
            row['precedingArrangementId'] = arrangement
            row['divisibleRecordSizesNotVerified'] = [
                size for size in (32, 60, 68, 80)
                if event.payload and len(event.payload) % size == 0]
        records.append(row)
    versions = [e.payload.rstrip(b'\0') for e in events if e.identifier == 199]
    version = None
    if len(versions) == 1 and re.fullmatch(rb'\d+(?:\.\d+)+', versions[0]):
        version = versions[0].decode('ascii')
    return {'sourceSha256': hashlib.sha256(data).hexdigest(), 'sourceBytes': len(data),
            'sourceVersion': version, 'ppq': ppq, 'headerChannels': channels,
            'eventCounts': dict(sorted(counts.items())), 'records': records,
            'availableArrangementIds': [int.from_bytes(e.payload, 'little') for e in events if e.identifier == 99],
            'layoutVerified': False, 'runtimeVerified': False}


def compare_structure(original, rebuilt):
    """Compare per-event payloads in stream order, not musical equivalence."""
    left, right = inspect_structure(original), inspect_structure(rebuilt)
    def signatures(report, identifier):
        return [(r['bytes'], r['sha256']) for r in report['records'] if r['id'] == identifier]
    comparisons = {}
    for identifier in (224, 233, 238):
        a, b = signatures(left, identifier), signatures(right, identifier)
        comparisons[str(identifier)] = {'originalCount': len(a), 'rebuiltCount': len(b),
            'payloadsEqualInOrder': a == b, 'presentInBoth': bool(a and b)}
    return {'original': left, 'rebuilt': right, 'payloadComparisons': comparisons,
            'musicalEquivalenceVerified': False}


def media_key(path):
    """Windows path comparison only; never resolve the source path on this host."""
    return ntpath.normcase(ntpath.normpath(path))


def load_media_info(path):
    """Probe only an explicitly mapped local file; no URL or asset discovery."""
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ImportError('Mapped media must be an existing absolute local file')
    if path.stat().st_size > 1024 * 1024 * 1024:
        raise ImportError('Mapped media exceeds1GiB inspection limit')
    duration = None
    if path.suffix.lower() == '.wav':
        try:
            with wave.open(str(path), 'rb') as source:
                rate, frames, channels = source.getframerate(), source.getnframes(), source.getnchannels()
            duration, basis = frames / rate if rate else 0, 'PCM frame count'
        except (wave.Error, EOFError):
            pass  # IEEE-float and other WAV encodings need the metadata probe.
    if duration is None:
        if path.suffix.lower() not in ('.wav', '.mp3', '.flac', '.ogg', '.aif', '.aiff'):
            raise ImportError('Mapped media extension is unsupported')
        # Fixed metadata-only invocation; local protocol only, no shell or installs.
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
                '-select_streams', 'a:0', '-show_entries',
                'stream=sample_rate,channels,duration:format=duration', '-of', 'json', str(path)],
                check=True, capture_output=True, text=True, timeout=15)
            metadata = json.loads(result.stdout)
            stream = metadata['streams'][0]
            rate, channels = int(stream['sample_rate']), int(stream['channels'])
            duration = float(stream.get('duration', metadata.get('format', {}).get('duration', 'nan')))
            frames, basis = round(duration * rate), 'ffprobe duration estimate'
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, OverflowError) as error:
            raise ImportError('Mapped audio metadata could not be measured') from error
    if not (0 < rate <= 768000 and 0 < channels <= 64 and 0 < duration <= MAX_BEATS and frames > 0):
        raise ImportError('Mapped media metadata is outside supported limits')
    return {'path': str(path), 'sampleRate': rate, 'frames': frames, 'channels': channels,
            'durationSeconds': duration, 'measurement': basis}


def convert(data, playlist_record_size=None, arrangement_id=None, media_map=None, empty_instrument_slots=False):
    channels, ppq, events = read_events(data)
    media_map = {media_key(k): v for k, v in (media_map or {}).items()}
    versions = [e.payload.rstrip(b'\0') for e in events if e.identifier == 199]
    if len(versions) != 1:
        raise ImportError('Expected one FL version event')
    try:
        version = versions[0].decode('ascii')
    except UnicodeError as error:
        raise ImportError('Invalid FL version') from error
    if not re.fullmatch(r'\d+(?:\.\d+)+', version):
        raise ImportError('Invalid FL version')
    major = int(version.split('.')[0])
    minor = int(version.split('.')[1])
    if major < 21 or major > 24:
        raise ImportError('Export scope is FL21 through24; other versions are deferred')
    report = {'sourceSha256': hashlib.sha256(data).hexdigest(), 'sourceVersion': version,
              'ppq': ppq, 'headerChannels': channels, 'issues': [], 'unmappedEvents': [],
              'runtimeVerified': False, 'completeImport': False}

    def issue(code, detail, offset=None):
        row = {'code': code, 'detail': detail}
        if offset is not None:
            row['offset'] = offset
        report['issues'].append(row)

    issue('EXPERIMENTAL', 'Synthetic tests and limited private fixtures; this conversion is not a runtime or musical acceptance check.')
    patterns = {}
    current = None
    tempo = None
    note_count = 0
    channel_note_counts = Counter()
    playlist = []
    arrangements = []
    active_arrangement = None
    for event in events:
        eid, payload = event.identifier, event.payload
        if eid == 199 or eid == 159:
            continue
        if eid == 156:
            candidate = int.from_bytes(payload, 'little') / 1000
            if not 20 <= candidate <= 999:
                raise ImportError('Tempo outside Aestra supported range')
            if tempo is not None and candidate != tempo:
                raise ImportError('Multiple different tempos need tempo-map support')
            tempo = candidate
        elif eid == 65:
            current = int.from_bytes(payload, 'little')
            if current == 0:
                raise ImportError('Pattern identity zero is unsupported')
            patterns.setdefault(current, {'id': current, 'name': f'Pattern {current}',
                                         'type': 'midi', 'length': 0, 'notes': []})
        elif eid == 193:
            if current is None:
                raise ImportError('Pattern name without pattern identity')
            patterns[current]['name'] = read_text(payload)
        elif eid == 224:
            if current is None or len(payload) % 24:
                raise ImportError('Notes require a pattern identity and complete 24-byte records')
            note_count += len(payload) // 24
            if note_count > MAX_NOTES:
                raise ImportError('Too many notes')
            for pos in range(0, len(payload), 24):
                raw = payload[pos:pos + 24]
                start = int.from_bytes(raw[0:4], 'little')
                flags = int.from_bytes(raw[4:6], 'little')
                channel = int.from_bytes(raw[6:8], 'little')
                channel_note_counts[channel] += 1
                duration = int.from_bytes(raw[8:12], 'little')
                key = int.from_bytes(raw[12:14], 'little')
                if (start + duration) / ppq > MAX_BEATS:
                    raise ImportError('Note timing exceeds Aestra million-beat range')
                if key > 127 or raw[20] > 128 or raw[21] > 128:
                    raise ImportError('Note values exceed supported MIDI/pan/velocity range')
                note = {'pitch': key, 'startBeat': start / ppq,
                        'durationBeats': duration / ppq, 'velocity': raw[21] / 128,
                        'pan': (raw[20] - 64) / 64, 'unitId': channel + 1}
                patterns[current]['notes'].append(note)
                patterns[current]['length'] = max(patterns[current]['length'], (start + duration) / ppq)
                if flags or int.from_bytes(raw[14:16], 'little') or raw[16] != 120 or any(raw[17:20]) or any(raw[22:24]):
                    issue('NOTE_EXPRESSION_UNMAPPED', 'Flags/group/tuning/release/MIDI-channel/modulation retained only in original FLP.', event.offset + pos)
        elif eid == 99:
            active_arrangement = int.from_bytes(payload, 'little')
            if active_arrangement in arrangements:
                raise ImportError('Duplicate arrangement identity; grouping is ambiguous')
            arrangements.append(active_arrangement)
        elif eid == 100:
            active_arrangement = None
            issue('CURRENT_ARRANGEMENT_NOT_APPLIED', 'Saved UI selection does not choose an export arrangement.', event.offset)
        elif eid == 233:
            playlist.append((active_arrangement, event))
        else:
            report['unmappedEvents'].append({'id': eid, 'offset': event.offset,
                'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
    if tempo is None:
        raise ImportError('No supported tempo event; refusing to invent BPM')
    if not patterns:
        raise ImportError('No supported MIDI patterns')
    report['availableArrangementIds'] = arrangements
    report['selectedArrangementId'] = arrangement_id
    if arrangement_id is None:
        if len(arrangements) > 1 or len(playlist) > 1:
            raise ImportError(f'Multiple arrangements require --arrangement-id; available IDs: {arrangements}. Use --structure-only for payload details')
        if arrangements and any(owner != arrangements[0] for owner, _ in playlist):
            raise ImportError('Playlist is outside its declared arrangement; ownership is ambiguous')
        playlist = [event for _, event in playlist]
    else:
        if arrangement_id not in arrangements:
            raise ImportError(f'Arrangement ID {arrangement_id} not found; available IDs: {arrangements}')
        selected = []
        for owner, event in playlist:
            if owner == arrangement_id:
                selected.append(event)
            else:
                issue('ARRANGEMENT_NOT_SELECTED', f'Playlist belongs to arrangement {owner}; explicitly excluded.', event.offset)
                report['unmappedEvents'].append({'id': event.identifier, 'offset': event.offset,
                    'bytes': len(event.payload), 'sha256': hashlib.sha256(event.payload).hexdigest()})
        if len(selected) != 1:
            raise ImportError('Selected arrangement needs exactly one associated playlist event; none guessed')
        playlist = selected
    # Channel sample/type facts only; do not fetch paths or load plugins.
    channel_facts = {}
    channel_id = None
    for event in events:
        if event.identifier == 64:
            channel_id = int.from_bytes(event.payload, 'little')
            channel_facts.setdefault(channel_id, {})
        elif event.identifier in (99, 100):
            channel_id = None  # Arrangement scope is not a channel or mixer plugin.
        elif channel_id is not None:
            facts = channel_facts[channel_id]
            if event.identifier == 21:
                facts.setdefault('type', event.payload[0])
            elif event.identifier == 196:
                facts.setdefault('hasSample', bool(event.payload.rstrip(b'\0')))
                facts.setdefault('samplePath', read_text(event.payload))
            elif event.identifier == 215:
                facts.setdefault('parameters', event.payload)
            elif event.identifier in (192, 203):
                facts.setdefault('legacyName' if event.identifier == 192 else 'name', read_text(event.payload))
            elif event.identifier == 201:
                facts.setdefault('pluginName', read_text(event.payload))
                # First channel plugin only; later mixer plugins are not this channel.
                facts.setdefault('emptyPlugin', not bool(event.payload.rstrip(b'\0')))
    # Names and references are diagnostics, never plugin IDs or executable state.
    report['instrumentMappings'] = []
    empty_units = []
    for cid in sorted(set(channel_facts) | set(channel_note_counts)):
        facts = channel_facts.get(cid, {})
        name = facts.get('name') or facts.get('legacyName') or facts.get('pluginName') or f'FL channel {cid}'
        sample = facts.get('samplePath', '')
        row = {'channelId': cid, 'aestraUnitId': cid + 1,
               'channelName': name, 'channelType': facts.get('type'),
               'pluginName': facts.get('pluginName'), 'sourceNoteCount': channel_note_counts[cid],
               'status': 'instrument-unmapped' if channel_note_counts[cid] else 'no-source-midi-notes',
               'emptySlotCreated': bool(empty_instrument_slots and channel_note_counts[cid])}
        if sample:
            row['sampleReference'] = {'basename': sample.replace('\\', '/').rsplit('/', 1)[-1],
                                     'sourcePathSha256': hashlib.sha256(sample.encode('utf-8')).hexdigest()}
        report['instrumentMappings'].append(row)
        if row['emptySlotCreated']:
            # UnitManager loads an empty pluginId without creating any instrument.
            empty_units.append({'id': cid + 1, 'name': name, 'type': 'Instrument',
                                'enabled': False, 'pluginId': '', 'targetMixerRoute': -1})
    if empty_units:
        issue('EMPTY_INSTRUMENT_SLOTS', 'Disabled, silent slots preserve note unit IDs; assign instruments and enable manually. No FL plugin state converted.')
    source_pattern_count = len(patterns)
    derived = {}
    derived_note_copies = 0
    next_pattern_id = max(patterns) + 1
    report['derivedPatterns'] = []
    report['media'] = []
    audio_sources = []
    audio_patterns = {}
    media_info = {}

    def window_pattern(pattern_id, extent, reason):
        nonlocal next_pattern_id, derived_note_copies
        key = (pattern_id, extent)
        if key in derived:
            return derived[key]
        original = patterns[pattern_id]
        derived_note_copies += len(original['notes'])
        if derived_note_copies > MAX_NOTES:
            raise ImportError('Derived pattern notes exceed bounded export limit')
        new_id = next_pattern_id
        next_pattern_id += 1
        patterns[new_id] = dict(original, id=new_id, length=max(original['length'], extent))
        derived[key] = new_id
        report['derivedPatterns'].append({'id': new_id, 'sourcePatternId': pattern_id,
                                         'minimumExtentBeats': extent, 'reason': reason})
        return new_id

    lanes = {}
    namespace = uuid.UUID('64c69b96-bd99-4d12-8bea-b7522ba9708a')
    for event in playlist:
        if playlist_record_size is not None and playlist_record_size not in (32, 60, 80):
            raise ImportError('Explicit playlist record size must be32,60 or80')
        if playlist_record_size is None and version not in MEASURED_PLAYLIST_SIZES and (major > 21 or (major == 21 and minor >= 2) or minor >= 99):
            issue('PLAYLIST_VERSION_UNSUPPORTED', 'FL21.2+ has conflicting reported playlist layouts; no arrangement guessed.', event.offset)
            report['unmappedEvents'].append({'id': event.identifier, 'offset': event.offset,
                'bytes': len(event.payload), 'sha256': hashlib.sha256(event.payload).hexdigest()})
            continue
        size = playlist_record_size or MEASURED_PLAYLIST_SIZES.get(version, 60)
        if playlist_record_size is None and version in MEASURED_PLAYLIST_SIZES:
            issue('MEASURED_PLAYLIST_LAYOUT', '60-byte records measured in private same-version fixtures; other saves/extensions remain unverified.', event.offset)
        if playlist_record_size is not None:
            issue('EXPLICIT_PLAYLIST_LAYOUT', f'Caller selected {size}-byte records; source-version compatibility is not established.', event.offset)
        if len(event.payload) % size:
            raise ImportError(f'Playlist does not match explicit {size}-byte version layout')
        if size > 32:
            issue('PLAYLIST_EXTENSION_UNMAPPED', 'Playlist extension is not interpreted.', event.offset)
        for index in range(0, len(event.payload), size):
            raw = event.payload[index:index + size]
            start, base, item, duration, reverse_track = struct.unpack_from('<IHHIH', raw)
            if (start + duration) / ppq > MAX_BEATS:
                raise ImportError('Clip timing exceeds Aestra million-beat range')
            if base != 20480:
                issue('UNKNOWN_CLIP', 'Unrecognized playlist pattern base; clip omitted.', event.offset + index)
                continue
            if item <= base:
                facts = channel_facts.get(item, {})
                if facts.get('type') == 5:
                    code = 'AUTOMATION_CLIP_UNMAPPED'
                elif facts.get('hasSample') and (facts.get('type') == 0 or facts.get('emptyPlugin')):
                    code = 'AUDIO_CLIP_UNMAPPED'
                else:
                    code = 'CHANNEL_CLIP_UNMAPPED'
                if code != 'AUDIO_CLIP_UNMAPPED':
                    issue(code, f'Channel {item} playlist item omitted; no asset/plugin/automation conversion.', event.offset + index)
                    continue
                source_path = facts.get('samplePath', '')
                mapped = media_map.get(media_key(source_path))
                if mapped is None:
                    issue('AUDIO_MEDIA_UNRESOLVED', f'Channel {item} needs an explicit local media mapping; clip omitted.', event.offset + index)
                    continue
                params = facts.get('parameters', b'')
                if len(params) < 112 or any(params[96:112]):
                    issue('AUDIO_STRETCH_UNSUPPORTED', f'Channel {item} has unknown/non-neutral stretch settings; no varispeed substitution.', event.offset + index)
                    continue
                left, right = struct.unpack_from('<ff', raw, 24)
                if (left, right) != (-1.0, -1.0):
                    issue('AUDIO_TRIM_UNVERIFIED', f'Channel {item} audio trim units are not verified; clip omitted.', event.offset + index)
                    continue
                if item not in media_info:
                    try:
                        media_info[item] = load_media_info(mapped)
                        report['media'].append(dict(media_info[item], channelId=item))
                    except ImportError as error:
                        media_info[item] = None
                        issue('AUDIO_MEDIA_UNRESOLVED', f'Channel {item}: {error}', event.offset + index)
                info = media_info[item]
                if info is None:
                    continue
                clip_seconds = duration / ppq * 60 / tempo
                if clip_seconds > info['durationSeconds'] + 60 / tempo / ppq:
                    issue('AUDIO_RATE_UNVERIFIED', f'Channel {item} clip exceeds measured media duration; no inferred stretching/repetition.', event.offset + index)
                    continue
                if info['durationSeconds'] * tempo / 60 > MAX_BEATS:
                    issue('AUDIO_RANGE_UNSUPPORTED', f'Channel {item} audio pattern exceeds Aestra timing bounds.', event.offset + index)
                    continue
                if info['measurement'] != 'PCM frame count':
                    issue('AUDIO_METADATA_ESTIMATED', f'Channel {item}: container duration/frame estimate; decoder roundtrip not verified.', event.offset + index)
                if item not in audio_patterns:
                    audio_id = next_pattern_id
                    next_pattern_id += 1
                    audio_patterns[item] = audio_id
                    source_id = len(audio_sources) + 1
                    audio_sources.append({'id': source_id, 'path': info['path'], 'name': f'FL audio channel {item}'})
                    patterns[audio_id] = {'id': audio_id, 'name': f'FL audio channel {item}',
                        'type': 'audio', 'length': info['durationSeconds'] * tempo / 60,
                        'sourceId': source_id, 'durationSeconds': info['durationSeconds'],
                        'slices': [{'start': 0, 'length': info['frames']}], 'mixerChannelId': 0}
                lane = lanes.setdefault(reverse_track, {'name': f'FL track reverse-index {reverse_track}',
                    'id': str(uuid.uuid5(namespace, f'lane:{reverse_track}')), 'clips': []})
                lane['clips'].append({'id': str(uuid.uuid5(namespace, f'clip:{index}')),
                    'patternId': audio_patterns[item], 'name': f'FL audio channel {item}',
                    'start': start / ppq, 'duration': duration / ppq, 'durationSeconds': clip_seconds,
                    'sourceOffset': 0, 'sourceOffsetSeconds': 0,
                    'edits': {'playbackRate': 1, 'muted': bool(struct.unpack_from('<H', raw, 18)[0] & 0x2000)}})
                issue('AUDIO_CHANNEL_PROCESSING_UNMAPPED', f'Channel {item}: raw mapped audio only; FL channel/mixer processing not reproduced.', event.offset + index)
                continue
            pattern_id = item - base
            if pattern_id not in patterns:
                raise ImportError('Playlist references absent pattern')
            # Pattern offsets are signed ticks, unlike audio offsets (float).
            # 0xffffffff is the -1 untrimmed sentinel, not a malformed NaN.
            left, right = struct.unpack_from('<ii', raw, 24)
            if left < -1 or right < -1:
                raise ImportError('Invalid negative pattern trim offset')
            source_offset = 0
            if left not in (0, -1) or right not in (0, -1):
                if left < 0 or right <= left or right - left != duration:
                    issue('TRIMMED_CLIP_UNMAPPED', 'Trim endpoints do not define the measured single-window duration; clip omitted.', event.offset + index)
                    continue
                if right / ppq > MAX_BEATS:
                    raise ImportError('Trim source window exceeds Aestra million-beat range')
                source_offset = left / ppq
                pattern_id = window_pattern(pattern_id, right / ppq, 'explicit non-repeating trim window')
                issue('TRIM_WINDOW_IMPORTED', 'Signed tick endpoints mapped to source offset and duration; runtime note-boundary behavior unverified.', event.offset + index)
            elif duration / ppq > patterns[pattern_id]['length']:
                # Aestra repeats at pattern length. Note-end alone is NOT an FL loop period.
                pattern_id = window_pattern(pattern_id, duration / ppq, 'unknown source loop period; no fabricated repeats')
                issue('LOOP_PERIOD_UNVERIFIED', 'Clip exceeds known note extent; preserve one pass plus silence rather than invent repeated notes.', event.offset + index)
            # Reverse index is retained as identity; no unproven track-count constant.
            lane = lanes.setdefault(reverse_track, {'name': f'FL track reverse-index {reverse_track}',
                'id': str(uuid.uuid5(namespace, f'lane:{reverse_track}')), 'clips': []})
            lane['clips'].append({'id': str(uuid.uuid5(namespace, f'clip:{index}')),
                'patternId': pattern_id, 'name': patterns[pattern_id]['name'],
                'start': start / ppq, 'duration': duration / ppq, 'sourceOffset': source_offset,
                'edits': {'muted': bool(struct.unpack_from('<H', raw, 18)[0] & 0x2000)}})
            if any(raw[14:24]):
                issue('CLIP_FLAGS_UNMAPPED', 'Playlist group/flags not interpreted.', event.offset + index)
    if note_count:
        issue('INSTRUMENTS_UNMAPPED', 'Notes keep nonzero unresolved unit IDs; no replacement instruments or plugin state invented.')
    if report['unmappedEvents']:
        issue('UNMAPPED_EVENTS', 'See event offsets, sizes and hashes; original FLP remains authoritative.')
    report['noteCount'] = note_count
    report['sourcePatternCount'] = source_pattern_count
    report['patternCount'] = len(patterns)
    report['clipCount'] = sum(len(lane['clips']) for lane in lanes.values())
    report['unmappedEventCounts'] = dict(Counter(e['id'] for e in report['unmappedEvents']))
    project = {'version': 4, 'tempo': tempo, 'playhead': 0, 'sources': audio_sources,
               'patterns': list(patterns.values()), 'lanes': [lanes[k] for k in sorted(lanes, reverse=True)],
               'mixerChannels': [], 'flpImportReport': report}
    if empty_units:
        project['arsenal'] = {'nextId': max(u['id'] for u in empty_units) + 1, 'units': empty_units}
    return project, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--allow-partial', action='store_true', help='Explicitly accept reported unsupported features')
    parser.add_argument('--playlist-record-size', type=int, choices=(32, 60, 80),
                        help='Use only after independently confirming the fixed record size; never inferred from divisibility')
    parser.add_argument('--empty-instrument-slots', action='store_true',
                        help='Create disabled, silent named slots for note channels; no replacement synth or plugin state')
    parser.add_argument('--media-map', type=Path, help='JSON mapping exact Windows source paths to absolute local media paths; no automatic lookup')
    parser.add_argument('--arrangement-id', type=int, help='Explicit FL arrangement identity, not its list index')
    parser.add_argument('--structure-only', action='store_true', help='Measure framing and payload hashes without converting music')
    parser.add_argument('--compare-structure', type=Path, metavar='REBUILT_FLP',
                        help='Compare input and rebuilt containers without converting music')
    args = parser.parse_args(argv)
    try:
        if args.input.stat().st_size > MAX_FILE:
            raise ImportError('FLP exceeds64MiB limit')
        if args.structure_only or args.compare_structure:
            if args.output or args.allow_partial or args.playlist_record_size is not None or args.arrangement_id is not None or args.media_map is not None:
                raise ImportError('Structure inspection cannot be combined with export options')
            data = args.input.read_bytes()
            if args.compare_structure:
                if args.compare_structure.stat().st_size > MAX_FILE:
                    raise ImportError('Comparison FLP exceeds64MiB limit')
                report = compare_structure(data, args.compare_structure.read_bytes())
            else:
                report = inspect_structure(data)
            print(json.dumps(report, indent=2))
            return 0
        media_map = None
        if args.media_map:
            if args.media_map.stat().st_size > 1024 * 1024:
                raise ImportError('Media map exceeds1MiB limit')
            try:
                media_map = json.loads(args.media_map.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as error:
                raise ImportError('Invalid media-map JSON') from error
            if not isinstance(media_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in media_map.items()):
                raise ImportError('Media map must map source-path strings to local-path strings')
        project, report = convert(args.input.read_bytes(), args.playlist_record_size, args.arrangement_id, media_map, args.empty_instrument_slots)
        print(json.dumps(report, indent=2))
        if args.output:
            if not args.allow_partial:
                raise ImportError('Export needs --allow-partial; inspect unsupported features first')
            if args.output.suffix.lower() != '.aes':
                raise ImportError('Output must end in .aes')
            # Exclusive creation also blocks identical input, existing files and symlinks.
            with args.output.open('x', encoding='utf-8') as output:
                json.dump(project, output, indent=2, allow_nan=False)
                output.write('\n')
        return 0
    except (ImportError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
