import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from sync_memory_vault import apply_archive


def payload(files):
    manifest={'version':1,'files':{p:hashlib.sha256(v.encode()).hexdigest() for p,v in files.items()}}
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w:gz') as archive:
        for path,text in {'manifest.json':json.dumps(manifest),**files}.items():
            raw=text.encode();info=tarfile.TarInfo(path);info.size=len(raw);archive.addfile(info,io.BytesIO(raw))
    return data.getvalue()


class LocalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'vault'

    def test_roundtrip_and_managed_deletion(self):
        apply_archive(payload({'scope/fact.md':'原始资料'}),self.root)
        self.assertEqual((self.root/'scope/fact.md').read_text(),'原始资料')
        apply_archive(payload({}),self.root)
        self.assertFalse((self.root/'scope/fact.md').exists())

    def test_local_annotations_and_unmanaged_files_survive(self):
        apply_archive(payload({'scope/fact.md':'原始资料'}),self.root)
        (self.root/'scope/fact.md').write_text('手工修改')
        (self.root/'自己的笔记.md').write_text('不能覆盖')
        apply_archive(payload({'scope/fact.md':'更正后资料'}),self.root)
        self.assertEqual((self.root/'自己的笔记.md').read_text(),'不能覆盖')
        self.assertTrue(any(p.read_text()=='手工修改' for p in (self.root/'local-edits').rglob('*.md')))

    def test_tampered_remote_hash_does_not_write(self):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w:gz') as archive:
            for p,v in {'manifest.json':'{"version":1,"files":{"x.md":"wrong"}}','x.md':'原文'}.items():
                raw=v.encode();i=tarfile.TarInfo(p);i.size=len(raw);archive.addfile(i,io.BytesIO(raw))
        with self.assertRaises(ValueError):apply_archive(data.getvalue(),self.root)
        self.assertFalse((self.root/'x.md').exists())

    def test_path_traversal_absolute_windows_paths_rejected(self):
        for name in ('../outside.md','/outside.md','C:/outside.md','a\\b.md'):
            with self.subTest(name=name),self.assertRaises(ValueError):apply_archive(payload({name:'禁止'}),self.root)
        self.assertFalse((self.root.parent/'outside.md').exists())

    def test_symlink_member_rejected(self):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode='w:gz') as archive:
            i=tarfile.TarInfo('x.md');i.type=tarfile.SYMTYPE;i.linkname='/outside';archive.addfile(i)
        with self.assertRaises(ValueError):apply_archive(data.getvalue(),self.root)


if __name__=='__main__':unittest.main()
