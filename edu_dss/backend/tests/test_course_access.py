import unittest

from app.course_access import course_is_authorized


class CourseAccessTests(unittest.TestCase):
    def test_student_requires_enrollment(self):
        self.assertTrue(course_is_authorized("student", "math-1", ["math-1"]))
        self.assertFalse(course_is_authorized("student", "physics-1", ["math-1"]))

    def test_instructor_requires_teaching_assignment(self):
        self.assertTrue(course_is_authorized("instructor", "math-1", (), ["math-1"]))
        self.assertFalse(course_is_authorized("instructor", "physics-1", (), ["math-1"]))

    def test_admin_is_global_and_unknown_role_is_denied(self):
        self.assertTrue(course_is_authorized("admin", "any-course"))
        self.assertFalse(course_is_authorized("viewer", "math-1", ["math-1"], ["math-1"]))


if __name__ == "__main__":
    unittest.main()
