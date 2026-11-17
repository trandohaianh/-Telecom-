"""Stateless Streamlit view. All work and role checks live behind FastAPI."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from concurrent.futures import Future
from urllib.parse import quote

import httpx
import streamlit as st

API_BASE = os.getenv("EDU_API_BASE_URL", "http://fastapi-backend:8000")


class ApiPool:
    """One long-lived AsyncClient on one owned loop; Streamlit reruns stay synchronous."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="edu-httpx-loop", daemon=True)
        self.thread.start()
        self.client = asyncio.run_coroutine_threadsafe(self._create(), self.loop).result(timeout=5)

    async def _create(self):
        return httpx.AsyncClient(base_url=API_BASE, timeout=httpx.Timeout(8.0, connect=2.0),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20), follow_redirects=False)

    async def _request(self, method, path, token, kwargs):
        return await self.client.request(method, path,
            headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def request(self, method, path, token, **kwargs):
        wait_timeout = kwargs.pop("_wait_timeout", 10)
        request_timeout = kwargs.pop("_request_timeout", None)
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        future: Future = asyncio.run_coroutine_threadsafe(
            self._request(method, path, token, kwargs), self.loop)
        return future.result(timeout=wait_timeout)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.client.aclose(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


@st.cache_resource
def api_pool() -> ApiPool:
    return ApiPool()


def api(method: str, path: str, token: str, **kwargs):
    return api_pool().request(method, path, token, **kwargs)


def seg(value: str) -> str:
    """Mã hoá một đoạn đường dẫn URL: "/" hay "?" trong mã khoá học không được đổi endpoint."""
    return quote(str(value).strip(), safe="")


# Thông báo lỗi thân thiện theo mã HTTP; chi tiết kỹ thuật để trong phần mở rộng.
ERROR_TEXT = {
    400: "Yêu cầu không hợp lệ. Hãy kiểm tra lại các ô đã nhập.",
    401: "Phiên đăng nhập không hợp lệ hoặc đã hết hạn. Hãy đăng xuất và dán mã truy cập mới.",
    403: "Bạn không có quyền thực hiện thao tác này (vai trò hoặc khoá học không khớp).",
    404: "Không tìm thấy dữ liệu. Có thể mã nhập sai hoặc dữ liệu chưa được chuẩn bị.",
    413: "Dữ liệu gửi lên quá lớn. Hãy rút gọn nội dung rồi thử lại.",
    422: "Dữ liệu nhập chưa đúng định dạng.",
    429: "Bạn gửi quá nhiều yêu cầu. Hãy chờ một lát rồi thử lại.",
}


def friendly_error(status: int) -> str:
    if status in ERROR_TEXT:
        return ERROR_TEXT[status]
    if status >= 500:
        return "Máy chủ đang gặp sự cố hoặc tạm thời không phản hồi. Hãy thử lại sau ít phút."
    return f"Yêu cầu không thành công (mã {status})."


def show_error(response) -> None:
    st.error(friendly_error(response.status_code))
    with st.expander("Chi tiết kỹ thuật"):
        st.code(f"HTTP {response.status_code}\n{response.text[:500]}")


def call(method: str, path: str, token: str, **kwargs):
    """Gọi API; lỗi mạng hiện thông báo tiếng Việt thay vì traceback."""
    try:
        return api(method, path, token, **kwargs)
    except (httpx.HTTPError, TimeoutError):
        st.error("Không kết nối được tới máy chủ API. Hãy kiểm tra mạng rồi thử lại.")
        return None


def missing(**fields: str) -> bool:
    """Kiểm tra các ô bắt buộc trước khi gửi yêu cầu; báo rõ ô nào còn trống."""
    empty = [label for label, value in fields.items() if not str(value or "").strip()]
    if empty:
        st.warning("Vui lòng nhập: " + ", ".join(empty) + ".")
    return bool(empty)


def jwt_claims(token: str) -> dict | None:
    """Giải mã phần payload của JWT KHÔNG kiểm chữ ký, chỉ để hiển thị vai trò.

    API vẫn xác thực chữ ký và phân quyền ở mọi endpoint; giá trị này không cấp thêm quyền gì.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
        return claims if isinstance(claims, dict) else None
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


ROLE_FROM_CLAIM = {"student": "Sinh viên", "instructor": "Giảng viên", "admin": "Quản trị"}


def show_raw(data) -> None:
    with st.expander("Dữ liệu gốc (JSON)"):
        st.json(data)


def show_structured(data) -> None:
    """Hiển thị dữ liệu chưa biết trước cấu trúc: bảng nếu là danh sách bản ghi, ngược lại liệt kê."""
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        st.dataframe(data, use_container_width=True)
    elif isinstance(data, list):
        for x in data:
            st.markdown(f"- {x}")
    elif isinstance(data, dict):
        st.table([{"Mục": k, "Giá trị": json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}
                  for k, v in data.items()])
    elif data is not None:
        st.write(data)


def require_token():
    token = st.session_state.get("jwt", "")
    if not token:
        st.info("Dán mã truy cập (JWT) do hệ thống đăng nhập của trường cấp để bắt đầu.")
        st.session_state.jwt = st.text_input(
            "Mã truy cập (JWT)", type="password",
            help="Mã do hệ thống đăng nhập/IdP cấp. Mã chỉ giữ trong phiên này và mất khi bấm Đăng xuất.")
        st.stop()
    return token


def submit_task(path: str, payload: dict, token: str):
    response = call("POST", path, token, json=payload)
    if response is None:
        return
    if response.is_error:
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        detail = error_body.get("detail", {}) if isinstance(error_body, dict) else {}
        if response.status_code == 503 and isinstance(detail, dict) and detail.get("task_id"):
            task_id = detail["task_id"]
            st.session_state.task_id = task_id
            st.session_state.submission_uncertain_task_id = task_id
            st.warning(f"Chưa xác định được broker đã nhận tác vụ hay chưa. Đang theo dõi {task_id}; đừng gửi lại trước khi đối soát.")
            return
        show_error(response)
        return
    task_id = response.json()["task_id"]
    st.session_state.task_id = task_id
    st.session_state.pop("submission_uncertain_task_id", None)
    st.success(f"Đã xếp hàng tác vụ: {task_id}")


st.set_page_config(page_title="Edu DSS", layout="wide")
st.title("Edu DSS · Hỗ trợ ra quyết định học tập")
token = require_token()

if st.sidebar.button("Đăng xuất", help="Xoá mã truy cập khỏi phiên làm việc này."):
    for key in ("jwt", "task_id", "submission_uncertain_task_id"):
        st.session_state.pop(key, None)
    st.rerun()

claims = jwt_claims(token)
claim_role = ROLE_FROM_CLAIM.get(str((claims or {}).get("role", "")).lower())
if claim_role:
    role = claim_role
    st.sidebar.markdown(f"**Vai trò:** {role}")
    if claims.get("sub"):
        st.sidebar.caption(f"Tài khoản: {claims['sub']}")
    st.caption("Vai trò đọc từ mã truy cập chỉ để hiển thị; API vẫn kiểm tra chữ ký và quyền ở mọi thao tác.")
else:
    role = st.sidebar.selectbox(
        "Vai trò hiển thị", ["Sinh viên", "Giảng viên", "Quản trị"],
        help="Không đọc được vai trò từ mã truy cập. Lựa chọn này chỉ đổi giao diện; API vẫn kiểm tra quyền thật.")
    st.caption("Vai trò thật được xác thực từ JWT phía API; lựa chọn này chỉ đổi giao diện.")

if role == "Giảng viên":
    st.header("Giảng viên")
    with st.form("automl"):
        st.subheader("Huấn luyện mô hình tự động (AutoML)")
        train_course = st.text_input("Mã khoá học", help="Bộ dữ liệu phải thuộc khoá học này; API kiểm tra bạn có dạy khoá đó.")
        dataset = st.text_input("Mã bộ dữ liệu", help="Tham chiếu bộ dữ liệu trong khoá học (dataset_ref).")
        target = st.text_input("Cột mục tiêu", help="Tên cột cần dự đoán, ví dụ: dat_mon.")
        metric = st.selectbox("Thước đo đánh giá", ["f1", "roc_auc", "accuracy"],
                              help="f1: cân bằng chính xác và độ phủ; roc_auc: khả năng xếp hạng; accuracy: tỉ lệ đúng.")
        train = st.form_submit_button("Xếp hàng AutoML")
    if train and not missing(**{"Mã khoá học": train_course, "Mã bộ dữ liệu": dataset, "Cột mục tiêu": target}):
        submit_task("/api/instructor/automl", {"course_id": train_course.strip(), "dataset_ref": dataset.strip(),
                                               "target": target.strip(), "metric": metric}, token)
    with st.form("cms"):
        st.subheader("Chuẩn bị giải thích bài giảng")
        course = st.text_input("Mã khoá học", key="cms_course", help="Khoá học chứa bài giảng.")
        lecture = st.text_input("Mã bài giảng", help="Mã bài giảng trong khoá học.")
        content = st.text_area("Nội dung bài giảng", help="Văn bản bài giảng dùng để tạo giải thích trước cho sinh viên.")
        precompute = st.form_submit_button("Tạo giải thích trước")
    if precompute and not missing(**{"Mã khoá học": course, "Mã bài giảng": lecture, "Nội dung bài giảng": content}):
        submit_task("/api/instructor/cms/precompute", {"course_id": course.strip(), "lecture_id": lecture.strip(),
                                                       "content": content}, token)
    st.subheader("Bảng xếp hạng mô hình")
    model_ref = st.text_input("Mã mô hình", help="Mã mô hình nhận được sau khi AutoML chạy xong.")
    if st.button("Tải bảng xếp hạng") and not missing(**{"Mã mô hình": model_ref}):
        r = call("GET", f"/api/instructor/leaderboard/{seg(model_ref)}", token)
        if r is not None and r.is_success:
            body = r.json()
            rows = body.get("rows", [])
            if rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("Bảng xếp hạng chưa có dòng nào.")
            show_raw(body)
        elif r is not None:
            show_error(r)

elif role == "Sinh viên":
    st.header("Sinh viên")
    with st.form("explanation"):
        st.subheader("Đọc giải thích bài giảng")
        course = st.text_input("Mã khoá học", help="Khoá học bạn đang theo học.")
        lecture = st.text_input("Mã bài giảng", help="Mã bài giảng cần xem giải thích.")
        read = st.form_submit_button("Đọc giải thích")
    if read and not missing(**{"Mã khoá học": course, "Mã bài giảng": lecture}):
        r = call("GET", f"/api/student/explanations/{seg(course)}/{seg(lecture)}", token)
        if r is not None and r.is_success:
            body = r.json()
            show_structured(body.get("explanations"))
            show_raw(body)
        elif r is not None:
            show_error(r)
    with st.form("advice"):
        st.subheader("Tư vấn lộ trình học")
        student_ref = st.text_input("Mã sinh viên ẩn danh", value=str((claims or {}).get("sub", "")),
                                    help="Phải trùng với tài khoản đăng nhập; API từ chối nếu khác.")
        goal = st.text_area("Mục tiêu học tập", help="Ví dụ: cải thiện điểm môn Toán rời rạc trong học kỳ này.")
        gpa = st.number_input("Điểm trung bình (GPA, thang 4)", min_value=0.0, max_value=4.0, value=2.5,
                              help="Điểm trung bình tích luỹ hiện tại, từ 0 đến 4.")
        submit = st.form_submit_button("Tư vấn lộ trình")
    if submit and not missing(**{"Mã sinh viên ẩn danh": student_ref, "Mục tiêu học tập": goal}):
        submit_task("/api/student/advice", {"student_ref": student_ref.strip(), "features": {"gpa": gpa}, "goal": goal}, token)

    with st.form("grade_baseline"):
        st.subheader("Dự đoán điểm (mô hình cơ sở)")
        st.caption("Mô hình cơ sở OLS ở chế độ demo dùng dữ liệu tổng hợp và GPA mẫu cố định 2,5; chế độ thật lấy dữ liệu qua adapter.")
        grade_course = st.text_input("Mã khoá học cần dự đoán", help="Khoá học bạn đang theo học.")
        predict_grade = st.form_submit_button("Dự đoán điểm")
    if predict_grade and not missing(**{"Mã khoá học cần dự đoán": grade_course}):
        response = call("POST", "/api/student/grade-baseline", token, json={
            "course_id": grade_course.strip(),
        })
        if response is not None and response.is_success:
            prediction = response.json()
            if prediction.get("mode") == "demo_fixture_only":
                st.warning("Kết quả minh hoạ trên dữ liệu tổng hợp, không dùng cho quyết định học vụ.")
            holdout = prediction.get("synthetic_holdout_metrics") or {}
            c1, c2, c3 = st.columns(3)
            c1.metric("Điểm dự đoán (thang 4)", prediction.get("predicted_grade", "—"))
            c2.metric("Sai số tuyệt đối TB (MAE)", holdout.get("mae", "—"), help="Trên tập kiểm tra tổng hợp nhỏ.")
            c3.metric("Căn sai số bình phương TB (RMSE)", holdout.get("rmse", "—"))
            if prediction.get("limitations"):
                st.caption(f"Giới hạn: {prediction['limitations']}")
            show_raw(prediction)
        elif response is not None:
            show_error(response)

else:
    st.header("Quản trị")
    if st.button("Xem hàng đợi và đồ thị tri thức", help="Số tác vụ đang chạy/chờ và tình trạng đồ thị tri thức."):
        q = call("GET", "/api/admin/queue", token)
        g = call("GET", "/api/admin/knowledge-graph", token)
        if q is not None and q.is_success:
            qd = q.json()
            st.subheader("Hàng đợi tác vụ")
            c1, c2, c3 = st.columns(3)
            c1.metric("Đang chạy", qd.get("active", "—"))
            c2.metric("Đã nhận, chờ chạy", qd.get("reserved", "—"))
            c3.metric("Số worker", len(qd.get("workers") or []))
            if qd.get("workers"):
                st.table([{"Worker": w} for w in qd["workers"]])
            show_raw(qd)
        elif q is not None:
            show_error(q)
        if g is not None and g.is_success:
            gd = g.json()
            st.subheader("Đồ thị tri thức")
            c1, c2, c3 = st.columns(3)
            c1.metric("Trạng thái", gd.get("status", "—"))
            c2.metric("Số nút", gd.get("nodes") if gd.get("nodes") is not None else "—")
            c3.metric("Số cạnh", gd.get("edges") if gd.get("edges") is not None else "—")
            show_raw(gd)
        elif g is not None:
            show_error(g)


TASK_STATE = {"PENDING": "Đang chờ", "STARTED": "Đang chạy", "PROGRESS": "Đang chạy", "RETRY": "Đang thử lại",
              "SUCCESS": "Hoàn tất", "FAILURE": "Thất bại", "REVOKED": "Đã huỷ"}


@st.fragment(run_every=2)
def poll_task():
    task_id = st.session_state.get("task_id")
    if not task_id or not st.session_state.get("jwt"):
        return
    try:
        response = api("GET", f"/api/tasks/{seg(task_id)}", st.session_state.jwt,
                       _request_timeout=2.0, _wait_timeout=3.0)
        if response.is_success:
            item = response.json()
            stage = (item.get("progress") or {}).get("stage", "")
            st.write(f"Tác vụ {task_id}: {TASK_STATE.get(item['state'], item['state'])}{' · ' + stage if stage else ''}")
            if (item["state"] == "PENDING"
                    and st.session_state.get("submission_uncertain_task_id") == task_id):
                st.warning("PENDING chưa phân biệt tác vụ đang chờ với tác vụ broker chưa xác nhận; hãy đối soát trước khi gửi lại.")
            elif st.session_state.get("submission_uncertain_task_id") == task_id:
                st.session_state.pop("submission_uncertain_task_id", None)
            if item["state"] in {"SUCCESS", "FAILURE", "REVOKED"}:
                show_structured(item.get("result"))
                show_raw(item.get("result"))
        else:
            st.warning("Không thể đọc trạng thái tác vụ: " + friendly_error(response.status_code))
    except (httpx.HTTPError, TimeoutError):
        st.warning("API tạm thời không phản hồi; thử lại sau.")


poll_task()
