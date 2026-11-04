"""Isolated bounded voice worker. Cloud failure never changes the chosen voice."""
import json
from pathlib import Path
import sys


def main():
 request=json.loads(sys.stdin.buffer.read(6000))
 root=Path(request['root'])
 if (root/'cloud-voice.json').exists():
  from voice_cloud import synthesize
  print(json.dumps(synthesize(root,request['text'],profile=request.get('profile'))))
 else:
  from voice_kokoro import main as local_main
  local_main(request)


if __name__=='__main__':
 try:main()
 except Exception as exc:
  sys.stderr.write('voice_worker_failed:'+type(exc).__name__+'\n')
  raise SystemExit(1)
