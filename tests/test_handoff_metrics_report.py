import unittest

from core.handoff_metrics_report import build_report


class HandoffMetricsReportTests(unittest.TestCase):
    def test_report_groups_results_and_exposes_unknown_cost_rate(self):
        report = build_report(
            [
                {
                    "type": "handoff_started",
                    "handoff_id": "h1",
                    "template_id": "pytest",
                    "action_id": "test.unit",
                    "estimated_direct_cost": "small",
                },
                {
                    "type": "handoff_finished",
                    "handoff_id": "h1",
                    "template_id": "pytest",
                    "action_id": "test.unit",
                    "result": "clean",
                    "wall_time": 2.0,
                },
                {
                    "type": "handoff_started",
                    "handoff_id": "h2",
                    "template_id": "python_module",
                    "action_id": "delivery.preview",
                },
                {
                    "type": "handoff_finished",
                    "handoff_id": "h2",
                    "template_id": "python_module",
                    "action_id": "delivery.preview",
                    "result": "failed",
                    "wall_time": 4.0,
                },
            ]
        )

        self.assertEqual(report["total_handoffs"], 2)
        self.assertEqual(report["clean"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["unknown_cost_count"], 1)
        self.assertEqual(report["unknown_cost_rate"], 0.5)
        self.assertEqual(report["by_template"]["python_module"]["unknown_cost_rate"], 1.0)
        self.assertEqual(report["by_action"]["test.unit"]["wall_time_avg"], 2.0)


if __name__ == "__main__":
    unittest.main()
