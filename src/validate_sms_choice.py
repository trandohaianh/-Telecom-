"""Should the SMS classifier be switched? Validate LR vs Linear SVM vs Naive Bayes beyond one split.

Step 1  McNemar test on paired decisions (same messages, two models).
Step 2  Re-generate train/validation with several seeds, retrain, recount false blocks on the
        pooled 11 x 50,000-event test windows (thresholds re-fitted on each validation window).
Step 3  Stress test: obfuscated / paraphrased / brand-new phishing lures, under three sender
        channels from easy (original route) to hardest (P2P SIM + link on a legitimate domain),
        plus legitimate SMS rewritten in teencode / emoji to check they are not blocked.

Run:  python src/validate_sms_choice.py   -> data/sms_validation.json + data/sms_validation.md
Decision rule: switch only if the alternative wins or ties in BOTH step 2 and step 3.
"""

from __future__ import annotations

import json
import random
import re
import time
import warnings
import zlib

import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import ComplementNB
from sklearn.svm import LinearSVC

from compare_models import TEST_NAMES, WINDOWS, features
from config import DATA_DIR, TARGET_BLOCK_FPR, TARGET_FLAG_FPR
from data_generator import BRAND_SENDERS
from train import sms_matrix, threshold_for_fpr

warnings.filterwarnings("ignore")
# (train seed, val seed); cặp đầu là dữ liệu gốc của project, các cặp sau là dữ liệu mới.
SEED_RUNS = [(11, 22), (301, 401), (302, 402), (303, 403), (304, 404), (305, 405)]
for _tr, _va in SEED_RUNS[1:]:
    WINDOWS[f"train_s{_tr}"] = (_tr, 48, 0, "train", 0, None)
    WINDOWS[f"val_s{_va}"] = (_va, 24, 0, "val", 12, None)
MODELS = {
    "LogReg (đang dùng)": (
        lambda: LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000),
        lambda c, X: c.predict_proba(X)[:, 1],
    ),
    "Linear SVM": (lambda: LinearSVC(C=0.5, class_weight="balanced"), lambda c, X: c.decision_function(X)),
    "Naive Bayes": (lambda: ComplementNB(alpha=0.3), lambda c, X: c.predict_proba(X)[:, 1]),
}
BASE = "LogReg (đang dùng)"
VARIANTS = 10  # random rewrites per phishing message and perturbation

