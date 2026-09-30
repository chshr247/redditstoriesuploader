"""Check Whisper's decoder against the installed PyAV without a model download."""

import io
import wave

from faster_whisper.audio import decode_audio


def test_decode_audio():
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    audio.seek(0)
    samples = decode_audio(audio)
    assert samples.shape == (16000,), samples.shape


if __name__ == "__main__":
    test_decode_audio()
    print("Whisper audio decoding OK")
