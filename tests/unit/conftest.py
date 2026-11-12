import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeAdapter  # noqa: E402
from jarvis_desktop.approvals import ApprovalManager  # noqa: E402
from jarvis_desktop.audit import AuditLogger  # noqa: E402
from jarvis_desktop.broker import Broker  # noqa: E402
from jarvis_desktop.credentials import CredentialGuard  # noqa: E402
from jarvis_desktop.emergency_stop import EmergencyStop  # noqa: E402
from jarvis_desktop.policy import Policy  # noqa: E402


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.adapter = FakeAdapter()
        self.policy = Policy.load()
        self.audit = AuditLogger(tmp / "audit", restrict_acl=False)
        self.estop = EmergencyStop(tmp / "EMERGENCY_STOP")
        self.approvals = ApprovalManager()
        self.guard = CredentialGuard()
        self.probe_dir = tmp / "probe-output"
        self.probe_dir.mkdir()
        self.broker = Broker(self.adapter, self.policy, self.audit, self.estop, self.approvals, self.guard,
                             probe_dir=self.probe_dir, launch_target="code.exe", launch_args=["-n", "x"])

    def audit_lines(self) -> str:
        return self.audit.path.read_text(encoding="utf-8") if self.audit.path.exists() else ""


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