# ------------------------------------------------------------------ step 3: perturbations
URL_RE = re.compile(r"https?://\S+")
SPLIT_SEPS = [".", "-", " ", "_", "\u200b"]  # ​ = zero-width space, invisible on the phone
LEET_NEW = str.maketrans({"a": "4", "t": "7", "g": "9", "b": "8", "o": "0", "i": "1", "e": "3"})
TEENCODE = {
    "tai khoan": "tk",
    "quy khach": "qk",
    "khong": "ko",
    "duoc": "dc",
    "ngay": "ngayy",
    "truy cap": "vao",
    "xac thuc": "xthuc",
    "thong bao": "tb",
    "ban": "b",
    "cua": "of",
    "nhan": "nhanh tay nhan",
}
SYNONYMS = {
    "khoa": "phong toa",
    "tam khoa": "dong bang",
    "xac thuc": "xac minh",
    "cap nhat": "bo sung",
    "het han": "qua han",
    "truy cap": "dang nhap vao",
    "nhan thuong": "lanh qua",
    "trung thuong": "duoc tang qua",
    "giao dich": "lenh chuyen tien",
    "huy lenh": "tu choi",
    "diem thuong": "uu dai tich luy",
    "ngay": "lap tuc",
    "canh sat": "co quan dieu tra",
    "khai bao": "cung cap thong tin",
    "thong bao": "luu y",
}
EMOJI = ["⚠️", "🔥", "🎁", "💳", "✅", "❗", "👉", "🏦", "‼️"]
NEW_LURES = [  # lures that exist in real Vietnamese scams but never appear in the simulator
    "Don hang cua ban khong the giao do sai dia chi. Cap nhat lai tai {url} de nhan hang trong ngay",
    "[{brand}] Ho so vay 50 trieu cua ban da duoc duyet, giai ngan ngay khi xac nhan tai {url}",
    "Tuyen CTV lam viec tai nha, thu nhap 500k-2tr/ngay, dang ky: {url}",
    "EVN: Dien nha ban se bi cat trong toi nay do no cuoc. Thanh toan gap tai {url}",
    "Thue bao cua ban se bi khoa 2 chieu do chua chuan hoa thong tin. Chuan hoa tai {url}",
    "Ban co 1 khoan phat nguoi chua nop. Tra cuu va nop phat tai {url} de tranh bi cuong che",
    "[{brand}] Nang cap SIM 5G mien phi, nhan them 100GB data. Kich hoat: {url}",
    "Chuc mung ban duoc hoan tien 2.350.000d tu don hang Shopee. Nhan tien: {url}",
    "Dau tu coin loi nhuan 30%/thang, cam ket hoan von. Tham gia nhom: {url}",
    "Con dang o benh vien can tien gap, me chuyen giup con qua link {url} nhe",
    "[{brand}] Quy khach chua xac thuc CCCD gan chip, the se bi tam ngung tu ngay mai. Xac thuc: {url}",
    "Bao hiem y te cua ban sap het han, gia han online tai {url} de tiep tuc huong quyen loi",
]
NEW_DOMAINS = [
    "giaohang-nhanh.shop",
    "vay-online24h.top",
    "ctv-tuyendung.site",
    "evn-thanhtoan247.click",
    "chuanhoa-thuebao.info",
    "phatnguoi-tracuu.xyz",
    "sim5g-kichhoat.online",
    "hoantien-don.store",
]


def _words_apply(text, rnd, fn, share=0.5):
    out = []
    for w in text.split(" "):
        out.append(fn(w) if (len(w) >= 3 and not w.startswith("http") and rnd.random() < share) else w)
    return " ".join(out)


def _phrase_sub(text, table, rnd, share=0.8):
    low = text
    for k, v in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if rnd.random() < share:
            low = re.sub(rf"\b{k}\b", v, low, flags=re.IGNORECASE)
    return low


PERTURB = {
    "Gốc (không đổi)": lambda t, r: t,
    "Tách chữ (t.a.i k-h-o-a-n, ký tự ẩn)": lambda t, r: _words_apply(
        t, r, lambda w: r.choice(SPLIT_SEPS).join(w), 0.4
    ),
    "Leetspeak mới (4, 7, 9, 8)": lambda t, r: _words_apply(t, r, lambda w: w.lower().translate(LEET_NEW), 0.5),
    "Teencode / viết tắt": lambda t, r: _phrase_sub(t, TEENCODE, r),
    "Diễn đạt lại (từ đồng nghĩa)": lambda t, r: _phrase_sub(t, SYNONYMS, r),
    "Chèn emoji + khoảng trắng": lambda t, r: " ".join(
        w + (r.choice(EMOJI) if r.random() < 0.3 else "") + (" " if r.random() < 0.2 else "") for w in t.split(" ")
    ),
    "Kết hợp (tách + leet + đồng nghĩa)": lambda t, r: PERTURB["Tách chữ (t.a.i k-h-o-a-n, ký tự ẩn)"](
        PERTURB["Leetspeak mới (4, 7, 9, 8)"](_phrase_sub(t, SYNONYMS, r), r), r
    ),
}
LEGIT_PERTURB = ["Teencode / viết tắt", "Leetspeak mới (4, 7, 9, 8)", "Chèn emoji + khoảng trắng"]

