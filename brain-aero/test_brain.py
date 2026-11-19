import contextlib, io, json, tempfile, unittest
from pathlib import Path
import brain

class BrainTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.root = Path(self.td.name)
        for d in ('manuals','handoffs','kb'): (self.root/d).mkdir()
        (self.root/'manuals'/'BPMSOFT_PING_SETUP.md').write_text('# BPMSoft ping\ntext about ping and T-010\n## Details\nMore text\n', encoding='utf-8')
        (self.root/'handoffs'/'h.md').write_text('# Handoff\nпинг BPMSoft\n', encoding='utf-8')
        (self.root/'kb'/'rules.txt').write_text('\nfirst rule\n\nsecond rule\n', encoding='utf-8')
        self.cfg = self.root/'cfg.json'; self.cfg.write_text(json.dumps({'sources':[{'kind':'manual','root':'manuals','glob':'*.md'},{'kind':'handoff','root':'handoffs','glob':'*.md'},{'kind':'kb','root':'kb','glob':'**/*'}], 'database':'brain.db','synonyms':{'пинг':['ping']}}), encoding='utf-8')
        self.c = brain.config_load(self.cfg)
    def tearDown(self): self.td.cleanup()
    def test_build_find_incremental_delete(self):
        s=brain.build(self.c); self.assertEqual(s['indexed'],3)
        self.assertGreaterEqual(len(brain.find(self.c,'пинг BPMSoft',limit=3)),1)
        s=brain.build(self.c); self.assertEqual(s['unchanged'],3); self.assertEqual(s['indexed'],0)
        (self.root/'kb'/'rules.txt').write_text('changed\n',encoding='utf-8'); s=brain.build(self.c); self.assertEqual(s['indexed'],1)
        (self.root/'handoffs'/'h.md').unlink(); s=brain.build(self.c); self.assertEqual(s['removed'],1)
    def test_chunks_and_safe_queries(self):
        brain.build(self.c); con=brain.sqlite3.connect(str(brain.db_path(self.c))); rows=con.execute('select heading,line_from,line_to from chunks where kind="manual"').fetchall(); con.close(); self.assertEqual(rows[0][0],'BPMSoft ping'); self.assertEqual(rows[0][1],1); self.assertEqual(rows[0][2],2); brain.find(self.c,'"T-010": x-y',limit=5)

    def test_check_and_validated_filters(self):
        (self.root/'manuals'/'bad-2024-02-31.md').write_bytes(b'# bad 2024-02-31\n')
        # overlap the manual source to verify duplicate detection without duplicate rows
        self.c['sources'].append({'kind':'manual','root':'manuals','glob':'*.md'})
        brain.build(self.c)
        report = brain.check(self.c)
        self.assertEqual(report['invalid_dates'], 1)
        self.assertGreaterEqual(report['duplicates'], 1)
        with self.assertRaises(ValueError): brain.find(self.c, 'ping', kinds=['bogus'])
        with self.assertRaises(ValueError): brain.find(self.c, 'ping', since='2024-02-31')

    def test_invalid_utf8_removes_stale(self):
        p = self.root/'kb'/'broken.txt'; p.write_text('searchable', encoding='utf-8'); brain.build(self.c)
        p.write_bytes(b'\xff'); stats = brain.build(self.c)
        self.assertEqual(stats['errors'], 1)
        con = brain.sqlite3.connect(str(brain.db_path(self.c)))
        self.assertEqual(con.execute('select count(*) from files where path like ?', ('%broken.txt',)).fetchone()[0], 0)
        con.close()

    def test_issue_todo_brief_eval_are_read_only(self):
        issue_db = self.root/'watch.db'
        con = brain.sqlite3.connect(str(issue_db))
        con.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,repo TEXT,num INTEGER,title TEXT,created TEXT,body TEXT)')
        con.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?)', [
            (1, 'issue', 'org/repo', 46, 'Pilot 46', '2026-11-19T10:00:00Z', 'Decision: proceed with pilot 46'),
            (2, 'comment', 'org/repo', 46, 'Pilot 46', '2026-11-18T10:00:00Z', 'Clarified the pilot 46 step'),
            (3, 'commit', 'org/repo', None, 'main', '2026-11-19T11:00:00Z', 'pilot 46 unrelated commit'),
            (4, 'comment', 'org/repo', 47, 'Another issue', 'bad-date', 'unrelated comment'),
        ])
        con.commit(); con.close()
        todo = self.root/'TODO.md'
        todo.write_text('## Open\n- [ ] T-101 · 2026-11-19 · lab · P1 · pilot 46 implementation\n'
                        '## Done\n- [x] T-102 · 2026-11-18 · lab · P2 · pilot 46 completed\n', encoding='utf-8')
        self.c['sources'].extend([
            {'kind': 'issue', 'root': 'watch.db'},
            {'kind': 'todo', 'root': 'TODO.md'},
        ])
        original = issue_db.read_bytes()
        stats = brain.build(self.c)
        self.assertEqual(stats['indexed'], 5)
        with contextlib.redirect_stdout(io.StringIO()):
            issues = brain.find(self.c, 'pilot 46', kinds=['issue'], limit=10)
            recent = brain.find(self.c, 'pilot 46', kinds=['issue'], since='2026-11-19', limit=10)
            digest = brain.brief(self.c, 'pilot 46', limit=8)
        self.assertEqual({row[0] for row in issues}, {'issue'})
        self.assertEqual(len(issues), 2)
        self.assertEqual(len(recent), 1)
        self.assertEqual({row[0] for row in digest}, {'issue', 'todo'})
        self.assertTrue(any('T-101' in row[4] for row in digest))
        self.assertFalse(any('T-102' in row[4] for row in digest))
        report = brain.check(self.c)
        self.assertEqual(report['invalid_issue_db'], 0)
        self.assertEqual(report['invalid_dates'], 1)
        self.assertEqual(issue_db.read_bytes(), original)
        self.assertEqual(brain.build(self.c)['unchanged'], 5)
        eval_file = self.root/'eval.txt'
        eval_file.write_text('# baseline|issue|fixture|1/1|0/1\n'
                             '# baseline|manual|INDEX.md|1/1|1/1\n'
                             'K | issue:org/repo#46 | pilot 46 decision\n'
                             'P | todo:T-101 | pilot 46 implementation\n'
                             'K | manual:BPMSOFT_PING_SETUP.md#Details | More text\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = brain.evaluate(self.c, eval_file)
        self.assertIn('baseline method', output.getvalue())
        self.assertIn('fixture', output.getvalue())
        self.assertIn('INDEX.md', output.getvalue())
        self.assertIn('1/1 100%', output.getvalue())
        self.assertEqual(result['issue']['K'], [1, 1])
        self.assertEqual(result['todo']['P'], [1, 1])
        self.assertEqual(result['manual']['K'], [1, 1])
        eval_file.write_text('P | todo:T-10 | pilot 46 implementation\n'
                             'K | issue:org/repo#4 | pilot 46 decision\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            prefix_result = brain.evaluate(self.c, eval_file)
        self.assertEqual(prefix_result['todo']['P'], [0, 1])
        self.assertEqual(prefix_result['issue']['K'], [0, 1])
        eval_file.write_text('K | unsupported:thing | pilot 46\n', encoding='utf-8')
        with self.assertRaises(ValueError): brain.evaluate(self.c, eval_file)
        with self.assertRaises(ValueError): brain.find(self.c, 'pilot', limit=-1)
        with self.assertRaises(ValueError): brain.brief(self.c, 'pilot', limit=-1)
        con = brain.sqlite3.connect(str(issue_db))
        con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                    (5, 'comment', 'org/repo', 46, 'Pilot 46', '2026-11-19T12:00:00Z', 'new pilot 46 note'))
        con.commit(); con.close()
        todo.write_text(todo.read_text(encoding='utf-8') +
                        '- [ ] T-103 · 2026-11-19 · lab · P2 · pilot 46 follow-up\n', encoding='utf-8')
        self.assertEqual(brain.build(self.c)['indexed'], 2)
        con = brain.sqlite3.connect(str(issue_db)); con.execute('DELETE FROM events WHERE id=5'); con.commit(); con.close()
        todo.write_text('## Open\n- [ ] T-101 · 2026-11-19 · lab · P1 · pilot 46 implementation\n'
                        '## Done\n- [x] T-102 · 2026-11-18 · lab · P2 · pilot 46 completed\n', encoding='utf-8')
        self.assertEqual(brain.build(self.c)['indexed'], 2)
        issue_db.write_bytes(b'not a sqlite database')
        failed = brain.build(self.c)
        self.assertEqual(failed['errors'], 1)
        con = brain.sqlite3.connect(str(brain.db_path(self.c)))
        self.assertEqual(con.execute('SELECT count(*) FROM chunks WHERE kind="issue"').fetchone()[0], 3)
        con.close()
        issue_db.unlink()
        missing = brain.build(self.c)
        self.assertEqual(missing['removed'], 0)
        empty_watch = self.root/'empty_watch'; empty_watch.mkdir()
        self.c['sources'].append({'kind': 'issue', 'root': 'empty_watch'})
        self.assertEqual(brain.check(self.c)['missing_sources'], 2)
        con = brain.sqlite3.connect(str(brain.db_path(self.c)))
        self.assertEqual(con.execute('SELECT count(*) FROM chunks WHERE kind="issue"').fetchone()[0], 3)
        con.close()

    def test_local_embedding_build_search_and_cleanup(self):
        semantic = self.root/'manuals'/'semantic.md'
        lexical = self.root/'manuals'/'lexical.md'
        lexical_two = self.root/'manuals'/'lexical_two.md'
        semantic.write_text('# Semantic candidate\nunrelated passage\n', encoding='utf-8')
        lexical.write_text('# rarequery reference\nrarequery lexical passage\n', encoding='utf-8')
        lexical_two.write_text('# rarequery second reference\nrarequery another passage\n', encoding='utf-8')
        self.c['embeddings'] = {
            'enabled': True, 'model': 'fixture-model', 'cache_dir': 'model-cache',
            'semantic_weight': 100.0, 'lexical_weight': 1.0,
        }

        class FakeEmbedder:
            def embed(self, documents, batch_size=32):
                return [[1.0, 0.0] if 'Semantic candidate' in text else [0.0, 1.0]
                        for text in documents]

            def query_embed(self, query):
                return [[1.0, 0.0]]

        original_get_embedder = brain._get_embedder
        brain._get_embedder = lambda config: (FakeEmbedder(), brain.embedding_settings(config))
        try:
            stats = brain.build(self.c)
            self.assertGreater(stats['embedded'], 0)
            lexical_rows = brain.search_rows(self.c, 'rarequery', limit=20)
            reranked_rows = brain.search_rows(self.c, 'rarequery', limit=1)
            semantic_rows = brain.search_rows(self.c, 'unmatchedquery', limit=5)
            self.assertIn('rarequery reference', {row[4] for row in lexical_rows})
            self.assertEqual(reranked_rows[0][4], 'Semantic candidate')
            self.assertEqual(semantic_rows[0][4], 'Semantic candidate')
            report = brain.check(self.c)
            self.assertEqual(report['embedding_missing'], 0)
            self.assertEqual(report['orphan_embeddings'], 0)
            self.assertEqual(report['invalid_embeddings'], 0)
            semantic.unlink()
            brain.build(self.c)
            report = brain.check(self.c)
            self.assertEqual(report['orphan_embeddings'], 0)
            con = brain.sqlite3.connect(str(brain.db_path(self.c)))
            self.assertEqual(con.execute('SELECT count(*) FROM embeddings').fetchone()[0],
                             con.execute('SELECT count(*) FROM chunks').fetchone()[0])
            con.close()
        finally:
            brain._get_embedder = original_get_embedder

if __name__=='__main__': unittest.main()
