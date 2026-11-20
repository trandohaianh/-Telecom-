"""Physical audio contracts and API domain separation; generated tones are fixtures, not accuracy evidence."""
import io
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from voice_pipeline import AudioValidationError, analyze_wav, decode_wav


def wav_bytes(samples, rate=16000, width=2):
    data = np.asarray(samples)
    if data.ndim == 1:
        data = data[:, None]
    if width == 3:
        values = np.rint(np.clip(data, -1, 1 - 2 ** -23) * 2 ** 23).astype(np.int32).ravel()
        raw = np.column_stack([values & 255, (values >> 8) & 255, (values >> 16) & 255]).astype("uint8").tobytes()
    else:
        raw = np.rint(np.clip(data, -1, 32767 / 32768) * 32768).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(data.shape[1])
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(raw)
    return out.getvalue()


def tone(seconds=1.0, rate=16000, frequency=200.0):
    t = np.arange(round(seconds * rate)) / rate
    return .15 * np.sin(2 * np.pi * frequency * t)


def test_silence_is_observation_without_fake_pitch_or_party_attribution():
    observation = analyze_wav(wav_bytes(np.zeros(16000)))
    assert observation["features"]["speech_s"] == 0
    assert observation["features"]["pitch_hz_median"] is None
    assert observation["caller_speech_ratio"] is None
    assert "near_silence" in observation["quality_warnings"]
    assert observation["transcript"] is None
    assert "vocoder_artifact" not in observation["features"]


def test_pitch_measurement_and_resampling_are_consistent():
    for rate in (8000, 16000, 48000):
        observation = analyze_wav(wav_bytes(tone(rate=rate), rate))
        assert observation["sample_rate"] == 16000
        assert observation["features"]["pitch_hz_median"] == pytest.approx(200, rel=.05)
        assert observation["features"]["voice_fraction"] > .9
        assert observation["duration_s"] == pytest.approx(1.0)


def test_stereo_roles_require_explicit_mapping():
    payload = wav_bytes(np.column_stack([np.zeros(16000), tone()]))
    unknown = analyze_wav(payload)
    observation = analyze_wav(payload, caller_channel=0)
    assert unknown["caller_speech_ratio"] is None
    assert observation["caller_speech_ratio"] == 0
    assert observation["callee_speech_s"] > .9
    assert "party_attribution_unavailable" not in observation["quality_warnings"]


def test_pcm24_roundtrip_keeps_sign_and_scale():
    source = np.array([-.9, -.1, 0, .1, .9] * 320)
    actual, rate = decode_wav(wav_bytes(source, width=3))
    assert rate == 16000
    np.testing.assert_allclose(actual[:, 0], source, atol=2 ** -22)


def test_malformed_oversized_or_truncated_audio_is_rejected():
    with pytest.raises(AudioValidationError):
        decode_wav(b"not a wav")
    payload = wav_bytes(tone())
    with pytest.raises(AudioValidationError, match="truncated"):
        decode_wav(payload[:-20])
    with pytest.raises(AudioValidationError, match="duration"):
        decode_wav(wav_bytes(np.zeros(160)))
    with pytest.raises(AudioValidationError):
        analyze_wav(payload, caller_channel=1)
    with pytest.raises(AudioValidationError):
        decode_wav(b"x" * (12 * 1024 * 1024 + 1))


def test_local_asr_adapter_contract_does_not_store_audio():
    class ASR:
        def transcribe(self, samples, sample_rate):
            assert sample_rate == 16000 and samples.ndim == 1
            return "xin chao"
    observation = analyze_wav(wav_bytes(tone()), asr=ASR())
    assert observation["transcript"] == "xin chao"
    assert observation["transcript_source"] == "local_asr"
    assert "samples" not in observation and "wav" not in observation
