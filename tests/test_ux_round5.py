"""Vòng 5 UX: kiểm tra tĩnh (đọc file dạng văn bản) các sửa đổi giao diện ở báo cáo 04 §5.

Không import gì nặng: mỗi test chỉ đọc HTML/XML/Kotlin/Python dưới dạng chuỗi.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


APP = "static/app/index.html"
ADMIN = "static/admin/index.html"
MANAGE = "static/admin/manage.html"
ANDROID = "android/app/src/main"


def _css_rule(html: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", html)
    assert m, f"không thấy luật CSS {selector}"
    return m.group(1)


# ------------------------------------------------------------------ app thuê bao
def test_app_history_rows_are_keyboard_buttons_with_expanded_state():
    s = read(APP)
    assert 'role="button" tabindex="0" aria-expanded=' in s
    assert "el.onkeydown" in s and "ev.key === 'Enter'" in s and "ev.key === ' '" in s
    assert "setAttribute('aria-expanded'" in s


def test_app_overlay_is_modal_dialog_with_focus_management():
    s = read(APP)
    assert re.search(r'id="ov"[^>]*role="dialog"[^>]*aria-modal="true"', s)
    assert "function openOv" in s and "function closeOv" in s
    assert "ev.key === 'Escape'" in s and "ev.key === 'Tab'" in s
    assert "OV_OPENER" in s and "opener.focus()" in s


def test_app_decorative_emoji_hidden_from_screen_readers():
    s = read(APP)
    assert '<i aria-hidden="true">🏠</i>' in s
    assert '<div class="tip"><div class="ic" aria-hidden="true">' in s
    assert '<button data-k="LEGIT"><span aria-hidden="true">📞</span>' in s


def test_app_minimum_font_sizes_and_touch_targets():
    s = read(APP)
    assert "font-size:12px" in _css_rule(s, ".stat span")
    assert "font-size:12px" in _css_rule(s, ".chip")
    assert "font-size:11" not in _css_rule(s, "nav button")
    assert "min-height:44px" in _css_rule(s, ".tt")
    assert "min-height:44px" in _css_rule(s, "a.rm")
    assert 'class="rm"' in s


def test_app_active_nav_uses_brand_and_aria_current():
    s = read(APP)
    rule = _css_rule(s, "nav button.on")
    assert "var(--brand)" in rule and "var(--red)" not in rule
    assert "aria-current" in s and "setAttribute('aria-current', 'page')" in s


def test_app_focus_visible_outline():
    assert ":focus-visible{outline" in read(APP)


def test_app_lookup_loading_and_visible_errors():
    s = read(APP)
    assert "Đang tra cứu…" in s
    assert "Không tra cứu được: ${esc(e.message)}" in s
    assert 'id="netErr" role="alert"' in s
    assert "Không tải được lịch sử cuộc gọi" in s
    assert "} catch(e){}\n}" not in s.split("async function refresh")[1].split("// ---")[0]


# ------------------------------------------------------------------ bảng vận hành
def test_admin_headings_sentence_case_without_wide_tracking():
    s = read(ADMIN)
    rule = _css_rule(s, ".ph h2")
    assert "uppercase" not in rule and "letter-spacing:.1" not in rule
    assert "uppercase" not in _css_rule(s, ".k .l")
    assert "letter-spacing:.1em" not in s
    assert 'data-vi="Bằng chứng" data-en="Evidence">Bằng chứng<' in s
    assert 'data-vi="Đăng nhập vận hành"' in s


def test_admin_min_label_font_12px():
    s = read(ADMIN)
    assert "10.5px" not in s
    assert "font-size:12px" in _css_rule(s, ".k .l")


def test_admin_evidence_drawer_is_dialog():
    s = read(ADMIN)
    assert re.search(r'id="drawer"[^>]*role="dialog"[^>]*aria-modal="true"', s)
    assert "function openDrawer" in s and "function closeDrawer" in s
    assert "ev.key === 'Escape'" in s
    assert "target.focus()" in s


def test_admin_canvas_chart_described():
    s = read(ADMIN)
    assert re.search(r'<canvas id="lt"[^>]*role="img"[^>]*aria-label="', s)
    assert "c.setAttribute('aria-label'" in s


def test_admin_empty_feed_and_poll_error_banner():
    s = read(ADMIN)
    assert "Chưa có cảnh báo" in s
    assert 'id="pollErr" role="alert"' in s
    assert "catch(e){} } setTimeout(poll" not in s


def test_admin_approve_button_brand_not_red():
    s = read(ADMIN)
    assert '<button class="pri" data-ok=' in s
    assert '<button class="red" data-ok=' not in s


# ------------------------------------------------------------------ trang quản trị
def test_manage_tabs_are_aria_tablist_with_arrow_keys():
    s = read(MANAGE)
    assert 'role="tablist"' in s and s.count(' role="tab" ') == 4 and s.count('role="tabpanel"') == 4
    assert 'aria-selected="true"' in s and 'aria-controls="reports"' in s
    assert "setAttribute('aria-selected'" in s
    assert "'ArrowRight'" in s and "'ArrowLeft'" in s


def test_manage_has_watermark_like_dashboard():
    s = read(MANAGE)
    assert '<div id="wm"' in s and "#wm{position:fixed" in s
    assert "function watermark" in s and "ME.name" in s.split("function watermark")[1][:200]
    assert "setInterval(watermark" in s


def test_manage_confirms_before_locking_staff():
    s = read(MANAGE)
    assert "confirm(`Khoá tài khoản" in s


def test_manage_inputs_labelled_and_roles_vietnamese():
    s = read(MANAGE)
    for tag in re.findall(r"<(?:input|select)\b[^>]*>", s):
        assert "aria-label=" in tag, tag
    assert "viewer — chỉ xem" not in s
    assert ">Nhân viên (chỉ xem)<" in s and ">Chuyên viên<" in s


def test_manage_loading_indicator():
    s = read(MANAGE)
    assert "function loading(" in s and "Đang tải…" in s
    assert "loading('#obody'" in s and "loading('#rbody'" in s


# ------------------------------------------------------------------ Android
def test_android_brand_is_cyan():
    colors = read(f"{ANDROID}/res/values/colors.xml")
    assert re.search(r'<color name="brand">#0E7490</color>', colors, re.I)
    assert not re.search(r'<color name="brand">#E11D48', colors, re.I)
    assert "@color/brand" in read(f"{ANDROID}/res/values/themes.xml")


def test_android_release_strings_have_no_dev_hints():
    main = read(f"{ANDROID}/res/values/strings.xml")
    assert "run_demo" not in main and "python" not in main.lower() and "10.0.2.2" not in main
    debug = read("android/app/src/debug/res/values/strings.xml")
    assert "run_demo" in debug and 'name="error_body"' in debug


def test_android_server_input_uri_hint_and_progress():
    kt = read(f"{ANDROID}/java/vn/lachan/fraudshield/MainActivity.kt")
    assert "TYPE_TEXT_VARIATION_URI" in kt and "import android.text.InputType" in kt
    assert "setHint(R.string.server_input_hint)" in kt
    assert "R.id.progress" in kt and "progress.visibility = View.VISIBLE" in kt and "progress.visibility = View.GONE" in kt
    layout = read(f"{ANDROID}/res/layout/activity_main.xml")
    assert '<ProgressBar' in layout and 'android:id="@+id/progress"' in layout
    assert 'android:contentDescription="@string/error_icon_desc"' in layout
    strings = read(f"{ANDROID}/res/values/strings.xml")
    for name in ("server_input_hint", "server_input_desc", "loading", "error_icon_desc"):
        assert f'name="{name}"' in strings


# ------------------------------------------------------------------ EduDSS Streamlit
def test_edu_ui_quotes_path_segments_and_validates():
    s = read("edu_dss/ui/app.py")
    assert "from urllib.parse import quote" in s and 'quote(str(value).strip(), safe="")' in s
    assert "/leaderboard/{seg(model_ref)}" in s
    assert "/explanations/{seg(course)}/{seg(lecture)}" in s
    assert "/explanations/{course}/{lecture}" not in s
    assert "def missing(" in s and "Vui lòng nhập" in s


def test_edu_ui_friendly_errors_and_structured_results():
    s = read("edu_dss/ui/app.py")
    for code in ("401", "403", "404", "413", "429"):
        assert f"    {code}: " in s
    assert "status >= 500" in s
    assert ".metric(" in s and "st.dataframe(" in s
    assert 'st.expander("Dữ liệu gốc (JSON)")' in s
    assert "st.json(r.json())" not in s and "st.json(prediction)" not in s


def test_edu_ui_role_from_jwt_and_logout():
    s = read("edu_dss/ui/app.py")
    assert "base64.urlsafe_b64decode" in s and 'token.split(".")[1]' in s
    assert 'ROLE_FROM_CLAIM' in s
    assert 'st.sidebar.button("Đăng xuất"' in s and 'st.session_state.pop(key, None)' in s
    assert '["Student", "Instructor", "Admin"]' not in s
    assert "help=" in s


def test_edu_ui_jwt_decode_is_display_only_and_safe():
    """Chạy hàm giải mã trên chuỗi mẫu mà không import streamlit/httpx."""
    import base64
    import json as _json
    src = read("edu_dss/ui/app.py")
    fn = src.split("def jwt_claims")[1].split("\nROLE_FROM_CLAIM")[0]
    ns: dict = {"base64": base64, "json": _json}
    exec("def jwt_claims" + fn, ns)  # noqa: S102 - mã của chính repo, chỉ hàm thuần
    body = base64.urlsafe_b64encode(_json.dumps({"role": "student", "sub": "s1"}).encode()).decode().rstrip("=")
    assert ns["jwt_claims"](f"h.{body}.sig") == {"role": "student", "sub": "s1"}
    assert ns["jwt_claims"]("khong-phai-jwt") is None
    assert ns["jwt_claims"]("a.!!!.b") is None
