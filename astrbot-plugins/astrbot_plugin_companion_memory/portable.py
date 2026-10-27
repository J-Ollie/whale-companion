"""Private memory archives: verified snapshots and restore to a NEW directory only.

Usage (no AstrBot imports required):
  python portable.py backup --data-root /AstrBot/data --output /private/backups
  python portable.py verify ARCHIVE.zip
  python portable.py restore ARCHIVE.zip --destination /new/empty-name
The output contains private conversations. Never serve it as a public download.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

FORMAT='whale-memory-archive-v1'
NATIVE_TABLES={'personas','persona_folders','conversations','platform_message_history',
               'attachments','platform_sessions','umo_aliases'}
PHONE_FILES={'state.json','autonomy.json','rhythm.json','activity.sqlite3','experiences.jsonl',
             'public-diary.jsonl','private-diary.jsonl','diary-schedule.json','life-diary.jsonl',
             'qzone-state.json','qzone-publications.jsonl','discussion-state.json','outbound-state.json'}


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def snapshot(source,target):
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        with closing(sqlite3.connect(target)) as copied:
            db.backup(copied)
            if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('SQLite snapshot failed integrity check')


def project_native(source,target,scratch):
    # Snapshot first, then export selected tables. Auth tables never enter the ZIP.
    snapshot(source,scratch)
    with closing(sqlite3.connect(scratch)) as db, closing(sqlite3.connect(target)) as out:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in sorted(NATIVE_TABLES & names):
            sql=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()[0]
            out.execute(sql)
            cursor=db.execute('SELECT * FROM "'+name+'"')
            placeholders=','.join('?' for _ in cursor.description)
            while True:
                rows=cursor.fetchmany(200)
                if not rows:break
                out.executemany('INSERT INTO "'+name+'" VALUES ('+placeholders+')',rows)
            for index in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(name,)):
                out.execute(index[0])
        out.commit()
        if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Native projection invalid')
    scratch.unlink()


def selected(data):
    plugin=data/'plugin_data'
    for name in ('companion_memory','companion_friends','companion_phone','companion_active',
                 'companion_visitors','companion_rations'):
        folder=plugin/name
        if not folder.exists():continue
        for path in sorted(folder.rglob('*')):
            relative=path.relative_to(folder)
            if path.is_symlink():raise ValueError('Symlink in private data is not supported')
            if not path.is_file():continue
            if any(p in ('backups','portable-backups','__pycache__') for p in relative.parts):continue
            if path.name.endswith(('-wal','-shm','-journal','.tmp','.log')):continue
            if name=='companion_memory' and (len(relative.parts)!=1 or path.name not in ('memory.sqlite3','public-profile.json')):continue
            if name=='companion_friends' and relative.parts[0] not in ('chats.sqlite3','sleep-inbox.sqlite3','requests.json','media'):continue
            if name=='companion_phone' and (len(relative.parts)!=1 or path.name not in PHONE_FILES):continue
            if name=='companion_active' and path.name!='state.json':continue
            if name=='companion_visitors' and path.name!='feed.json':continue
            if name=='companion_rations' and path.name!='summary.json':continue
            yield path


def copy_stable(source,target):
    for _ in range(3):
        before=source.stat()
        shutil.copyfile(source,target)
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns):return
    raise ValueError('A private file kept changing; retry backup later')


def native_attachments(data):
    """Only registered files inside known attachment folders, never arbitrary paths."""
    native=data/'data_v4.db'
    if not native.exists():return [],0
    found=[];missing=0
    with closing(sqlite3.connect(native.as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone():return [],0
        for row in db.execute('SELECT path FROM attachments'):
            value=str(row[0] or '').replace('\\','/')
            if value.startswith('/AstrBot/data/'):source=data/value[len('/AstrBot/data/'):]
            else:
                path=Path(value);source=path if path.is_absolute() else data/path
            resolved=source.resolve()
            if (resolved.is_relative_to(data) and resolved.relative_to(data).parts
                    and resolved.relative_to(data).parts[0] in ('attachments','temp','chatui_uploads','uploads')
                    and source.is_file() and not source.is_symlink()):found.append(source)
            else:missing+=1
    return found,missing


def create_archive(data_root,output):
    data=Path(data_root).resolve();output=Path(output).resolve()
    if not (data/'plugin_data/companion_memory/memory.sqlite3').is_file():
        raise ValueError('Memory database is required; wrong data root')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    output.chmod(0o700)
    name='memory-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.zip'
    final=output/name;partial=output/(name+'.tmp')
    started=time.time()
    try:
        with tempfile.TemporaryDirectory(prefix='.memory-snapshot-',dir=output) as directory:
            work=Path(directory);work.chmod(0o700);payload=work/'payload';payload.mkdir()
            manifest=[]
            paths=list(selected(data))
            attachments,missing=native_attachments(data)
            paths=list(dict.fromkeys(paths+attachments))
            native=data/'data_v4.db'
            if native.exists():paths.append(native)
            for source in paths:
                if source.is_symlink() or not source.resolve().is_relative_to(data):raise ValueError('Data path escapes data root')
                relative=source.relative_to(data).as_posix();target=payload/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                database=source.suffix in ('.db','.sqlite','.sqlite3')
                if source==native:project_native(source,target,work/'native-snapshot.sqlite3')
                elif database:snapshot(source,target)
                else:copy_stable(source,target)
                target.chmod(0o600)
                manifest.append({'path':relative,'bytes':target.stat().st_size,'sha256':digest(target),'sqlite':database})
            document={'format':FORMAT,'started_at':started,'completed_at':time.time(),
                'consistency':'Per-database snapshots; files checked for concurrent modification; not a global transaction',
                'excluded':'Provider keys, dashboard auth, ADB pairing, QQ login, native auth/config tables, old backups',
                'native_tables':sorted(NATIVE_TABLES) if native.exists() else [],
                'unavailable_native_attachments':missing,'files':manifest}
            with zipfile.ZipFile(partial,'x',compression=zipfile.ZIP_DEFLATED) as archive:
                partial.chmod(0o600)
                archive.writestr('manifest.json',json.dumps(document,ensure_ascii=False,indent=2))
                for item in manifest:archive.write(payload/item['path'],'payload/'+item['path'])
            verify_archive(partial)
            os.replace(partial,final)
        return final
    finally:
        partial.unlink(missing_ok=True)


def safe_relative(value):
    if not isinstance(value,str):raise ValueError('Invalid archive path')
    p=PurePosixPath(value)
    if not value or p.is_absolute() or '\\' in value or ':' in value or '..' in p.parts or p.as_posix()!=value:
        raise ValueError('Unsafe archive path')
    return p


def verify_open(archive):
    names=archive.namelist()
    if len(names)!=len(set(names)) or 'manifest.json' not in names:raise ValueError('Duplicate or missing archive entries')
    if archive.getinfo('manifest.json').file_size>16*1024*1024:raise ValueError('Manifest too large')
    manifest=json.loads(archive.read('manifest.json'))
    if manifest.get('format')!=FORMAT or not isinstance(manifest.get('files'),list):raise ValueError('Unsupported archive format')
    expected={'manifest.json'}
    for item in manifest['files']:
        relative=safe_relative(item['path']);name='payload/'+relative.as_posix()
        if name in expected:raise ValueError('Duplicate manifest path')
        expected.add(name)
        if type(item.get('bytes')) is not int or item['bytes']<0:raise ValueError('Invalid file size')
        info=archive.getinfo(name)
        if info.file_size!=item['bytes'] or (info.external_attr>>16)&0o170000==0o120000:raise ValueError('Invalid file entry')
        hashed=hashlib.sha256()
        with archive.open(name) as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):hashed.update(chunk)
        if hashed.hexdigest()!=item['sha256']:raise ValueError('Archive checksum mismatch')
    if set(names)!=expected:raise ValueError('Unexpected archive entries')
    if not any(i['path']=='plugin_data/companion_memory/memory.sqlite3' for i in manifest['files']):
        raise ValueError('Required memory database missing')
    return manifest


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:manifest=verify_open(archive)
    return {'format':FORMAT,'files':len(manifest['files']),'bytes':sum(i['bytes'] for i in manifest['files'])}


def restore_archive(path,destination):
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ValueError('Restore destination must not exist')
    if not destination.parent.is_dir():raise ValueError('Restore parent must already exist')
    # Validate before creating anything. Extract manually, never extractall().
    with zipfile.ZipFile(path) as archive:
        manifest=verify_open(archive)
        with tempfile.TemporaryDirectory(prefix='.memory-restore-',dir=destination.parent) as temporary:
            stage=Path(temporary);stage.chmod(0o700)
            for item in manifest['files']:
                target=stage/'data'/safe_relative(item['path'])
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open('payload/'+item['path']) as source,target.open('xb') as out:shutil.copyfileobj(source,out)
                target.chmod(0o600)
                if item.get('sqlite'):
                    with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored SQLite file invalid')
            (stage/'RESTORE.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            (stage/'RESTORE.json').chmod(0o600)
            if destination.exists():raise ValueError('Restore destination appeared during validation')
            # Reserve the name exclusively; never replace an existing directory.
            destination.mkdir(mode=0o700)
            try:
                for child in stage.iterdir():shutil.move(str(child),str(destination/child.name))
            except BaseException:
                shutil.rmtree(destination);raise
    return {'restored':True,'files':len(manifest['files']),'destination':str(destination),
            'notice':'Isolated data only; no credentials restored, services started or existing instance overwritten'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='operation',required=True)
    backup=commands.add_parser('backup');backup.add_argument('--data-root',type=Path,required=True);backup.add_argument('--output',type=Path,required=True)
    verify=commands.add_parser('verify');verify.add_argument('archive',type=Path)
    restore=commands.add_parser('restore');restore.add_argument('archive',type=Path);restore.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    if args.operation=='backup':print(json.dumps({'archive':str(create_archive(args.data_root,args.output))}))
    elif args.operation=='verify':print(json.dumps(verify_archive(args.archive)))
    else:print(json.dumps(restore_archive(args.archive,args.destination)))


if __name__=='__main__':main()
