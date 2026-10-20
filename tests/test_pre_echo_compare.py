import unittest

from compare_results import pre_echo_clip_comparison


def row(values, *, eligible=None):
    count = len(values)
    eligible = count if eligible is None else eligible
    return {
        'pre_echo_db': values,
        'pre_echo_coverage': {
            'candidates': 8, 'refined': 7, 'eligible': eligible, 'scored': count,
        },
        'pre_echo_metric_version': 1,
        'pre_echo_lag_samples': 0,
    }


class PreEchoComparisonTests(unittest.TestCase):
    def test_current_rows_compare_positive_excess_and_counts(self):
        baseline = row([-3.0, 3.0, 6.0], eligible=5)
        candidate = row([-9.0, 1.0, 5.0, 7.0], eligible=6)
        result = pre_echo_clip_comparison(candidate, baseline)
        self.assertEqual(result['baseline_mean'], 3.0)
        self.assertEqual(result['candidate_mean'], 3.25)
        self.assertEqual(result['delta'], 0.25)
        self.assertEqual(result['baseline_events'], 3)
        self.assertEqual(result['candidate_events'], 4)
        self.assertEqual(result['baseline_eligible'], 5)
        self.assertEqual(result['candidate_eligible'], 6)

    def test_legacy_rows_are_not_compared(self):
        legacy = {'pre_echo_db': [2.0], 'pre_echo_coverage': {},
                  'pre_echo_lag_samples': 0}
        self.assertIsNone(pre_echo_clip_comparison(row([1.0]), legacy))

    def test_zero_and_negative_values_do_not_cancel_positive_excess(self):
        baseline = row([-8.0, 0.0, -2.0])
        candidate = row([-3.0, 4.0, -7.0])
        result = pre_echo_clip_comparison(candidate, baseline)
        self.assertEqual(result['baseline_mean'], 0.0)
        self.assertAlmostEqual(result['candidate_mean'], 4.0 / 3.0)
        self.assertAlmostEqual(result['delta'], 4.0 / 3.0)

    def test_no_scored_events_is_not_reported_as_zero(self):
        self.assertIsNone(pre_echo_clip_comparison(row([]), row([0.0])))
        self.assertIsNone(pre_echo_clip_comparison(row([0.0]), row([])))

    def test_partial_coverage_is_reported_with_eligible_counts(self):
        baseline = row([1.0, 3.0], eligible=4)
        candidate = row([0.0, 4.0], eligible=5)
        result = pre_echo_clip_comparison(candidate, baseline)
        self.assertEqual(result['baseline_events'], 2)
        self.assertEqual(result['candidate_events'], 2)
        self.assertEqual(result['baseline_eligible'], 4)
        self.assertEqual(result['candidate_eligible'], 5)


if __name__ == '__main__':
    unittest.main()
