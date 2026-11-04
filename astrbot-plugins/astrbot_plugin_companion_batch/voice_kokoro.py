"""One cold CPU synthesis process. Fixed model/voice, no network or audio retention."""
import base64
import io
import json
import os
from pathlib import Path
import resource
import sys
import time
import wave


def main(request=None):
 if request is None:request=json.loads(sys.stdin.buffer.read(6000))
 text=request['text'];root=Path(request['root'])
 if not isinstance(text,str) or not 1<=len(text)<=180:raise ValueError('text_length')
 # Avoid inference competing with the main bot for scarce physical RAM.
 available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
 if available<512000:raise RuntimeError('memory_busy')
 resource.setrlimit(resource.RLIMIT_CPU,(45,46));resource.setrlimit(resource.RLIMIT_AS,(1600*1024*1024,1600*1024*1024))
 for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):os.environ[key]='1'
 sys.path.insert(0,str(root/'deps'))
 import sherpa_onnx
 import numpy as np
 model=root/'model';started=time.monotonic()
 config=sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
  kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(model=str(model/'model.int8.onnx'),voices=str(model/'voices.bin'),
   tokens=str(model/'tokens.txt'),data_dir=str(model/'espeak-ng-data'),
   lexicon=str(model/'lexicon-us-en.txt')+','+str(model/'lexicon-zh.txt'),dict_dir=str(model/'dict')),
  num_threads=1),rule_fsts=','.join(str(model/n) for n in ('date-zh.fst','number-zh.fst','phone-zh.fst') if (model/n).exists()),max_num_sentences=1)
 if not config.validate():raise RuntimeError('voice_config_invalid')
 tts=sherpa_onnx.OfflineTts(config)
 audio=tts.generate(text,sid=3,speed=1.02)
 duration=len(audio.samples)/audio.sample_rate
 if not 0<duration<=35:raise ValueError('voice_duration')
 pcm=(np.clip(audio.samples,-1,1)*32767).astype('<i2').tobytes();out=io.BytesIO()
 with wave.open(out,'wb') as wav:wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(audio.sample_rate);wav.writeframes(pcm)
 print(json.dumps({'wav':base64.b64encode(out.getvalue()).decode(),'seconds':round(duration,2),
  'elapsed':round(time.monotonic()-started,2),'peak_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
  'voice':'kokoro-1.1-zh/zf_001/int8','speed':1.02}))

if __name__=='__main__':main()
