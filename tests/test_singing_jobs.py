"""Source parsing and narrow durable worker protocol; no network or messages."""
from pathlib import Path
import base64,hashlib,io,json,os,sqlite3,struct,sys,tempfile,unittest,wave,zipfile
from contextlib import closing
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS',str(Path(__file__).parent)))
from astrbot_plugin_companion_batch.music_score import parse,preview,excerpt
from astrbot_plugin_companion_batch.singing_jobs import SingingJobs

XML=b'''<score-partwise><part-list><score-part id="P"><part-name>Voice</part-name></score-part></part-list><part id="P"><measure><attributes><divisions>4</divisions></attributes><direction><sound tempo="100"/></direction><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note><note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration></note></measure></part></score-partwise>'''

class ScoreTests(unittest.TestCase):
 def test_real_download_link_is_found_outside_article_without_injecting_sidebar_text(self):
  from astrbot_plugin_companion_phone.reading import extract
  body=('<aside>不应进入正文<a href="/score.mxl">MusicXML</a></aside><main>'+('公开乐谱介绍。'*40)+'</main>').encode()
  page=extract('https://example.org/song','text/html',body)
  self.assertEqual(page['links'][0],'https://example.org/score.mxl');self.assertNotIn('不应进入正文',page['text'])
 def test_musicxml_actual_pitch_and_tempo_are_preserved(self):
  tracks=parse(XML);self.assertEqual([n['pitch'] for n in tracks[0]['notes']],[60,62]);self.assertAlmostEqual(tracks[0]['notes'][1]['start'],.6)
  self.assertTrue(preview(tracks)[0]['monophonic'])
 def test_mxl_reads_in_memory_without_extracting_paths(self):
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w') as z:z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>');z.writestr('score.xml',XML)
  self.assertEqual(len(parse(stream.getvalue())[0]['notes']),2)
 def test_entity_bombs_and_random_files_do_not_parse(self):
  for raw in [b'<!ENTITY x "evil">'+XML,b'not a score'*4,b'x'*(1024*1024+1)]:
   with self.assertRaises(ValueError):parse(raw)
 def test_polyphonic_part_requires_explicit_monophonic_melody(self):
  tracks=[{'track':0,'monophonic':False,'notes':[{'start':0,'duration':1,'pitch':60}]}]
  with self.assertRaises(ValueError):excerpt(tracks,'微风',0,0)
 def test_excerpt_cannot_exceed_notes_or_duration_or_forge_words(self):
  tracks=parse(XML)
  for text in ['abc','微风轻轻']:
   with self.assertRaises(ValueError):excerpt(tracks,text)
 def test_ambiguous_tracks_are_not_silently_chosen(self):
  tracks=parse(XML);tracks.append({**tracks[0],'track':1})
  with self.assertRaisesRegex(ValueError,'choose_one'):excerpt(tracks,'微风')
  self.assertEqual(excerpt(tracks,'微风',0)['track'],0)
 def test_midi_note_and_tempo_parsing(self):
  data=b'\x00\xff\x51\x03\x07\xa1\x20\x00\x90\x3c\x50\x60\x80\x3c\x00\x00\x90\x3e\x50\x60\x80\x3e\x00\x00\xff\x2f\x00'
  raw=b'MThd'+struct.pack('>IHHH',6,0,1,96)+b'MTrk'+struct.pack('>I',len(data))+data
  notes=parse(raw)[0]['notes'];self.assertEqual([n['pitch'] for n in notes],[60,62]);self.assertEqual(notes[1]['start'],.5)

class JobTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.jobs=SingingJobs(self.root);self.payload={'title':'test','lyrics':'微风','source':{'url':'https://example.org/score'},'score':{'notes':[]}}
 def submit(self):return self.jobs.submit(self.payload,'same-original-event')
 def audio(self):
  stream=io.BytesIO()
  with wave.open(stream,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*72000)
  return stream.getvalue()
 def test_repeated_original_request_is_idempotent_and_survives_reload(self):
  one=self.submit();self.assertEqual(self.submit(),one);self.assertEqual(SingingJobs(self.root).find(self.payload,'same-original-event'),one)
 def test_sleep_gate_leaves_queued_job_unclaimed(self):
  identifier=self.submit();self.assertIsNone(self.jobs.claim(False)['job']);self.assertEqual(self.jobs.get(identifier)['status'],'queued')
 def test_lease_claim_is_atomic_and_not_reissued(self):
  identifier=self.submit();job=self.jobs.claim(True)['job'];self.assertEqual(job['reference'],identifier);self.assertIsNone(SingingJobs(self.root).claim(True)['job'])
 def test_wrong_lease_cannot_write_any_audio(self):
  identifier=self.submit();self.jobs.claim(True)
  with self.assertRaises(ValueError):self.jobs.complete(identifier,'a'*32,{'ok':False})
  self.assertFalse((self.root/'local-songs').exists())
 def test_result_receipt_is_verified_and_safe_to_resend_after_network_loss(self):
  identifier=self.submit();job=self.jobs.claim(True)['job'];raw=self.audio();result={'ok':True,'wav':base64.b64encode(raw).decode(),'metrics':{'finite':True,'clipped_fraction':0,'total_notes':2,'notes_within_semitone':2}}
  self.jobs.complete(identifier,job['lease'],result);self.assertTrue(self.jobs.complete(identifier,job['lease'],result)['already_completed'])
  row=self.jobs.get(identifier);self.assertEqual(row['result']['sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(row['status'],'complete')
 def test_quality_failure_does_not_masquerade_as_ready(self):
  identifier=self.submit();job=self.jobs.claim(True)['job'];self.jobs.complete(identifier,job['lease'],{'ok':False,'reason':'quality_pitch_failed'})
  self.assertEqual(self.jobs.get(identifier)['status'],'quality_rejected')
 def test_worker_online_state_is_metadata_not_a_role_wakeup(self):
  self.jobs.heartbeat(True);self.assertTrue(self.jobs.worker_status()['online']);self.assertFalse((self.root/'rhythm.json').exists())
 def test_unknown_schema_is_not_overwritten(self):
  with closing(sqlite3.connect(self.jobs.path)) as db,db:db.execute('CREATE TABLE other_data(x)')
  with self.assertRaises(ValueError):self.submit()
 def test_portable_backup_includes_queue_without_worker_keys(self):
  from astrbot_plugin_companion_memory.portable import selected
  data=self.root/'data';voice=data/'plugin_data/companion_voice';voice.mkdir(parents=True);SingingJobs(voice).heartbeat(True);(voice/'worker.key').write_text('private')
  names=[p.name for p in selected(data)];self.assertIn('singing-jobs.sqlite3',names);self.assertNotIn('worker.key',names)

if __name__=='__main__':unittest.main()
