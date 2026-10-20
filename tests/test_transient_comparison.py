"""Paired transient diagnostics must stay independent of merge gates."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from compare_results import (analyze_pair, aggregate_suite_metrics,
                             render_summary_table, transient_comparison,
                             render_transient_block)
from render_job_summary import render_job_summary
from transient import PRE_ECHO_METRIC_VERSION, POST_ONSET_METRIC_VERSION


def row(nper=3, residual=-20, events=3):
    return dict(scenario='48k_stereo_128k', filename='clip.wav', mos=4,
                decode_valid=True, decode_error='',
                pre_echo_db=[nper] * events,
                post_onset_residual_db=[residual] * events,
                pre_echo_coverage=dict.fromkeys(('candidates', 'refined', 'eligible', 'scored'), events),
                pre_echo_lag_samples=0, pre_echo_metric_version=PRE_ECHO_METRIC_VERSION,
                post_onset_metric_version=POST_ONSET_METRIC_VERSION)


class TestTransientComparison(unittest.TestCase):
    def block(self, base, cand):
        return '\n'.join(render_transient_block(
            {'transients': transient_comparison({'k': base}, {'k': cand})}))

    def test_unavailable(self):
        for cand in ({}, row(events=0), row(events=2),
                     dict(row(), post_onset_metric_version=-1),
                     dict(row(), pre_echo_metric_version=-1),
                     dict(row(), decode_valid=False),
                     dict(row(), decode_error='bad'),
                     dict(row(), scenario='other')):
            with self.subTest(cand=cand):
                self.assertEqual(self.block(row(), cand), '')

    def test_root_matrix_decode_success(self):
        base, cand = row(), row()
        base.pop("decode_valid")
        cand.pop("decode_valid")
        base["decode_error"] = cand["decode_error"] = None
        self.assertIn("Covered pairs: 1 of 1", self.block(base, cand))

    def test_improvement(self):
        text = self.block(row(), row(nper=-2))
        self.assertIn('NPER: 3.0 → 0.0 (Δ -3.0)', text)
        self.assertIn('Improved / traded / regressed: 1 / 0 / 0', text)
        self.assertIn('no transient regression flagged', text)

    def test_exclusive_categories_at_thresholds(self):
        # Every boundary belongs to exactly one category; unchanged is implicit.
        for dnper, dres, counts in (
                (-1, 1, '0 / 1 / 0'),  # paid-for residual cost
                (-2.3, 1.9, '0 / 1 / 0'),
                (-0.9, 1, '0 / 0 / 1'),  # residual not paid for
                (-1, 0.9, '1 / 0 / 0'),
                (1, -2, '0 / 0 / 1'),
                (0.9, 0.9, '0 / 0 / 0')):
            with self.subTest(dnper=dnper, dres=dres):
                text = self.block(row(), row(nper=3+dnper, residual=-20+dres))
                self.assertIn('Improved / traded / regressed: ' + counts, text)
                if counts.endswith('/ 1'):
                    self.assertIn('1 clip(s) regressed', text)
                    self.assertIn('clip.wav @', text)
                else:
                    self.assertIn('no transient regression flagged', text)
                    self.assertNotIn('clip.wav @', text)

    def test_nper_flag(self):
        self.assertIn('clip.wav @ 48k_stereo_128k: dNPER +1.0, dRes +0.0 (events 3)',
                      self.block(row(), row(nper=4)))

    def test_residual_flag_and_null(self):
        cand = row(residual=-19)
        cand['post_onset_residual_db'][0] = None
        self.assertIn('dNPER +0.0, dRes +1.0', self.block(row(), cand))
        cand['post_onset_residual_db'] = [None] * 3
        self.assertEqual(self.block(row(), cand), '')

    def test_report_and_gate_independence(self):
        with tempfile.TemporaryDirectory() as td:
            base, cand = Path(td)/'base.json', Path(td)/'cand.json'
            base.write_text(json.dumps({'matrix': {'k': row()}}))
            cand.write_text(base.read_text())
            before = analyze_pair(base, cand)
            cand.write_text(json.dumps({'matrix': {'k': row(nper=5)}}))
            after = analyze_pair(base, cand)
        stripped = copy.deepcopy(after)
        stripped['transients'] = before['transients']
        stripped['scenario_stats'] = copy.deepcopy(before['scenario_stats'])
        self.assertEqual(before, stripped)
        metrics = aggregate_suite_metrics([('test', after)])
        summary_table_text = '\n'.join(render_summary_table(metrics, 'MOS', 'ABR'))
        self.assertIn('1 clip(s) regressed', summary_table_text)
        self.assertIn('| **Avg Transient Δ** |', summary_table_text)
        self.assertIn('| **Worst Transient Drop** |', summary_table_text)
        self.assertIn('1 clip(s) regressed', render_job_summary(metrics))
        self.assertIn('1 clip(s) regressed', render_job_summary(
            {'matrix': {'k': row(nper=5)}}, baseline={'matrix': {'k': row()}}))

    def test_scenario_performance_transient_column(self):
        with tempfile.TemporaryDirectory() as td:
            base, cand = Path(td)/'test_base.json', Path(td)/'test_cand.json'
            base.write_text(json.dumps({'matrix': {'k': row(nper=3)}}))
            cand.write_text(json.dumps({'matrix': {'k': row(nper=1)}}))
            suite_data = analyze_pair(base, cand)
            sc_stats = suite_data['scenario_stats']['48k_stereo_128k']
            self.assertEqual(sc_stats['transient_count'], 1)
            self.assertEqual(sc_stats['transient_wins'], 1)
            self.assertEqual(sc_stats['transient_nper_delta_sum'], -2.0)

    def test_coverage_and_top_three(self):
        base = {str(i): row() for i in range(5)}
        cand = {str(i): dict(row(nper=4+i), filename=f'{i}.wav') for i in range(5)}
        for k in base:
            base[k]['filename'] = cand[k]['filename']
        cand['0'] = dict(cand['0'], post_onset_metric_version=-1)
        text = '\n'.join(render_transient_block({'transients': transient_comparison(base, cand)}))
        self.assertIn('Covered pairs: 4 of 5', text)
        self.assertEqual(text.count(' @ '), 3)
        self.assertLess(text.index('4.wav @'), text.index('3.wav @'))
