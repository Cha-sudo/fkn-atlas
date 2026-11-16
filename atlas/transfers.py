"""Fenced disk checkpoints. Incomplete bytes are never snapshots or search content."""
from contextlib import contextmanager
import hashlib
import os
import re
import time
import uuid
from .store import digest, file_digest


class Transfer:
    def __init__(self, store, job, check=None):
        self.store,self.job,self.rid,self.check=store,job,job['resource_id'],check
        with self.lock():
            store.db.execute('''INSERT OR IGNORE INTO transfers
                (resource_id,filename,received,prefix_sha,state,updated) VALUES(?,?,0,?,'partial',?)''',
                (self.rid,self.rid+'.'+uuid.uuid4().hex+'.part',digest(b''),time.time()))
        self.reload()
        filename=self.row['filename']
        if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{32}\.part',filename):
            raise ValueError('Invalid staging filename')
        self.path=(store.home/'partials'/filename).resolve()
        if self.path.parent!=(store.home/'partials').resolve():
            raise ValueError('Invalid staging directory')
        self.hasher=hashlib.sha256()
        if self.row['restart_required']:
            self.reset('explicit_refresh')
        elif self.row['state']=='complete':
            # install_file handles recovery when rename completed before DB commit.
            return
        elif not self.path.exists():
            if self.row['received']: self.reset('missing_partial')
        else:
            remaining=self.row['received']
            with self.path.open('rb') as f:
                while remaining:
                    if check: check()
                    block=f.read(min(1024*1024,remaining))
                    if not block: break
                    self.hasher.update(block); remaining-=len(block)
            if remaining or self.hasher.hexdigest()!=self.row['prefix_sha']:
                self.reset('partial_integrity_failed')
            elif self.path.stat().st_size!=self.row['received']:
                # A process may die after fsync but before committing its checkpoint.
                with self.lock():
                    with self.path.open('r+b') as f:
                        f.truncate(self.row['received']); f.flush(); os.fsync(f.fileno())
                    self.event('truncate_uncommitted_tail')

    @contextmanager
    def lock(self):
        db=self.store.db; db.execute('BEGIN IMMEDIATE')
        try:
            changed=db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND token=? AND state='running'",
                               (time.time()+600,self.job['id'],self.job['token'])).rowcount
            if not changed: raise RuntimeError('Stale worker lease')
            yield
            db.commit()
        except BaseException:
            db.rollback(); raise

    def reload(self):
        self.row=self.store.rows('SELECT * FROM transfers WHERE resource_id=?',(self.rid,))[0]

    def event(self, name, detail=''):
        self.store.db.execute('INSERT INTO transfer_events(resource_id,at,event,received,detail) VALUES(?,?,?,?,?)',
                              (self.rid,time.time(),name,self.row['received'],detail))

    def reset(self, reason):
        with self.lock():
            self.event('restart',reason+'; previous_sha='+self.row['prefix_sha']+'; previous_etag='+str(self.row['etag']))
            # Discard only incomplete staging bytes, recording why. Successful
            # content-addressed originals and previous snapshots remain untouched.
            with self.path.open('wb') as f: f.flush(); os.fsync(f.fileno())
            self.store.db.execute('''UPDATE transfers SET received=0,prefix_sha=?,etag=NULL,
                last_modified=NULL,total=NULL,final_url=NULL,mime=NULL,original_name=NULL,
                state='partial',restart_required=0,updated=? WHERE resource_id=?''',(digest(b''),time.time(),self.rid))
        self.hasher=hashlib.sha256(); self.reload()

    def headers(self, etag, modified, total, final, mime, name):
        with self.lock():
            self.store.db.execute('''UPDATE transfers SET etag=?,last_modified=?,total=?,final_url=?,
                mime=?,original_name=?,updated=? WHERE resource_id=?''',
                (etag,modified,total,final,mime,name,time.time(),self.rid))
        self.reload()

    def append(self, block):
        updated=self.hasher.copy(); updated.update(block)
        with self.lock():
            with self.path.open('ab') as f:
                if f.tell()!=self.row['received']: raise ValueError('Staging size changed outside checkpoint')
                f.write(block); f.flush(); os.fsync(f.fileno())
            self.store.db.execute('UPDATE transfers SET received=?,prefix_sha=?,updated=? WHERE resource_id=?',
                (self.row['received']+len(block),updated.hexdigest(),time.time(),self.rid))
        self.hasher=updated; self.reload()

    def complete(self):
        with self.lock():
            self.store.db.execute("UPDATE transfers SET state='complete',total=received,updated=? WHERE resource_id=?",(time.time(),self.rid))
        self.reload()

    def publish(self):
        if self.row['state']!='complete': raise ValueError('Incomplete transfer cannot be published')
        sid=self.store.install_file(self.rid,self.path,self.row['prefix_sha'],self.row['received'],
            self.row['mime'],self.row['final_url'],self.check,self.job)
        with self.lock():
            self.event('published',sid)
            self.store.db.execute('INSERT OR IGNORE INTO retrieval_metadata VALUES(?,?)',(sid,self.row['original_name'] or 'document'))
            self.store.db.execute('DELETE FROM transfers WHERE resource_id=?',(self.rid,))
            self.store.db.execute('UPDATE jobs SET refresh=0 WHERE id=? AND token=?',(self.job['id'],self.job['token']))
        return sid