# Kênh gửi: A giữ nguyên metadata gốc; B gửi P2P từ SIM thường; C thêm link đặt trên domain hợp lệ (drive.google.com).
P2P_META = {
    "route_brand_gw": 0.0,
    "route_p2p": 1.0,
    "route_unverified_bts": 0.0,
    "sender_is_brand": 0.0,
    "brand_not_via_gateway": 0.0,
    "url_count": 1.0,
    "url_unofficial": 1.0,
    "cell_registered": 1.0,
    "sender_sms_1h": 1.0,
}
CHANNELS = {
    "A. Kênh gốc của kẻ lừa": None,
    "B. Gửi P2P từ SIM thường": P2P_META,
    "C. P2P + link trên domain hợp lệ (khó nhất)": {**P2P_META, "url_unofficial": 0.0},
}


def stress_sets(fraud: pd.DataFrame):
    """Build every (channel, perturbation) set of phishing SMS; deterministic."""
    base_rows = fraud.to_dict("records")
    rnd = random.Random(2026)
    new_rows = []
    for tpl in NEW_LURES:
        for _ in range(VARIANTS * 5):
            row = dict(rnd.choice(base_rows))
            row["text"] = tpl.format(
                brand=rnd.choice(BRAND_SENDERS), url=f"https://{rnd.choice(NEW_DOMAINS)}/{rnd.randint(10, 999)}"
            )
            new_rows.append(row)
    sets = {}
    for ch, meta in CHANNELS.items():
        for name, fn in list(PERTURB.items()) + [("Mẫu lừa hoàn toàn mới (12 kịch bản)", None)]:
            r = random.Random(zlib.crc32(f"{ch}|{name}".encode()))
            rows = []
            src = new_rows if fn is None else [dict(b) for b in base_rows for _ in range(VARIANTS)]
            for row in src:
                row = dict(row)
                if fn is not None:
                    row["text"] = fn(row["text"], r)
                if meta is not None:
                    row.update(meta)
                    if meta["url_unofficial"] == 0.0:
                        row["text"] = URL_RE.sub(
                            lambda _: f"https://drive.google.com/{r.randint(1000, 99999)}",  # noqa: B023
                            row["text"],
                        )
                rows.append(row)
            sets[(ch, name)] = pd.DataFrame(rows)
    return sets


def legit_sets(legit: pd.DataFrame):
    rows = legit.to_dict("records")
    out = {}
    for name in LEGIT_PERTURB:
        r = random.Random(zlib.crc32(name.encode()))
        out[name] = pd.DataFrame([{**x, "text": PERTURB[name](x["text"], r)} for x in rows])
    return out


# ------------------------------------------------------------------ helpers
def mcnemar(a: np.ndarray, b: np.ndarray):
    """Exact McNemar on paired boolean outcomes: counts where only A / only B is true, two-sided p."""
    only_a, only_b = int((a & ~b).sum()), int((~a & b).sum())
    p = 1.0 if only_a + only_b == 0 else float(binomtest(only_a, only_a + only_b, 0.5).pvalue)
    return {"only_base": only_a, "only_other": only_b, "p_value": round(p, 5)}


def vec():
    return TfidfVectorizer(
        analyzer="word", token_pattern=r"[a-z0-9]+", ngram_range=(1, 2), min_df=2, max_features=40000, sublinear_tf=True
    )


