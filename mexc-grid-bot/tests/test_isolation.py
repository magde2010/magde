import tempfile,unittest
from pathlib import Path
from gridbot.db import GridDB
class T(unittest.TestCase):
 def test_only_grid_tables(self):
  with tempfile.TemporaryDirectory() as d:
   db=GridDB(Path(d)/"grid.sqlite3")
   names={r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   self.assertEqual(names,{"grid_bot_configs","grid_positions","grid_orders","grid_events","sqlite_sequence"}); db.close()
if __name__=="__main__": unittest.main()
