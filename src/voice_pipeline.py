"""Bounded local WAV feature extraction. Acoustic measurements are not a deepfake verdict."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import wave

import numpy as np
from scipy.signal import resample_poly

SCHEMA_VERSION = "voice-acoustic-v2"
TARGET_RATE = 16000
MAX_WAV_BYTES = 12 * 1024 * 1024
MAX_DURATION_S = 30.0
MIN_DURATION_S = 0.10
REAL_FEATURES = [
    "voice_fraction", "speech_s", "rms_dbfs", "clipping_ratio", "zero_crossing_rate",
    "spectral_flatness", "spectral_centroid_hz", "spectral_centroid_std_hz",
    "pitch_hz_median", "pitch_period_variation", "spectral_drift",
]


class AudioValidationError(ValueError):
    pass


def _decode_pcm(payload: bytes) -> tuple[np.ndarray, int, int]:
    """Decode integer PCM only, validate the header before allocating sample arrays."""
    if not isinstance(payload, bytes) or len(payload) > MAX_WAV_BYTES:
        raise AudioValidationError("WAV exceeds the 12 MiB limit")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            rate, channels, width, frames = (
                source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getnframes()
            )
            if source.getcomptype() != "NONE" or width not in (1, 2, 3, 4):
                raise AudioValidationError("expected uncompressed integer PCM WAV (8/16/24/32 bit)")
            if channels not in (1, 2) or rate not in (8000, 16000, 24000, 32000, 44100, 48000):
                raise AudioValidationError("expected mono/stereo PCM at 8, 16, 24, 32, 44.1 or 48 kHz")
            duration = frames / rate
            if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
                raise AudioValidationError("audio duration must be between 0.1 and 30 seconds")
            size = frames * channels * width
            if size > MAX_WAV_BYTES:
                raise AudioValidationError("decoded PCM exceeds the size limit")
            raw = source.readframes(frames)
            if len(raw) != size:
                raise AudioValidationError("truncated WAV data")
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioValidationError("invalid or unsupported PCM WAV") from exc
    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        samples = values.astype(np.float64) / 8388608.0
    else:
        samples = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").astype(np.float64)
        samples /= float(1 << (width * 8 - 1))
    return samples.reshape(-1, channels), rate, width


def decode_wav(payload: bytes) -> tuple[np.ndarray, int]:
    samples, rate, _ = _decode_pcm(payload)
    return samples, rate


def _frames(samples: np.ndarray) -> np.ndarray:
    length, hop = 400, 160
    if len(samples) < length:
        samples = np.pad(samples, (0, length - len(samples)))
    count = 1 + (len(samples) - length) // hop
    return np.lib.stride_tricks.sliding_window_view(samples, length)[::hop][:count].copy()


def _segments(mask: np.ndarray, duration: float) -> list[dict]:
    spans, start = [], None
    for i, value in enumerate(np.r_[mask, False]):
        if value and start is None:
            start = i
        if not value and start is not None:
            if i - start >= 8:
                spans.append({"start_s": round(start * .01, 3),
                              "end_s": round(min(duration, (i - 1) * .01 + .025), 3)})
            start = None
    return spans


def _channel_metrics(samples: np.ndarray, *, clipping_ratio: float) -> tuple[dict, list[dict]]:
    raw_rms = float(np.sqrt(np.mean(samples ** 2)))
    centered = samples - float(np.mean(samples))
    frames = _frames(centered)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(np.maximum(frame_rms, 1e-10))
    threshold = max(-55.0, min(-35.0, float(np.percentile(db, 20)) + 8.0))
    # Transparent energy VAD baseline. It is intentionally not advertised as a neural speech detector.
    mask = db > threshold
    spans = _segments(mask, len(samples) / TARGET_RATE)
    valid_mask = np.zeros(len(mask), dtype=bool)
    for span in spans:
        start = int(round(span["start_s"] / .01))
        end = min(len(mask), int(round((span["end_s"] - .025) / .01)) + 1)
        valid_mask[start:end] = True
    active = frames[valid_mask]
    duration = len(samples) / TARGET_RATE
    speech_s = min(duration, sum(s["end_s"] - s["start_s"] for s in spans))
    result = {
        "voice_fraction": float(speech_s / duration),
        "speech_s": float(speech_s),
        "rms_dbfs": float(20 * np.log10(max(raw_rms, 1e-10))),
        "clipping_ratio": clipping_ratio,
        "zero_crossing_rate": float(np.mean(np.signbit(centered[:-1]) != np.signbit(centered[1:]))),
        "spectral_flatness": 0.0, "spectral_centroid_hz": 0.0,
        "spectral_centroid_std_hz": 0.0, "pitch_hz_median": None,
        "pitch_period_variation": None, "spectral_drift": 0.0,
    }
    if not len(active):
        return result, spans
    windowed = active * np.hanning(400)
    power = np.abs(np.fft.rfft(windowed, n=512, axis=1)) ** 2 + 1e-16
    freq = np.fft.rfftfreq(512, 1 / TARGET_RATE)
    band = (freq >= 80) & (freq <= 7500)
    band_power = power[:, band]
    centroids = np.sum(band_power * freq[band], axis=1) / np.sum(band_power, axis=1)
    result["spectral_flatness"] = float(np.mean(
        np.exp(np.mean(np.log(band_power), axis=1)) / np.mean(band_power, axis=1)))
    result["spectral_centroid_hz"] = float(np.mean(centroids))
    result["spectral_centroid_std_hz"] = float(np.std(centroids))
    norm_spectrum = band_power / np.sum(band_power, axis=1, keepdims=True)
    if len(norm_spectrum) > 1:
        midpoint = len(norm_spectrum) // 2
        a, b = norm_spectrum[:midpoint].mean(axis=0), norm_spectrum[midpoint:].mean(axis=0)
        result["spectral_drift"] = float(np.clip(
            1 - np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-16), 0, 1))
    # Frame-period variability is a proxy; it is not cycle-level clinical jitter or a speaker identity score.
    transformed = np.fft.rfft(windowed, n=1024, axis=1)
    corr = np.fft.irfft(np.abs(transformed) ** 2, n=1024, axis=1)
    lo, hi = int(TARGET_RATE / 400), int(TARGET_RATE / 60)
    lags = np.argmax(corr[:, lo:hi + 1], axis=1) + lo
    peaks = corr[np.arange(len(corr)), lags] / np.maximum(corr[:, 0], 1e-16)
    periods = lags[peaks >= .55].astype(float) / TARGET_RATE
    if len(periods):
        result["pitch_hz_median"] = float(1 / np.median(periods))
    if len(periods) > 1:
        result["pitch_period_variation"] = float(np.mean(np.abs(np.diff(periods))) / np.mean(periods))
    return result, spans


class LocalWhisperASR:
    """Optional ASR adapter: existing local faster-whisper model, no implicit model downloads."""

    def __init__(self, model_path: str | Path, language: str = "vi"):
        path = Path(model_path)
        if not path.is_dir():
            raise AudioValidationError("ASR model must be an existing local model directory")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AudioValidationError("install requirements-voice-asr.txt to enable local ASR") from exc
        self.model = WhisperModel(str(path), device="cpu", compute_type="int8", local_files_only=True)
        self.language = language

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> str:
        if sample_rate != TARGET_RATE:
            raise AudioValidationError("ASR requires 16 kHz input")
        segments, _ = self.model.transcribe(samples.astype(np.float32), language=self.language,
                                             beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


def analyze_wav(payload: bytes, *, caller_channel: int | None = None, asr=None) -> dict:
    samples, source_rate, width = _decode_pcm(payload)
    channels = samples.shape[1]
    # Count source PCM extreme codes before filtering/resampling can hide or
    # introduce peaks. Positive full scale depends on the source bit depth.
    positive_limit = 1.0 - 2.0 ** -(8 * width - 1)
    clipping = np.mean((samples <= -1.0) | (samples >= positive_limit), axis=0)
    if caller_channel is not None and (type(caller_channel) is not int or not 0 <= caller_channel < channels):
        raise AudioValidationError("caller_channel must identify an existing channel")
    if source_rate != TARGET_RATE:
        divisor = math.gcd(source_rate, TARGET_RATE)
        samples = resample_poly(samples, TARGET_RATE // divisor, source_rate // divisor, axis=0)
    # Mono has no party attribution; two channels also require an explicit caller-channel mapping.
    channel_results = [_channel_metrics(samples[:, c], clipping_ratio=float(clipping[c]))
                       for c in range(channels)]
    selected = caller_channel if caller_channel is not None else 0
    features, segments = channel_results[selected]
    warnings = []
    if channels == 1 or caller_channel is None:
        warnings.append("party_attribution_unavailable")
    if features["rms_dbfs"] < -50:
        warnings.append("near_silence")
    if features["clipping_ratio"] > .01:
        warnings.append("clipping")
    if features["speech_s"] < .2:
        warnings.append("insufficient_speech")
    transcript = asr.transcribe(samples[:, selected], TARGET_RATE) if asr else None
    attributable = channels == 2 and caller_channel is not None
    return {
        "schema_version": SCHEMA_VERSION, "source": "real_pcm_wav",
        "audio_sha256": hashlib.sha256(payload).hexdigest(),
        "source_sample_rate": source_rate, "sample_rate": TARGET_RATE,
        "channels": channels, "duration_s": len(samples) / TARGET_RATE,
        "vad": "adaptive_energy_v1", "clipping_measurement": "source_pcm_extreme_code_fraction",
        "features": features, "segments": segments,
        "caller_speech_ratio": features["voice_fraction"] if attributable else None,
        "callee_speech_s": channel_results[1 - selected][0]["speech_s"] if attributable else None,
        "transcript": transcript, "transcript_source": "local_asr" if asr else "unavailable",
        "quality_warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract real PCM WAV observations; raw audio is never saved.")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--caller-channel", type=int)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--out", type=Path, help="Optional new JSON output path")
    args = parser.parse_args()
    if args.wav.stat().st_size > MAX_WAV_BYTES:
        parser.error("WAV exceeds 12 MiB")
    asr = LocalWhisperASR(args.asr_model) if args.asr_model else None
    result = analyze_wav(args.wav.read_bytes(), caller_channel=args.caller_channel, asr=asr)
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        with args.out.open("x", encoding="utf-8") as destination:
            destination.write(content + "\n")
    else:
        print(content)


if __name__ == "__main__":
    main()
