"""Stateless Streamlit view. All work and role checks live behind FastAPI."""
from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import Future

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


def require_token():
    token = st.session_state.get("jwt", "")
    if not token:
        st.info("Dán access token JWT do hệ thống đăng nhập/IdP cấp.")
        st.session_state.jwt = st.text_input("Access token", type="password")
        st.stop()
    return token


def submit_task(path: str, payload: dict, token: str):
    response = api("POST", path, token, json=payload)
    if response.is_error:
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        detail = error_body.get("detail", {})
        if response.status_code == 503 and isinstance(detail, dict) and detail.get("task_id"):
            task_id = detail["task_id"]
            st.session_state.task_id = task_id
            st.session_state.submission_uncertain_task_id = task_id
            st.warning(f"Chưa xác định được broker đã nhận tác vụ hay chưa. Đang theo dõi {task_id}; đừng gửi lại trước khi đối soát.")
            return
        st.error(f"API {response.status_code}: {response.text[:500]}")
        return
    task_id = response.json()["task_id"]
    st.session_state.task_id = task_id
    st.session_state.pop("submission_uncertain_task_id", None)
    st.success(f"Đã xếp hàng: {task_id}")


st.set_page_config(page_title="Edu DSS", layout="wide")
st.title("Edu DSS · Learning services")
token = require_token()
role = st.sidebar.selectbox("Vai trò hiển thị", ["Student", "Instructor", "Admin"])
st.caption("Vai trò thật được xác thực từ JWT phía API; lựa chọn này chỉ đổi giao diện.")

if role == "Instructor":
    st.header("Giảng viên")
    with st.form("automl"):
        train_course = st.text_input("Course ID (dataset thuộc khoá học này)")
        dataset = st.text_input("Dataset reference")
        target = st.text_input("Target")
        metric = st.selectbox("Metric", ["f1", "roc_auc", "accuracy"])
        train = st.form_submit_button("Xếp hàng AutoML")
    if train:
        submit_task("/api/instructor/automl", {"course_id": train_course, "dataset_ref": dataset, "target": target, "metric": metric}, token)
    with st.form("cms"):
        course = st.text_input("Course ID")
        lecture = st.text_input("Lecture ID")
        content = st.text_area("Nội dung bài giảng")
        precompute = st.form_submit_button("Tạo giải thích trước")
    if precompute:
        submit_task("/api/instructor/cms/precompute", {"course_id": course, "lecture_id": lecture, "content": content}, token)
    model_ref = st.text_input("Model ref để xem leaderboard")
    if st.button("Tải leaderboard"):
        r = api("GET", f"/api/instructor/leaderboard/{model_ref}", token)
        st.dataframe(r.json().get("rows", [])) if r.is_success else st.error(r.text)

elif role == "Student":
    st.header("Sinh viên")
    with st.form("explanation"):
        course = st.text_input("Course ID")
        lecture = st.text_input("Lecture ID")
        read = st.form_submit_button("Đọc giải thích")
    if read:
        r = api("GET", f"/api/student/explanations/{course}/{lecture}", token)
        st.json(r.json()) if r.is_success else st.warning(r.text)
    with st.form("advice"):
        student_ref = st.text_input("Pseudonymous student ref")
        goal = st.text_area("Mục tiêu học tập")
        gpa = st.number_input("GPA", min_value=0.0, max_value=4.0, value=2.5)
        submit = st.form_submit_button("Tư vấn lộ trình")
    if submit:
        submit_task("/api/student/advice", {"student_ref": student_ref, "features": {"gpa": gpa}, "goal": goal}, token)

    with st.form("grade_baseline"):
        st.caption("Baseline OLS demo dùng fixture tổng hợp và GPA mẫu cố định 2.5; chế độ thật lấy dữ liệu qua adapter.")
        grade_course = st.text_input("Course ID cho dự đoán baseline")
        predict_grade = st.form_submit_button("Dự đoán điểm baseline")
    if predict_grade:
        response = api("POST", "/api/student/grade-baseline", token, json={
            "course_id": grade_course,
        })
        if response.is_success:
            prediction = response.json()
            if prediction.get("mode") == "demo_fixture_only":
                st.warning("Kết quả minh họa trên dữ liệu tổng hợp, không dùng cho quyết định học vụ.")
            st.json(prediction)
        else:
            st.error(f"API {response.status_code}: {response.text[:500]}")

else:
    st.header("Quản trị")
    if st.button("Trạng thái queue / knowledge graph"):
        q, g = api("GET", "/api/admin/queue", token), api("GET", "/api/admin/knowledge-graph", token)
        st.json({"queue": q.json() if q.is_success else q.text, "graph": g.json() if g.is_success else g.text})


@st.fragment(run_every=2)
def poll_task():
    task_id = st.session_state.get("task_id")
    if not task_id or not st.session_state.get("jwt"):
        return
    try:
        response = api("GET", f"/api/tasks/{task_id}", st.session_state.jwt,
                       _request_timeout=2.0, _wait_timeout=3.0)
        if response.is_success:
            item = response.json()
            st.write(f"Tác vụ {task_id}: {item['state']} · {item['progress'].get('stage', '')}")
            if (item["state"] == "PENDING"
                    and st.session_state.get("submission_uncertain_task_id") == task_id):
                st.warning("PENDING chưa phân biệt tác vụ đang chờ với tác vụ broker chưa xác nhận; hãy đối soát trước khi gửi lại.")
            elif st.session_state.get("submission_uncertain_task_id") == task_id:
                st.session_state.pop("submission_uncertain_task_id", None)
            if item["state"] in {"SUCCESS", "FAILURE", "REVOKED"}:
                st.json(item.get("result"))
        else:
            st.warning("Không thể đọc trạng thái tác vụ.")
    except (httpx.HTTPError, TimeoutError):
        st.warning("API tạm thời không phản hồi; thử lại sau.")


poll_task()
