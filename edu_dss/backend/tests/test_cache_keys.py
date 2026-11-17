from app.cache_keys import explanation_key, leaderboard_key


def test_colon_in_ids_cannot_reach_another_course_key():
    assert explanation_key("CS101", "sec2:lec1") != explanation_key("CS101:sec2", "lec1")
    assert leaderboard_key("alice", "x:y") != leaderboard_key("alice:x", "y")


def test_plain_ids_keep_previous_key_format():
    assert explanation_key("CS101", "lec-1.2_a") == "edu:explanation:CS101:lec-1.2_a"
    assert leaderboard_key("sub-1", "demo-baseline-v1") == "edu:leaderboard:sub-1:demo-baseline-v1"
