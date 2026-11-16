"""Fail-closed course authorization using short-lived IdP JWT membership claims."""

def course_is_authorized(role, course_id, course_ids=(), teaching_course_ids=()):
    """Admin is global; students and instructors require resource-specific membership."""
    if role == "admin":
        return True
    if role == "student":
        return course_id in course_ids
    if role == "instructor":
        return course_id in teaching_course_ids
    return False
