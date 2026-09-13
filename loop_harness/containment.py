from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _current_cgroup() -> Path:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        relative = next(line[3:] for line in lines if line.startswith("0::"))
    except (OSError, StopIteration) as exc:
        raise RuntimeError("cgroup v2 membership is unavailable") from exc
    path = (_CGROUP_ROOT / relative.lstrip("/")).resolve()
    try:
        path.relative_to(_CGROUP_ROOT)
    except ValueError as exc:
        raise RuntimeError("invalid cgroup v2 membership path") from exc
    if not (path / "cgroup.procs").is_file() or not (path / "cgroup.kill").is_file():
        raise RuntimeError("cgroup v2 kill support is unavailable")
    if not os.access(path, os.W_OK):
        raise RuntimeError("current cgroup is not delegated for containment")
    return path


@dataclass(frozen=True)
class CgroupScope:
    path: Path
    parent: Path

    @classmethod
    def create(cls, kind: str) -> "CgroupScope":
        parent = _current_cgroup()
        path = parent / f"loop-{kind}-{uuid.uuid4().hex}"
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise RuntimeError("cannot create delegated cgroup containment") from exc
        return cls(path=path, parent=parent)

    def move(self, pid: int) -> None:
        (self.path / "cgroup.procs").write_text(str(pid), encoding="ascii")

    def move_to_parent(self, pid: int) -> None:
        (self.parent / "cgroup.procs").write_text(str(pid), encoding="ascii")

    def kill_all(self) -> None:
        kill = self.path / "cgroup.kill"
        if kill.exists():
            try:
                kill.write_text("1", encoding="ascii")
            except FileNotFoundError:
                pass

    def remove(self, timeout: float = 2) -> None:
        deadline = time.monotonic() + timeout
        while self.path.exists():
            try:
                self.path.rmdir()
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"contained cgroup did not drain: {self.path.name}")
                time.sleep(0.02)

    def cleanup(self) -> None:
        self.kill_all()
        self.remove()
