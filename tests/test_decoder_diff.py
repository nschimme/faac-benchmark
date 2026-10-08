"""Tiny fake-faad fixtures exercise exact equality and gate selection."""
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_results
import phase5_decoder_diff as decoder


class TestDecoderDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stream = self.root / 'tiny.m4a'
        self.stream.write_bytes(b'fixture')
        self.streams = [{'path': self.stream, 'file': 'tiny.m4a',
                         'scenario': 'test', 'profile': 'LC'}]
        self.base = self.binary('base', [10, 20, 30])

    def tearDown(self):
        self.tmp.cleanup()

    def binary(self, name, samples, fail=False):
        path = self.root / name
        path.write_text(f'''#!{sys.executable}
import os, struct, sys, wave
if {fail!r}: sys.exit(1)
out = sys.argv[sys.argv.index('-o') + 1]
if os.path.exists(out): sys.exit(2)
with wave.open(out, 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(44100)
    w.writeframes(struct.pack('<{len(samples)}h', *{samples!r}))
''')
        path.chmod(0o755)
        return str(path)

    def gate(self, block, enabled=None):
        suite = {'gates': [], 'has_regression': False}
        with patch.object(compare_results, 'ENABLED_GATES', enabled):
            compare_results.check_decoder(suite, {}, {'decoder_diff': block})
        return suite, suite['gates'][0]

    def test_identical_pass(self):
        block = decoder.compare_streams(self.base, self.base, self.streams)
        self.assertEqual(block, {'streams': 1, 'unchanged': 1, 'changed': [], 'failed': []})
        suite, gate = self.gate(block)
        self.assertEqual(gate['status'], 'pass')
        self.assertFalse(suite['has_regression'])

    def test_single_lsb_fails_naming_stream(self):
        block = decoder.compare_streams(self.base, self.binary('cand', [10, 21, 30]), self.streams)
        suite, gate = self.gate(block)
        self.assertTrue(suite['has_regression'])
        self.assertIn('tiny.m4a', gate['detail'])
        self.assertEqual(block['changed'][0]['first_diff_sample'], 1)
        self.assertEqual(block['changed'][0]['max_abs_diff'], 1)

    def test_candidate_failure_fails(self):
        block = decoder.compare_streams(self.base, self.binary('cand', [], True), self.streams)
        self.assertEqual(block['failed'][0]['side'], 'cand')
        self.assertEqual(self.gate(block)[1]['status'], 'fail')

    def test_frame_count_fails(self):
        block = decoder.compare_streams(self.base, self.binary('cand', [10, 20]), self.streams)
        self.assertEqual(block['changed'][0]['base_frames'], 3)
        self.assertEqual(block['changed'][0]['cand_frames'], 2)
        self.assertEqual(block['changed'][0]['first_diff_sample'], 2)
        self.assertEqual(self.gate(block)[1]['status'], 'fail')

    def test_allowed_change_does_not_fail(self):
        block = decoder.compare_streams(self.base, self.binary('cand', [11, 20, 30]), self.streams)
        suite, gate = self.gate(block, {'footprint', 'haze'})
        self.assertFalse(suite['has_regression'])
        self.assertEqual(gate['status'], 'skip')

    def test_missing_baseline_skips(self):
        block = decoder.compare_streams(str(self.root / 'absent'), self.base, self.streams)
        self.assertIn('baseline faad', block['skipped'])
        self.assertEqual(self.gate(block)[1]['status'], 'skip')

    def test_baseline_decode_failure_is_not_candidate_regression(self):
        block = decoder.compare_streams(self.binary('bad', [], True), self.base, self.streams)
        self.assertEqual(self.gate(block)[1]['status'], 'pass')

    def test_no_streams_skips(self):
        self.assertIn('skipped', decoder.compare_streams(self.base, self.base, []))

    def test_missing_block_skips(self):
        self.assertEqual(self.gate(None)[1]['status'], 'skip')

    def test_error_block_fails_and_renders(self):
        error = 'exit code 7: decoder crashed'
        suite, gate = self.gate({'error': error})
        self.assertEqual(gate['status'], 'fail')
        self.assertTrue(suite['has_regression'])
        self.assertEqual(gate['detail'], 'decoder diff did not run: ' + error)
        expected = 'decoder output: DID NOT RUN (' + error + ')'
        self.assertEqual(decoder.summary({'error': error}), expected)
        from render_job_summary import render_job_summary
        self.assertIn(expected, render_job_summary({'decoder_diff': {'error': error}}))

    def test_missing_candidate_with_baseline_fails(self):
        for baseline in ({'streams': 1, 'unchanged': 1, 'changed': [], 'failed': []},
                         {'error': 'crashed'}):
            with self.subTest(baseline=baseline):
                suite = {'gates': [], 'has_regression': False}
                compare_results.check_decoder(suite, {'decoder_diff': baseline}, {})
                self.assertEqual(suite['gates'][0]['status'], 'fail')
                self.assertEqual(suite['gates'][0]['detail'], 'candidate produced no decoder diff')

    def test_missing_both_and_explicit_skipped_skip(self):
        for base, cand in (({}, {}),
                           ({'decoder_diff': {'streams': 1}}, {'decoder_diff': {'skipped': 'unavailable'}}),
                           ({'decoder_diff': {'skipped': 'unavailable'}}, {})):
            with self.subTest(base=base, cand=cand):
                suite = {'gates': [], 'has_regression': False}
                compare_results.check_decoder(suite, base, cand)
                self.assertEqual(suite['gates'][0]['status'], 'skip')

    def test_phase5_failure_preserves_results_and_completes_run(self):
        import run_benchmark
        from types import SimpleNamespace
        result = self.root / 'run.json'
        argv = ['run_benchmark.py', 'test', str(result), '--skip-mos', '--skip-haze',
                '--decoder-ref-bin', self.base, '--decoder-bin', self.base]
        for failure, expected in ((subprocess.CalledProcessError(7, 'phase5', stderr='context\ndecoder crashed\n'),
                                   'exit code 7: decoder crashed'),
                                  (OSError('launch failed'), 'OSError: launch failed')):
            with self.subTest(failure=failure):
                def fake_run(cmd, **kwargs):
                    if cmd[1].endswith('phase1_encode.py'):
                        result.write_text(json.dumps({'matrix': {'preserved': {}}, 'other': 42}))
                    elif cmd[1].endswith('phase5_decoder_diff.py'):
                        raise failure
                    return subprocess.CompletedProcess(cmd, 0)
                output = io.StringIO()
                with patch.object(sys, 'argv', argv), patch.object(run_benchmark.subprocess, 'run', side_effect=fake_run), \
                     patch.object(run_benchmark, 'get_decoder_instance', return_value=SimpleNamespace(name='fake')), \
                     redirect_stdout(output):
                    run_benchmark.main()
                data = json.loads(result.read_text())
                self.assertEqual(data['decoder_diff'], {'error': expected})
                self.assertEqual(data['other'], 42)
                self.assertEqual(data['matrix'], {'preserved': {}})
                self.assertEqual(data['decoder_name'], 'fake')
                self.assertIn('Warning: Phase 5', output.getvalue())
                self.assertIn('All benchmarks complete.', output.getvalue())

    def test_matrix_selection_and_atomic_storage(self):
        data = {'matrix': {'x': {'aac': 'tiny.m4a', 'scenario': 'test', 'object_type': 'LC'},
                           'y': {'aac': 'other.opus'}}}
        streams = decoder.matrix_streams(data, self.root)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]['profile'], 'LC')
        path = self.root / 'result.json'
        path.write_text(json.dumps(data))
        decoder.store_block(path, {'skipped': 'test'})
        self.assertEqual(json.loads(path.read_text())['matrix'], data['matrix'])
        with self.assertRaises(TypeError):
            decoder.store_block(path, {'bad': object()})
        self.assertEqual(json.loads(path.read_text())['decoder_diff'], {'skipped': 'test'})
        self.assertFalse(Path(str(path) + '.tmp').exists())

    def test_cli_stores_block_and_report_keeps_detail_in_cases(self):
        result = self.root / 'test_cand.json'
        baseline = self.root / 'test_base.json'
        data = {'matrix': {'tiny': {'aac': 'tiny.m4a', 'scenario': 'test',
                                   'object_type': 'LC'}}}
        result.write_text(json.dumps(data))
        baseline.write_text(json.dumps(data))
        candidate = self.binary('cand', [10, 21, 30])
        repo = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, str(repo / 'phase5_decoder_diff.py'),
                        str(result), str(self.root), '--decoder-ref-bin', self.base,
                        '--decoder-bin', candidate], check=True, capture_output=True)
        block = json.loads(result.read_text())['decoder_diff']
        self.assertEqual(block['changed'][0]['profile'], 'LC')
        summary = self.root / 'summary.md'
        report = self.root / 'report.md'
        cases = self.root / 'cases.md'
        proc = subprocess.run([sys.executable, str(repo / 'compare_results.py'),
                               str(self.root), '--gates', 'decoder', '--skip-graphs',
                               '--summary-output', str(summary), '--output', str(report),
                               '--cases-output', str(cases)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(summary.read_text().count('decoder output:'), 1)
        self.assertNotIn('first_diff_sample', report.read_text())
        self.assertIn('tiny.m4a', cases.read_text())
        self.assertIn('first_diff_sample', cases.read_text())
        decoder.store_block(result, {'error': 'exit code 7: decoder crashed'})
        proc = subprocess.run(proc.args, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn('decoder output: DID NOT RUN (exit code 7: decoder crashed)', summary.read_text())

    def test_summary_one_line_no_details(self):
        block = decoder.compare_streams(self.base, self.binary('cand', [11, 20, 30]), self.streams)
        self.assertEqual(decoder.summary(block), 'decoder output: CHANGED on 1 of 1 streams (see cases)')


if __name__ == '__main__':
    unittest.main()
