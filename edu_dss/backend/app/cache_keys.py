"""Redis keys built from caller-controlled IDs.

Each part is percent-encoded so ':' (the key separator) inside an ID cannot reach another
tenant's key: course "CS101" + lecture "sec2:lec1" must not read course "CS101:sec2" lecture "lec1".
Plain [A-Za-z0-9_.-] IDs keep their previous key unchanged.
"""
from urllib.parse import quote


def _part(value) -> str:
    return quote(str(value), safe="")


def explanation_key(course_id, lecture_id) -> str:
    return f"edu:explanation:{_part(course_id)}:{_part(lecture_id)}"


def leaderboard_key(subject, model_ref) -> str:
    return f"edu:leaderboard:{_part(subject)}:{_part(model_ref)}"
