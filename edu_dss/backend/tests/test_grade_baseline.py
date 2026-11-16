import math
import unittest

from app.grade_baseline import _fit_ols, predict_demo_grade


class GradeBaselineTests(unittest.TestCase):
    def test_ols_coefficients_are_closed_form(self):
        intercept, slope = _fit_ols(((1.0, 2.0), (2.0, 4.0), (3.0, 6.0)))
        self.assertAlmostEqual(intercept, 0.0)
        self.assertAlmostEqual(slope, 2.0)

    def test_invalid_training_rows_are_rejected(self):
        for rows in (
            (),
            ((1.0, 2.0),),
            ((1.0, 2.0), (1.0, 3.0)),
            ((1.0, 2.0), (2.0, math.inf)),
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                _fit_ols(rows)

    def test_demo_response_is_bounded_and_explicitly_synthetic(self):
        result = predict_demo_grade(2.5)
        self.assertGreaterEqual(result["predicted_grade"], 0)
        self.assertLessEqual(result["predicted_grade"], 4)
        self.assertEqual(result["mode"], "demo_fixture_only")
        self.assertEqual(result["training_rows"], 7)
        self.assertEqual(result["synthetic_holdout_metrics"]["samples"], 2)
        with self.assertRaises(ValueError):
            predict_demo_grade(float("nan"))


if __name__ == "__main__":
    unittest.main()
