import os
from pathlib import Path
class SingletonLock:
    def __init__(self,path): self.path=Path(path); self.handle=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True); self.handle=open(self.path,"a+")
        self.handle.seek(0,os.SEEK_END)
        if self.handle.tell()==0: self.handle.write('0'); self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name=="nt":
                import msvcrt; self.handle.seek(0); msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl; fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError):
            self.handle.close(); self.handle=None; raise RuntimeError("Another Grid Bot instance appears to be running")
        return self
    def release(self):
        if self.handle:
            try:
                if os.name=="nt":
                    import msvcrt; self.handle.seek(0); msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl; fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
            finally: self.handle.close(); self.handle=None
    def __enter__(self): return self.acquire()
    def __exit__(self,*_): self.release()
