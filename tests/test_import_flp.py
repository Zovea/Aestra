"""Synthetic binary fixtures; no songs, plugins, samples or third-party code."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

SPEC = importlib.util.spec_from_file_location('import_flp', Path(__file__).parents[1] / 'scripts/import_flp.py')
importer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = importer
SPEC.loader.exec_module(importer)


def event(identifier, payload):
    prefix = bytes([identifier])
    if identifier >= 192:
        n = len(payload)
        while n >= 128:
            prefix += bytes([(n & 127) | 128])
            n >>= 7
        prefix += bytes([n])
    return prefix + payload


def fixture(version='21.0.0.0', ppq=96, extra=b'', playlist=True):
    note = bytearray(24)
    struct.pack_into('<IHHIHH', note, 0, ppq, 0, 0, ppq * 2, 60, 0)
    note[16], note[20], note[21] = 120, 64, 100
    stream = event(199, version.encode() + b'\0') + event(156, struct.pack('<I', 120000))
    stream += event(65, struct.pack('<H', 1)) + event(193, 'Synthetic'.encode('utf-16-le'))
    stream += event(224, note)
    if playlist:
        clip = bytearray(60 if int(version.split('.')[0]) >= 21 else 32)
        struct.pack_into('<IHHIH', clip, 0, ppq * 4, 20480, 20481, ppq * 4, 499)
        stream += event(233, clip)
    stream += extra
    return b'FLhd' + struct.pack('<IHHH', 6, 0, 1, ppq) + b'FLdt' + struct.pack('<I', len(stream)) + stream


class ImportTests(unittest.TestCase):
    def test_instrument_mapping_keeps_identity_without_loading_plugin(self):
        text = lambda value: value.encode('utf-16-le') + b'\0\0'
        extra = event(64, struct.pack('<H', 0)) + event(21, b'\2')
        extra += event(203, text('Named synth')) + event(201, text('Original synth'))
        extra += event(201, text('Later mixer effect'))
        extra += event(196, text('C:/private/samples/example.wav'))
        project, report = importer.convert(fixture(extra=extra))
        row = report['instrumentMappings'][0]
        self.assertEqual((row['channelId'], row['aestraUnitId'], row['sourceNoteCount']), (0, 1, 1))
        self.assertEqual(row['channelName'], 'Named synth')
        self.assertEqual(row['pluginName'], 'Original synth')
        self.assertEqual(row['sampleReference']['basename'], 'example.wav')
        self.assertNotIn('C:/private', json.dumps(row))
        self.assertNotIn('arsenal', project)
        self.assertEqual(row['status'], 'instrument-unmapped')

    def test_empty_slots_are_explicit_disabled_and_have_no_plugin(self):
        project, report = importer.convert(fixture(), empty_instrument_slots=True)
        unit = project['arsenal']['units'][0]
        self.assertEqual(unit['id'], project['patterns'][0]['notes'][0]['unitId'])
        self.assertFalse(unit['enabled'])
        self.assertEqual(unit['pluginId'], '')
        self.assertNotIn('pluginStateHex', unit)
        self.assertNotIn('audioClipPath', unit)
        self.assertEqual(project['arsenal']['nextId'], 2)
        self.assertTrue(report['instrumentMappings'][0]['emptySlotCreated'])
        self.assertFalse(report['completeImport'])

    def test_channels_without_notes_get_report_but_no_empty_slot(self):
        extra = event(64, struct.pack('<H', 7)) + event(21, b'\5')
        project, report = importer.convert(fixture(extra=extra), empty_instrument_slots=True)
        self.assertEqual([u['id'] for u in project['arsenal']['units']], [1])
        row = next(r for r in report['instrumentMappings'] if r['channelId'] == 7)
        self.assertEqual(row['status'], 'no-source-midi-notes')
        self.assertFalse(row['emptySlotCreated'])

    def test_note_and_playlist_timing(self):
        for ppq in (96, 480, 960):
            with self.subTest(ppq=ppq):
                project, report = importer.convert(fixture(ppq=ppq))
                note = project['patterns'][0]['notes'][0]
                self.assertEqual((note['startBeat'], note['durationBeats'], note['pitch']), (1, 2, 60))
                self.assertEqual(note['velocity'], 100 / 128)
                self.assertEqual(project['lanes'][0]['clips'][0]['start'], 4)
                self.assertEqual(report['clipCount'], 1)
                self.assertFalse(report['completeImport'])
                self.assertIn('INSTRUMENTS_UNMAPPED', [x['code'] for x in report['issues']])

    def test_export_scope_21_to_24(self):
        for version in (21, 22, 23, 24):
            _, report = importer.convert(fixture(version=f'{version}.0.0.0'))
            self.assertEqual(report['noteCount'], 1)
            self.assertFalse(report['runtimeVerified'])
            self.assertEqual(report['clipCount'], 1 if version == 21 else 0)
        for version in (12, 20, 25):
            with self.assertRaises(importer.ImportError):
                importer.convert(fixture(version=f'{version}.0.0.0'))

    def test_unknown_event_has_offset_size_hash(self):
        _, report = importer.convert(fixture(extra=event(250, b'unknown')))
        unknown = report['unmappedEvents'][0]
        self.assertEqual((unknown['id'], unknown['bytes']), (250, 7))
        self.assertEqual(len(unknown['sha256']), 64)

    def test_invalid_container_rejected(self):
        valid = fixture()
        cases = [b'', valid[:13], valid[:-1], valid + b'junk', valid[:18] + struct.pack('<I', 0) + valid[22:]]
        for data in cases:
            with self.subTest(size=len(data)):
                with self.assertRaises(importer.ImportError):
                    importer.convert(data)

    def test_truncated_events(self):
        header = b'FLhd' + struct.pack('<IHHH', 6, 0, 1, 96) + b'FLdt'
        for stream in (b'\xc7\x80', b'\x9f\x01', b'\xc7\xff\xff\xff\xff\xff', b'\xc7\x04x'):
            with self.subTest(stream=stream):
                with self.assertRaises(importer.ImportError):
                    importer.read_events(header + struct.pack('<I', len(stream)) + stream)

    def test_zero_ppq(self):
        data = bytearray(fixture())
        data[12:14] = b'\0\0'
        with self.assertRaises(importer.ImportError):
            importer.convert(bytes(data))

    def test_invalid_notes(self):
        for payload in (b'x', bytes(23), bytes(25)):
            with self.assertRaises(importer.ImportError):
                importer.convert(fixture(extra=event(224, payload)))

    def test_future_version_rejected(self):
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(version='26.0.0.0'))

    def test_output_requires_partial_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'input.flp', Path(folder) / 'result.aes'
            data = fixture()
            source.write_bytes(data)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(importer.main([str(source), '--output', str(output)]), 2)
            self.assertFalse(output.exists())
            args = [str(source), '--output', str(output), '--allow-partial']
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(importer.main(args), 0)
            self.assertEqual(json.loads(output.read_text())['patterns'][0]['notes'][0]['pitch'], 60)
            before = output.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(importer.main(args), 2)
            self.assertEqual(before, output.read_bytes())
            self.assertEqual(data, source.read_bytes())

    def test_report_embedded_in_export(self):
        project, report = importer.convert(fixture(extra=event(250, b'unknown')))
        self.assertEqual(project['flpImportReport'], report)

    def test_malformed_playlist(self):
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(playlist=False, extra=event(233, b'x')))

    def test_missing_tempo_is_not_defaulted(self):
        data = fixture()
        needle = event(156, struct.pack('<I', 120000))
        payload = data[22:].replace(needle, b'')
        with self.assertRaises(importer.ImportError):
            importer.convert(data[:18] + struct.pack('<I', len(payload)) + payload)

    def test_24_never_guesses_playlist_size(self):
        for size in (60, 68, 120, 136):
            project, report = importer.convert(fixture(version='24.0.0.0', playlist=False,
                                                       extra=event(233, bytes(size))))
            self.assertEqual(project['lanes'], [])
            self.assertEqual(report['unmappedEvents'][0]['bytes'], size)

    def test_212_never_guesses_playlist_size(self):
        project, report = importer.convert(fixture(version='21.2.0.0'))
        self.assertEqual(project['lanes'], [])
        self.assertIn('PLAYLIST_VERSION_UNSUPPORTED', [x['code'] for x in report['issues']])

    def test_explicit_80_byte_playlist(self):
        clip = bytearray(80)
        struct.pack_into('<IHHIH', clip, 0, 384, 20480, 20481, 384, 499)
        project, report = importer.convert(fixture(version='24.0.0.0', playlist=False,
                                                  extra=event(233, clip)), playlist_record_size=80)
        self.assertEqual(project['lanes'][0]['clips'][0]['start'], 4)
        self.assertIn('EXPLICIT_PLAYLIST_LAYOUT', [x['code'] for x in report['issues']])

    def test_ambiguous_divisibility_not_auto_detected(self):
        project, report = importer.convert(fixture(version='24.0.0.0', playlist=False,
                                                  extra=event(233, bytes(240))))
        self.assertEqual(project['lanes'], [])

    def test_beta_playlist_not_guessed(self):
        project, report = importer.convert(fixture(version='21.99.0.0'))
        self.assertEqual(project['lanes'], [])

    def test_note_timing_never_clamped(self):
        for start, length, accepted in ((999999, 1, True), (1000000, 1, False), (2**32 - 1, 1, False)):
            raw = bytearray(24)
            struct.pack_into('<IHHIHH', raw, 0, start, 0, 0, length, 60, 0)
            raw[16], raw[20], raw[21] = 120, 64, 100
            data = fixture(ppq=1, playlist=False, extra=event(224, raw))
            if accepted:
                project, _ = importer.convert(data)
                self.assertEqual(project['patterns'][0]['length'], 1000000)
            else:
                with self.assertRaises(importer.ImportError):
                    importer.convert(data)

    def test_clip_timing_never_clamped(self):
        raw = bytearray(60)
        struct.pack_into('<IHHIH', raw, 0, 1000000, 20480, 20481, 1, 499)
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(ppq=1, playlist=False, extra=event(233, raw)))

    def test_multiple_arrangements_rejected(self):
        extra = event(99, struct.pack('<H', 0)) + event(99, struct.pack('<H', 1))
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(extra=extra))

    def test_structure_handles_multiple_arrangements_without_export(self):
        data = fixture(extra=event(99, struct.pack('<H', 2)) + event(233, bytes(240)))
        report = importer.inspect_structure(data)
        self.assertEqual(report['eventCounts'][233], 2)
        self.assertEqual(report['sourceBytes'], len(data))
        record = report['records'][-1]
        self.assertEqual(record['precedingArrangementId'], 2)
        self.assertEqual(record['divisibleRecordSizesNotVerified'], [60, 80])
        self.assertFalse(report['layoutVerified'])
        self.assertNotIn('Synthetic', json.dumps(report))
        with self.assertRaises(importer.ImportError):
            importer.convert(data)

    def test_structure_comparison_does_not_confuse_version_change_with_music(self):
        a, b = fixture(version='24.2.2.4649'), fixture(version='21.0.0.0')
        report = importer.compare_structure(a, b)
        self.assertNotEqual(report['original']['sourceSha256'], report['rebuilt']['sourceSha256'])
        self.assertTrue(report['payloadComparisons']['233']['payloadsEqualInOrder'])
        self.assertFalse(report['payloadComparisons']['238']['presentInBoth'])
        self.assertFalse(report['musicalEquivalenceVerified'])

    def test_structure_cli_is_read_only_and_rejects_export_options(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b, out = [Path(folder) / name for name in ('original.flp', 'rebuilt.flp', 'out.aes')]
            a.write_bytes(fixture())
            b.write_bytes(fixture(version='24.2.2.4649'))
            before = a.read_bytes(), b.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(importer.main([str(a), '--compare-structure', str(b)]), 0)
            self.assertIn('payloadComparisons', json.loads(stdout.getvalue()))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(importer.main([str(a), '--structure-only', '--output', str(out)]), 2)
            self.assertFalse(out.exists())
            self.assertEqual(before, (a.read_bytes(), b.read_bytes()))

    def test_structure_still_rejects_corrupt_container(self):
        with self.assertRaises(importer.ImportError):
            importer.inspect_structure(fixture()[:-1])

    def test_explicit_arrangement_selection_preserves_others_in_report(self):
        def arrangement(identifier, start):
            clip = bytearray(60)
            struct.pack_into('<IHHIH', clip, 0, start, 20480, 20481, 384, 499)
            return event(99, struct.pack('<H', identifier)) + event(233, clip)
        data = fixture(playlist=False, extra=arrangement(2, 96) + arrangement(7, 480))
        with self.assertRaisesRegex(importer.ImportError, 'available IDs:'):
            importer.convert(data)
        project, report = importer.convert(data, arrangement_id=7)
        self.assertEqual(project['lanes'][0]['clips'][0]['start'], 5)
        self.assertEqual(report['availableArrangementIds'], [2, 7])
        self.assertEqual(report['selectedArrangementId'], 7)
        self.assertEqual(report['unmappedEvents'][0]['id'], 233)
        self.assertIn('ARRANGEMENT_NOT_SELECTED', [x['code'] for x in report['issues']])
        with self.assertRaises(importer.ImportError):
            importer.convert(data, arrangement_id=0)

    def test_arrangement_boundaries_and_ambiguity_are_not_guessed(self):
        marker = event(99, struct.pack('<H', 7))
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(extra=marker))
        for extra in (marker + marker, marker + event(100, b'\x07\0') + event(233, bytes(60)),
                      marker + event(233, bytes(60)) + event(233, bytes(60))):
            with self.assertRaises(importer.ImportError):
                importer.convert(fixture(playlist=False, extra=extra), arrangement_id=7)

    def test_measured_versions_accept_untrimmed_integer_sentinel(self):
        for version in ('21.2.1.3859', '24.2.2.4649'):
            clip = bytearray(60)
            struct.pack_into('<IHHIH', clip, 0, 384, 20480, 20481, 384, 499)
            struct.pack_into('<ii', clip, 24, -1, -1)
            project, report = importer.convert(fixture(version=version, playlist=False,
                                                       extra=event(233, clip)))
            self.assertEqual(report['clipCount'], 1)
            self.assertEqual(project['lanes'][0]['clips'][0]['sourceOffset'], 0)
            self.assertIn('MEASURED_PLAYLIST_LAYOUT', [x['code'] for x in report['issues']])

    def test_pattern_integer_trims_are_reported_not_treated_as_small_floats(self):
        clip = bytearray(60)
        struct.pack_into('<IHHIH', clip, 0, 384, 20480, 20481, 384, 499)
        struct.pack_into('<ii', clip, 24, 0, 1152)
        project, report = importer.convert(fixture(playlist=False, extra=event(233, clip)))
        self.assertEqual(project['lanes'], [])
        self.assertIn('TRIMMED_CLIP_UNMAPPED', [x['code'] for x in report['issues']])
        struct.pack_into('<i', clip, 24, -2)
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(playlist=False, extra=event(233, clip)))

    def test_trim_window_maps_ticks_without_automatic_wrap(self):
        clip = bytearray(60)
        struct.pack_into('<IHHIH', clip, 0, 960, 20480, 20481, 384, 499)
        struct.pack_into('<ii', clip, 24, 192, 576)
        project, report = importer.convert(fixture(playlist=False, extra=event(233, clip)))
        placed = project['lanes'][0]['clips'][0]
        self.assertEqual((placed['start'], placed['duration'], placed['sourceOffset']), (10, 4, 2))
        window = next(p for p in project['patterns'] if p['id'] == placed['patternId'])
        self.assertGreaterEqual(window['length'], placed['sourceOffset'] + placed['duration'])
        self.assertEqual(window['notes'], project['patterns'][0]['notes'])
        self.assertEqual(report['sourcePatternCount'], 1)
        self.assertEqual(len(report['derivedPatterns']), 1)
        self.assertIn('TRIM_WINDOW_IMPORTED', [i['code'] for i in report['issues']])

    def test_untrimmed_clip_does_not_invent_repeat_period_from_note_end(self):
        project, report = importer.convert(fixture())
        clip = project['lanes'][0]['clips'][0]
        pattern = next(p for p in project['patterns'] if p['id'] == clip['patternId'])
        self.assertGreaterEqual(pattern['length'], clip['duration'])
        self.assertIn('LOOP_PERIOD_UNVERIFIED', [i['code'] for i in report['issues']])

    def test_channel_clip_categories_do_not_load_assets_or_confuse_mixer(self):
        def channel(cid, kind, sample=False):
            return (event(64, struct.pack('<H', cid)) + event(21, bytes([kind])) +
                    event(196, 'unused.wav'.encode('utf-16-le') if sample else b'') +
                    event(201, b'') + event(201, 'Mixer effect'.encode('utf-16-le')))
        # One playlist containing all channel items.
        playlist = bytearray()
        for cid in (2, 3, 4):
            clip = bytearray(60)
            struct.pack_into('<IHHIH', clip, 0, 0, 20480, cid, 96, 499)
            playlist += clip
        extra = channel(2, 4, True) + channel(3, 5) + channel(4, 4) + event(233, playlist)
        project, report = importer.convert(fixture(playlist=False, extra=extra))
        self.assertEqual(project['lanes'], [])
        codes = [i['code'] for i in report['issues']]
        for expected in ('AUDIO_MEDIA_UNRESOLVED', 'AUTOMATION_CLIP_UNMAPPED', 'CHANNEL_CLIP_UNMAPPED'):
            self.assertEqual(codes.count(expected), 1)

    def test_trim_window_limits_and_mismatch(self):
        clip = bytearray(60)
        struct.pack_into('<IHHIH', clip, 0, 0, 20480, 20481, 2, 499)
        struct.pack_into('<ii', clip, 24, 1000000, 1000002)
        with self.assertRaises(importer.ImportError):
            importer.convert(fixture(ppq=1, playlist=False, extra=event(233, clip)))
        struct.pack_into('<ii', clip, 24, 0, 100)
        _, report = importer.convert(fixture(ppq=1, playlist=False, extra=event(233, clip)))
        self.assertIn('TRIMMED_CLIP_UNMAPPED', [i['code'] for i in report['issues']])

    def test_muted_pattern_clip_stays_muted(self):
        clip = bytearray(60)
        struct.pack_into('<IHHIH', clip, 0, 0, 20480, 20481, 96, 499)
        struct.pack_into('<H', clip, 18, 0x2040)
        project, _ = importer.convert(fixture(playlist=False, extra=event(233, clip)))
        self.assertTrue(project['lanes'][0]['clips'][0]['edits']['muted'])

    def audio_fixture(self, stretch=False, offsets=(-1.0, -1.0), duration=96):
        params = bytearray(112)
        if stretch:
            struct.pack_into('<i', params, 108, 1)
        data = (event(64, struct.pack('<H', 2)) + event(21, b'\x04') +
                event(196, 'C:\\Samples\\test.wav'.encode('utf-16-le')) +
                event(201, b'') + event(215, params))
        clip = bytearray(60)
        struct.pack_into('<IHHIH', clip, 0, 384, 20480, 2, duration, 499)
        struct.pack_into('<ff', clip, 24, *offsets)
        return fixture(playlist=False, extra=data + event(233, clip))

    def test_explicit_pcm_media_mapping_has_measured_audio_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'local.wav'
            with wave.open(str(audio), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b'\0' * 16000)
            before = audio.read_bytes()
            project, report = importer.convert(self.audio_fixture(), media_map={'c:/samples/test.wav': str(audio)})
            self.assertEqual(project['sources'][0]['path'], str(audio))
            pattern = next(p for p in project['patterns'] if p['type'] == 'audio')
            self.assertEqual(pattern['slices'][0]['length'], 8000)
            clip = project['lanes'][0]['clips'][0]
            self.assertEqual((clip['durationSeconds'], clip['sourceOffsetSeconds'], clip['edits']['playbackRate']), (.5, 0, 1))
            self.assertEqual(report['media'][0]['measurement'], 'PCM frame count')
            self.assertEqual(audio.read_bytes(), before)

    def test_audio_missing_and_unsupported_never_invent_audio(self):
        cases = (({}, self.audio_fixture(), 'AUDIO_MEDIA_UNRESOLVED'),
                 ({'c:/samples/test.wav': '/missing.wav'}, self.audio_fixture(), 'AUDIO_MEDIA_UNRESOLVED'),
                 ({'c:/samples/test.wav': '/missing.wav'}, self.audio_fixture(stretch=True), 'AUDIO_STRETCH_UNSUPPORTED'),
                 ({'c:/samples/test.wav': '/missing.wav'}, self.audio_fixture(offsets=(0, 20)), 'AUDIO_TRIM_UNVERIFIED'))
        for mapping, data, expected in cases:
            project, report = importer.convert(data, media_map=mapping)
            self.assertEqual(project['sources'], [])
            self.assertEqual(project['lanes'], [])
            self.assertIn(expected, [x['code'] for x in report['issues']])

    def test_audio_duration_does_not_infer_stretch_or_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'local.wav'
            with wave.open(str(audio), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b'\0' * 1600)
            project, report = importer.convert(self.audio_fixture(), media_map={'c:/samples/test.wav': str(audio)})
            self.assertEqual(project['sources'], [])
            self.assertIn('AUDIO_RATE_UNVERIFIED', [x['code'] for x in report['issues']])

    def test_audio_url_or_relative_paths_are_not_probed(self):
        for path in ('https://example.com/song.wav', 'relative.wav'):
            with self.assertRaises(importer.ImportError):
                importer.load_media_info(path)

    def test_invalid_media_map_cli_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source, mapping = Path(folder) / 'test.flp', Path(folder) / 'map.json'
            source.write_bytes(self.audio_fixture())
            mapping.write_text('[1, 2]')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(importer.main([str(source), '--media-map', str(mapping)]), 2)


if __name__ == '__main__':
    unittest.main()
