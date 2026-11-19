import json, tempfile, unittest
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

if __name__=='__main__': unittest.main()