def one_run(tr, va, te, stress, legit_p):
    v = vec()
    Xtr = sms_matrix(tr, v, fit=True)
    Xva, Xte = sms_matrix(va, v), sms_matrix(te, v)
    Xs = {k: sms_matrix(d, v) for k, d in stress.items()}
    Xl = {k: sms_matrix(d, v) for k, d in legit_p.items()}
    yte = te.label.values
    res, dec = {}, {}
    for name, (make, score) in MODELS.items():
        c = make().fit(Xtr, tr.label.values)
        neg = score(c, Xva)[va.label.values == 0]
        th = {"block": threshold_for_fpr(neg, TARGET_BLOCK_FPR), "flag": threshold_for_fpr(neg, TARGET_FLAG_FPR)}
        s = score(c, Xte)
        blk, anyf = s >= th["block"], s >= th["flag"]
        r = {
            "legit_block": int((blk & (yte == 0)).sum()),
            "legit_flag_or_block": int((anyf & (yte == 0)).sum()),
            "fraud_block_pct": round(100 * float(blk[yte == 1].mean()), 2),
            "fraud_any_pct": round(100 * float(anyf[yte == 1].mean()), 2),
            "stress": {},
            "legit_perturbed": {},
        }
        d = {"test_block": blk, "stress_any": {}, "stress_block": {}}
        for k, X in Xs.items():
            ss = score(c, X)
            r["stress"][" | ".join(k)] = {
                "block_pct": round(100 * float((ss >= th["block"]).mean()), 2),
                "any_pct": round(100 * float((ss >= th["flag"]).mean()), 2),
            }
            d["stress_any"][k], d["stress_block"][k] = ss >= th["flag"], ss >= th["block"]
        for k, X in Xl.items():
            sl = score(c, X)
            r["legit_perturbed"][k] = {
                "block": int((sl >= th["block"]).sum()),
                "flag_or_block": int((sl >= th["flag"]).sum()),
            }
        res[name], dec[name] = r, d
    # Bước 1: McNemar giữa mô hình đang dùng và từng ứng viên, trên cùng tin nhắn.
    tests = {}
    legit_mask = yte == 0
    all_any = lambda n: np.concatenate([dec[n]["stress_any"][k] for k in Xs])
    for name in MODELS:
        if name == BASE:
            continue
        tests[name] = {
            "false_block_legit": mcnemar(dec[BASE]["test_block"][legit_mask], dec[name]["test_block"][legit_mask]),
            "stress_caught_any": mcnemar(all_any(BASE), all_any(name)),
        }
    return res, tests


# ------------------------------------------------------------------ report
def summarize(runs):
    names = list(MODELS)
    keys = list(runs[0]["models"][BASE]["stress"].keys())
    S = {}
    for n in names:
        lb = [r["models"][n]["legit_block"] for r in runs]
        S[n] = {
            "legit_block_per_run": lb,
            "legit_block_mean": round(float(np.mean(lb)), 2),
            "legit_flag_mean": round(float(np.mean([r["models"][n]["legit_flag_or_block"] for r in runs])), 1),
            "test_fraud_any_mean": round(float(np.mean([r["models"][n]["fraud_any_pct"] for r in runs])), 2),
            "stress": {
                k: {
                    "block_mean": round(float(np.mean([r["models"][n]["stress"][k]["block_pct"] for r in runs])), 2),
                    "any_mean": round(float(np.mean([r["models"][n]["stress"][k]["any_pct"] for r in runs])), 2),
                    "any_min": round(float(np.min([r["models"][n]["stress"][k]["any_pct"] for r in runs])), 2),
                }
                for k in keys
            },
            "legit_perturbed_block_mean": {
                k: round(float(np.mean([r["models"][n]["legit_perturbed"][k]["block"] for r in runs])), 2)
                for k in LEGIT_PERTURB
            },
        }
        S[n]["stress_any_overall"] = round(float(np.mean([v["any_mean"] for v in S[n]["stress"].values()])), 2)
        S[n]["stress_block_overall"] = round(float(np.mean([v["block_mean"] for v in S[n]["stress"].values()])), 2)
    verdict = {}
    for n in names:
        if n == BASE:
            continue
        w = sum(o < b for o, b in zip(S[n]["legit_block_per_run"], S[BASE]["legit_block_per_run"]))
        t = sum(o == b for o, b in zip(S[n]["legit_block_per_run"], S[BASE]["legit_block_per_run"]))
        step2 = "thắng" if w + t == len(runs) and w > 0 else ("hòa" if w + t == len(runs) else "thua ít nhất 1 lần")
        d_any = S[n]["stress_any_overall"] - S[BASE]["stress_any_overall"]
        d_blk = S[n]["stress_block_overall"] - S[BASE]["stress_block_overall"]
        step3 = "thắng" if d_any > 1 else ("hòa" if d_any >= -1 else "thua")
        # Cái giá phía người dùng thật: cờ nhầm trên tin bình thường và chặn nhầm tin thật viết teencode / leetspeak.
        flag_ratio = S[n]["legit_flag_mean"] / max(S[BASE]["legit_flag_mean"], 1.0)
        lp_other = sum(S[n]["legit_perturbed_block_mean"].values())
        lp_base = sum(S[BASE]["legit_perturbed_block_mean"].values())
        legit_cost = "thua" if (flag_ratio > 1.5 or lp_other > 2 * lp_base + 5) else "đạt"
        verdict[n] = {
            "step2_runs_better": w,
            "step2_runs_equal": t,
            "step2": step2,
            "step3_any_diff_pts": round(d_any, 2),
            "step3_block_diff_pts": round(d_blk, 2),
            "step3": step3,
            "legit_flag_ratio": round(flag_ratio, 2),
            "legit_perturbed_block": round(lp_other, 1),
            "legit_perturbed_block_base": round(lp_base, 1),
            "legit_cost": legit_cost,
            "switch_recommended": step2 in ("thắng", "hòa")
            and step3 in ("thắng", "hòa")
            and (step2 == "thắng" or step3 == "thắng")
            and legit_cost == "đạt",
        }
    return S, verdict


