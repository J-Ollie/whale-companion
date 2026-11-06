"""Decode fully and protect the end of speech before QQ's SILK conversion."""
import io
import subprocess
import wave

SAMPLE_RATE = 24000
TAIL_MS = 600
MAX_SECONDS = 120
MAX_PCM = SAMPLE_RATE * 2 * MAX_SECONDS


def prepare_qq_audio(mp3):
    # Both reactive and proactive speech share this client boundary. Never ask
    # TTS to say filler, fade out the last phoneme, or re-encode it as MP3.
    # 600 ms also covers the bridge's nearest-second duration rounding (500 ms).
    try:
        decoded = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
             '-err_detect', 'explode', '-f', 'mp3', '-i', 'pipe:0',
             '-map', '0:a:0', '-t', str(MAX_SECONDS + .1),
             '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le',
             '-f', 's16le', 'pipe:1'],
            input=mp3, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('voice_audio_decode_unavailable') from None
    pcm = decoded.stdout
    if decoded.returncode or not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM:
        raise ValueError('voice_audio_decode_invalid')
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
        wav.writeframes(b'\0\0' * (SAMPLE_RATE * TAIL_MS // 1000))
    return output.getvalue()
