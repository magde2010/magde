import os, tempfile, unittest
from pathlib import Path
from gridbot.config import BotConfig
from gridbot.lock import SingletonLock
from decimal import Decimal as D
class ConfigLockTests(unittest.TestCase):
    def test_live_requires_explicit_flag(self):
        c=BotConfig(dry_run=False,live_enabled=False)
        with self.assertRaises(ValueError): c.validate()
        BotConfig(dry_run=False,live_enabled=True).validate()
    def test_unimplemented_stop_loss_fails_closed(self):
        with self.assertRaises(ValueError): BotConfig(stop_loss_enabled=True,stop_loss_percent=D('5')).validate()
    def test_exactly_four_dca_levels(self):
        c=BotConfig(); c.dca_drops.append(D('12'))
        with self.assertRaises(ValueError): c.validate()
    def test_singleton_lock_blocks_second_instance(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'lockfile'
            first=SingletonLock(path).acquire()
            try:
                with self.assertRaises(RuntimeError): SingletonLock(path).acquire()
            finally: first.release()
if __name__=='__main__': unittest.main()