def markdown(S, verdict, runs, meta):
    names = list(MODELS)
    L = [
        "# Xác thực lựa chọn mô hình SMS / SMS model validation",
        "",
        f"{len(runs)} lần huấn luyện (seed train/val khác nhau) · test: {meta['test_sms']:,} SMS thật + {meta['test_fraud']} SMS lừa "
        f"(11 cửa sổ) · stress: {meta['stress_msgs']:,} tin lừa biến đổi mỗi lần · dữ liệu giả lập.",
        "",
        "## Bước 2 – Chặn nhầm tin nhắn thật qua các seed",
        "",
        "| Mô hình | "
        + " | ".join(f"seed {a}/{b}" for a, b in SEED_RUNS[: len(runs)])
        + " | TB chặn nhầm | TB cờ nhầm |",
        "|---" * (len(runs) + 3) + "|",
    ]
    for n in names:
        L.append(
            f"| {n} | "
            + " | ".join(str(x) for x in S[n]["legit_block_per_run"])
            + f" | {S[n]['legit_block_mean']} | {S[n]['legit_flag_mean']} |"
        )
    L += [
        "",
        "## Bước 1 – McNemar (so với LogReg, cùng tin nhắn)",
        "",
        "| Seed | Ứng viên | Chặn nhầm chỉ LogReg / chỉ ứng viên | p | Tin lừa (stress) chỉ LogReg bắt / chỉ ứng viên bắt | p |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        for n, t in r["mcnemar"].items():
            a, b = t["false_block_legit"], t["stress_caught_any"]
            L.append(
                f"| {r['seeds'][0]}/{r['seeds'][1]} | {n} | {a['only_base']} / {a['only_other']} | {a['p_value']} "
                f"| {b['only_base']} / {b['only_other']} | {b['p_value']} |"
            )
    L += [
        "",
        "p < 0,05: khác biệt có ý nghĩa thống kê trong lần chạy đó.",
        "",
        "## Bước 3 – Stress test: % tin lừa bị CHẶN hoặc GẮN CỜ (trung bình các seed, [thấp nhất])",
        "",
    ]
    for ch in CHANNELS:
        L += [f"### {ch}", "", "| Biến đổi | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
        for k in S[BASE]["stress"]:
            if not k.startswith(ch):
                continue
            L.append(
                f"| {k.split(' | ', 1)[1]} | "
                + " | ".join(
                    f"{S[n]['stress'][k]['any_mean']:.1f}% [{S[n]['stress'][k]['any_min']:.0f}]" for n in names
                )
                + " |"
            )
        L.append("")
    L += [
        "| Tổng hợp stress | " + " | ".join(names) + " |",
        "|---" * (len(names) + 1) + "|",
        "| TB % bắt (chặn+cờ) | " + " | ".join(f"{S[n]['stress_any_overall']}%" for n in names) + " |",
        "| TB % CHẶN | " + " | ".join(f"{S[n]['stress_block_overall']}%" for n in names) + " |",
        "",
        "### Tin nhắn THẬT bị biến đổi – số tin bị chặn nhầm (trung bình mỗi seed)",
        "",
        "| Biến đổi | " + " | ".join(names) + " |",
        "|---" * (len(names) + 1) + "|",
    ]
    for k in LEGIT_PERTURB:
        L.append(f"| {k} | " + " | ".join(str(S[n]["legit_perturbed_block_mean"][k]) for n in names) + " |")
    L += [
        "",
        "## Kết luận theo quy tắc",
        "",
        "Đổi chỉ khi ứng viên thắng/hòa ở bước 2 **và** bước 3, **và** không đẩy thêm chi phí sang người dùng thật "
        "(cờ nhầm ≤ 1,5× LogReg; chặn nhầm tin thật viết teencode/leetspeak/emoji ≤ 2× LogReg + 5).",
        "",
        "| Ứng viên | Bước 2 (chặn nhầm) | Bước 3 (chênh % bắt so với LogReg) | Chi phí cho người dùng thật | Nên đổi? |",
        "|---|---|---|---|---|",
    ]
    for n, v in verdict.items():
        L.append(
            f"| {n} | {v['step2']} (tốt hơn {v['step2_runs_better']}/{len(runs)}, bằng {v['step2_runs_equal']}) "
            f"| {v['step3']} ({v['step3_any_diff_pts']:+.2f} điểm; CHẶN {v['step3_block_diff_pts']:+.2f}) "
            f"| {v['legit_cost']} (cờ nhầm ×{v['legit_flag_ratio']}; chặn nhầm tin biến đổi "
            f"{v['legit_perturbed_block']} vs {v['legit_perturbed_block_base']}) "
            f"| {'**CÓ**' if v['switch_recommended'] else '**KHÔNG**'} |"
        )
    L += ["", "Ngưỡng: lượng tử thuần trên validation (không áp FLOORS của train.py), giống compare_models.py."]
    return "\n".join(L)


if __name__ == "__main__":
    t0 = time.time()
    print("Chuẩn bị dữ liệu test (cache) …", flush=True)
    te = pd.concat([features(n)["sms"] for n in TEST_NAMES], ignore_index=True)
    stress = stress_sets(te[te.label == 1])
    legit_p = legit_sets(te[te.label == 0].sample(20_000, random_state=7))
    runs = []
    for s_tr, s_va in SEED_RUNS:
        t = time.time()
        tr = features("train" if s_tr == 11 else f"train_s{s_tr}")["sms"]
        va = features("val" if s_va == 22 else f"val_s{s_va}")["sms"]
        res, tests = one_run(tr, va, te, stress, legit_p)
        runs.append({"seeds": [s_tr, s_va], "models": res, "mcnemar": tests})
        print(
            f"seed {s_tr}/{s_va} xong ({time.time() - t:.0f}s): "
            + ", ".join(f"{n}: chặn nhầm {r['legit_block']}" for n, r in res.items()),
            flush=True,
        )
    S, verdict = summarize(runs)
    meta = {
        "runtime_s": round(time.time() - t0, 1),
        "test_sms": int((te.label == 0).sum()),
        "test_fraud": int(te.label.sum()),
        "stress_msgs": int(sum(len(d) for d in stress.values())),
        "legit_perturbed_msgs": 20_000,
    }
    out = {"meta": meta, "summary": S, "verdict": verdict, "runs": runs}
    (DATA_DIR / "sms_validation.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    md = markdown(S, verdict, runs, meta)
    (DATA_DIR / "sms_validation.md").write_text(md, encoding="utf-8")
    print("\n" + md + f"\n\nXong sau {meta['runtime_s']} s", flush=True)
