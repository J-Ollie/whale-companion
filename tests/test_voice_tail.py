"""Actual ffmpeg decoding, sample conservation and strict failure behavior."""
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, os.environ.get('COMPANION_TEST_PLUGINS', str(Path(__file__).parent)))
from astrbot_plugin_companion_chat.breeze.audio import prepare_qq_audio, TAIL_MS, SAMPLE_RATE
from astrbot_plugin_companion_chat.breeze.client import BreezeClient
from astrbot_plugin_companion_breeze.client import BreezeClient as ExternalClient


class AudioTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp3 = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'sine=frequency=983:sample_rate=24000:duration=1.133', '-c:a', 'libmp3lame',
            '-f', 'mp3', 'pipe:1'], capture_output=True, check=True).stdout

    def test_preserves_every_decoded_sample_and_adds_exact_silence(self):
        pcm = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'mp3', '-i', 'pipe:0',
            '-ac', '1', '-ar', '24000', '-f', 's16le', 'pipe:1'],
            input=self.mp3, capture_output=True, check=True).stdout
        with wave.open(io.BytesIO(prepare_qq_audio(self.mp3))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, SAMPLE_RATE))
            result = wav.readframes(wav.getnframes())
            self.assertEqual(result, pcm + b'\0\0' * (SAMPLE_RATE * TAIL_MS // 1000))
            self.assertEqual(wav.getnframes() * 2, len(result))
            # QQ's nearest-second duration must still cover the final source sample.
            self.assertGreaterEqual(round(wav.getnframes() / SAMPLE_RATE), len(pcm) / (2 * SAMPLE_RATE))

    def test_bad_mp3_is_rejected_instead_of_sent(self):
        with self.assertRaises(ValueError): prepare_qq_audio(b'ID3' + b'x' * 200)

    def test_decoder_missing_or_timed_out_never_returns_original(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('ffmpeg', 15)):
            with patch('subprocess.run', side_effect=error):
                with self.assertRaises(RuntimeError): prepare_qq_audio(self.mp3)

    def test_partial_decode_or_overlong_output_is_rejected(self):
        for result in (SimpleNamespace(returncode=1, stdout=b'\0\0' * 24),
                       SimpleNamespace(returncode=0, stdout=b''),
                       SimpleNamespace(returncode=0, stdout=b'x'),
                       SimpleNamespace(returncode=0, stdout=b'\0\0' * (24000 * 120 + 1))):
            with patch('subprocess.run', return_value=result):
                with self.assertRaises(ValueError): prepare_qq_audio(self.mp3)

    def check_client(self, client_cls):
        with tempfile.TemporaryDirectory() as folder:
            client = client_cls(folder)
            ctx = MagicMock(); ctx.__enter__.return_value = SimpleNamespace(status=200, read=lambda _: self.mp3)
            opener = SimpleNamespace(open=Mock(return_value=ctx))
            with patch.object(client, 'key', return_value='synthetic'), patch('urllib.request.build_opener', return_value=opener):
                audio = client.synthesize('晚安，明天见。', '自然温暖，句尾完整落下。')
            self.assertTrue(audio.startswith(b'RIFF'))
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(json.loads((Path(folder) / 'state.json').read_text())['tts'], 1)
            body = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(body['text'], '晚安，明天见。')
            self.assertEqual(body['instructions'], '自然温暖，句尾完整落下。')

    def test_both_clients_decode_once_without_additional_paid_call(self):
        for client_cls in (BreezeClient, ExternalClient):
            with self.subTest(client=client_cls): self.check_client(client_cls)


if __name__ == '__main__': unittest.main()
