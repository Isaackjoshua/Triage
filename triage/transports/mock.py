"""MockTransport — a scripted patient, so the whole loop is testable with no real machine.

This exists for two reasons. The obvious one is tests. The important one is that you
should never rehearse a diagnostic agent for the first time on an actual broken computer,
and the acceptance criteria for this system are "the full flow runs end to end against a
mock" long before they are "it runs against hardware".

The bundled fixture (`faulty_workstation`) is deliberately not a single tidy fault. It has
one software-fixable problem, one that is hardware, and enough overlap between their
symptoms that concluding correctly requires actually reading the evidence — an unrotated
log has filled /var *and* a second disk is throwing uncorrectable read errors. A correct
triage fixes the first, refuses to "fix" the second, and says so.

Responses can be gated on state, so the machine's answers change after an approved
remediation is applied. That is what makes the verify step a real test rather than a
formality.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..core.capability import Capability
from ..core.models import CommandResult, SnapshotKind, SnapshotRef, TransportInfo
from .base import Transport


@dataclass
class MockResponse:
    """One scripted answer.

    `requires_state` / `sets_state` let the fixture model a machine that actually
    changes when something is applied to it.
    """

    pattern: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    requires_state: set[str] = field(default_factory=set)
    forbids_state: set[str] = field(default_factory=set)
    sets_state: set[str] = field(default_factory=set)

    def matches(self, command: str, state: set[str]) -> bool:
        if self.requires_state - state:
            return False
        if self.forbids_state & state:
            return False
        return re.search(self.pattern, command) is not None


class MockTransport(Transport):
    name = "mock"

    def __init__(
        self,
        script: list[MockResponse] | None = None,
        target: str = "mock-target",
        capability: Capability = Capability.EXECUTE_RW,
        **kwargs: object,
    ) -> None:
        snapshot_capable = bool(kwargs.pop("snapshot_capable", True))
        filesystem_type = str(kwargs.pop("filesystem_type", "ext4"))
        super().__init__(target=target, capability=capability, **kwargs)  # type: ignore[arg-type]
        self.script = script or []
        self.state: set[str] = set()
        #: Set False to rehearse the "no rollback point available" branch of approval.
        self.snapshot_capable = snapshot_capable
        #: "btrfs"/"zfs" put the mock on the snapshot rung of the ladder instead.
        self.filesystem_type = filesystem_type
        #: Every command the mock was asked to run, for assertions in tests.
        self.executed: list[str] = []

    def describe(self) -> TransportInfo:
        return TransportInfo(
            name=self.name,
            capability=self.capability.value,
            reachable=True,
            target=self.target,
            supports_snapshot=True,
            supports_observation=self.observation_provider is not None,
            detail="Scripted mock target. Nothing here touches a real machine.",
        )

    async def _execute(self, command: str, timeout_s: float) -> CommandResult:
        started = time.monotonic()
        self.executed.append(command)
        for response in self.script:
            if response.matches(command, self.state):
                self.state |= response.sets_state
                return CommandResult(
                    command=command,
                    exit_code=response.exit_code,
                    stdout=response.stdout,
                    stderr=response.stderr,
                    duration_s=time.monotonic() - started,
                )
        return CommandResult(
            command=command,
            exit_code=127,
            stderr=f"mock: no scripted response for: {command}",
            duration_s=time.monotonic() - started,
        )

    async def snapshot(self, scope: str) -> SnapshotRef | None:
        if self.dry_run:
            return await super().snapshot(scope)
        if not self.snapshot_capable:
            return None
        return SnapshotRef(
            kind=SnapshotKind.BTRFS,
            scope=scope,
            rollback_hint=f"btrfs subvolume snapshot restore (mock) for {scope}",
            detail="Mock snapshot — no data was actually captured.",
        )

    async def backup_files(self, paths: list[str]) -> SnapshotRef | None:
        """The file-backup rung of the snapshot ladder, so the mock is honest about it."""
        if not paths:
            return None
        if self.dry_run:
            return await super().snapshot(", ".join(paths))
        if not self.snapshot_capable:
            return None
        return SnapshotRef(
            kind=SnapshotKind.FILE_BACKUP,
            scope=", ".join(paths),
            rollback_hint=f"cp -a /var/backups/triage/<id>/<path> <path> (mock) for {paths}",
            detail="Mock file backup — no data was actually copied.",
        )

    async def _filesystem_type(self, path: str) -> str:
        """The mock's /var is plain ext4, so the snapshot ladder falls to file backup."""
        return self.filesystem_type


# ---------------------------------------------------------------------------------------
# The bundled fixture
# ---------------------------------------------------------------------------------------

_DF_FULL = """Filesystem      Size  Used Avail Use% Mounted on
/dev/sda2       234G   61G  161G  28% /
/dev/sda1       511M  6.1M  505M   2% /boot/efi
/dev/sdb1        49G   49G     0 100% /var
tmpfs            16G  118M   16G   1% /dev/shm
"""

_DF_FREED = """Filesystem      Size  Used Avail Use% Mounted on
/dev/sda2       234G   61G  161G  28% /
/dev/sda1       511M  6.1M  505M   2% /boot/efi
/dev/sdb1        49G  6.2G   41G  14% /var
tmpfs            16G  118M   16G   1% /dev/shm
"""

_FAILED_UNITS = """  UNIT             LOAD   ACTIVE SUB    DESCRIPTION
* ledger.service   loaded failed failed Ledger ingest worker

LOAD   = Reflects whether the unit definition was properly loaded.
ACTIVE = The high-level unit activation state.
SUB    = The low-level unit activation state.
1 loaded units listed.
"""

_NO_FAILED_UNITS = """  UNIT LOAD ACTIVE SUB DESCRIPTION
0 loaded units listed.
"""

_UNIT_STATUS_FAILED = """* ledger.service - Ledger ingest worker
     Loaded: loaded (/etc/systemd/system/ledger.service; enabled)
     Active: failed (Result: exit-code) since Tue 2026-08-11 03:14:22 UTC; 2 days ago
    Process: 1841 ExecStart=/usr/local/bin/ledger-ingest (code=exited, status=1/FAILURE)

Aug 11 03:14:22 ws-14 ledger-ingest[1841]: FATAL: could not write checkpoint
Aug 11 03:14:22 ws-14 ledger-ingest[1841]: OSError: [Errno 28] No space left on device: '/var/lib/ledger/ckpt.tmp'
Aug 11 03:14:22 ws-14 systemd[1]: ledger.service: Main process exited, code=exited, status=1/FAILURE
Aug 11 03:14:22 ws-14 systemd[1]: ledger.service: Failed with result 'exit-code'.
"""

_UNIT_STATUS_OK = """* ledger.service - Ledger ingest worker
     Loaded: loaded (/etc/systemd/system/ledger.service; enabled)
     Active: active (running) since Thu 2026-08-13 09:02:10 UTC; 4s ago
   Main PID: 5120 (ledger-ingest)

Aug 13 09:02:10 ws-14 systemd[1]: Started Ledger ingest worker.
Aug 13 09:02:11 ws-14 ledger-ingest[5120]: checkpoint written, resuming from offset 44182
"""

_JOURNAL_ERRORS = """Aug 11 03:14:22 ws-14 ledger-ingest[1841]: OSError: [Errno 28] No space left on device
Aug 11 03:14:22 ws-14 systemd[1]: ledger.service: Failed with result 'exit-code'.
Aug 12 22:41:07 ws-14 kernel: blk_update_request: I/O error, dev sdb, sector 1902847 op 0x0:(READ)
Aug 12 22:41:07 ws-14 kernel: EXT4-fs warning (device sdb1): ext4_end_bio:343: I/O error 10 writing to inode 262147
Aug 13 01:09:55 ws-14 kernel: blk_update_request: I/O error, dev sdb, sector 1902851 op 0x0:(READ)
Aug 13 01:09:55 ws-14 smartd[912]: Device: /dev/sdb, 24 Currently unreadable (pending) sectors
"""

_DMESG = """[691402.118331] ata2.00: exception Emask 0x0 SAct 0x8 SErr 0x0 action 0x0
[691402.118344] ata2.00: irq_stat 0x40000008
[691402.118352] ata2.00: failed command: READ FPDMA QUEUED
[691402.118368] ata2.00: status: { DRDY ERR }
[691402.118374] ata2.00: error: { UNC }
[691402.121905] blk_update_request: I/O error, dev sdb, sector 1902847 op 0x0:(READ) flags 0x0
[691402.121918] EXT4-fs warning (device sdb1): ext4_end_bio:343: I/O error 10 writing to inode 262147
[694655.884210] ata2.00: exception Emask 0x0 SAct 0x20 SErr 0x0 action 0x0
[694655.884233] ata2.00: error: { UNC }
[694655.887701] blk_update_request: I/O error, dev sdb, sector 1902851 op 0x0:(READ) flags 0x0
"""

_SMART_SDA = """smartctl 7.4 2023-08-01 r5530 [x86_64-linux] (local build)

=== START OF INFORMATION SECTION ===
Device Model:     Samsung SSD 870 EVO 250GB
Serial Number:    S6PENL0T412886X
Rotation Rate:    Solid State Device

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  0
  9 Power_On_Hours          0x0032   095   095   000    Old_age   19204
177 Wear_Leveling_Count     0x0013   094   094   000    Pre-fail  62
197 Current_Pending_Sector  0x0032   100   100   000    Old_age   0
198 Offline_Uncorrectable   0x0030   100   100   000    Old_age   0
199 UDMA_CRC_Error_Count    0x0032   100   100   000    Old_age   0
"""

_SMART_SDB = """smartctl 7.4 2023-08-01 r5530 [x86_64-linux] (local build)

=== START OF INFORMATION SECTION ===
Device Model:     WDC WD10EZEX-08WN4A0
Serial Number:    WD-WCC6Y5KJTZ8P
Rotation Rate:    7200 rpm

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   118   118   140    Pre-fail  486
  9 Power_On_Hours          0x0032   041   041   000    Old_age   43318
187 Reported_Uncorrect      0x0032   001   001   000    Old_age   1642
197 Current_Pending_Sector  0x0032   200   200   000    Old_age   24
198 Offline_Uncorrectable   0x0030   200   200   000    Old_age   24
199 UDMA_CRC_Error_Count    0x0032   200   200   000    Old_age   0

SMART Error Log Version: 1
ATA Error Count: 1642 (device log contains only the most recent five errors)
  Error 1642 occurred at disk power-on lifetime: 43317 hours
    Error: UNC at LBA = 0x001d0bff = 1902847
"""

_DU_VAR_LOG = """39G     /var/log/ledger
44M     /var/log/journal
12M     /var/log/apt
39G     /var/log
"""

_LS_LEDGER_LOG = """total 39G
drwxr-xr-x 2 ledger ledger 4.0K Aug 13 09:01 .
-rw-r--r-- 1 ledger ledger  39G Aug 13 09:01 debug.log
-rw-r--r-- 1 ledger ledger  18M Jun 02 04:00 debug.log.1.gz
"""

_LOGROTATE_CONF = """/var/log/ledger/*.log {
    weekly
    rotate 4
    compress
    missingok
}
"""

_LSBLK = """NAME   FSTYPE FSVER LABEL SIZE MOUNTPOINTS
sda                          232G
|-sda1 vfat   FAT32          512M /boot/efi
`-sda2 ext4   1.0            232G /
sdb                          932G
`-sdb1 ext4   1.0     varlog  50G /var
"""

_FREE = """               total        used        free      shared  buff/cache   available
Mem:            31Gi       6.1Gi        18Gi       118Mi       7.2Gi        24Gi
Swap:          8.0Gi          0B       8.0Gi
"""

_SENSORS = """coretemp-isa-0000
Adapter: ISA adapter
Package id 0:  +44.0 C  (high = +84.0 C, crit = +100.0 C)
Core 0:        +42.0 C  (high = +84.0 C, crit = +100.0 C)
Core 1:        +43.0 C  (high = +84.0 C, crit = +100.0 C)

nvme-pci-0100
Adapter: PCI adapter
Composite:     +38.9 C
"""

_MEMORY_DMI = """# dmidecode 3.5
Handle 0x0010, DMI type 17, 40 bytes
Memory Device
        Size: 16384 MB
        Locator: DIMM_A1
        Speed: 3200 MT/s
        Manufacturer: Micron

Handle 0x0012, DMI type 17, 40 bytes
Memory Device
        Size: 16384 MB
        Locator: DIMM_B1
        Speed: 3200 MT/s
        Manufacturer: Micron
"""

_EDAC = """0
0
"""


def faulty_workstation(**kwargs: object) -> MockTransport:
    """A Linux workstation with two independent faults, one of each kind.

    * **Software-fixable:** `ledger.service` is dead because /var is 100% full, and /var
      is full because `/var/log/ledger/debug.log` grew to 39G — logrotate only matches
      `*.log` weekly and the service never reopened its handle. Truncating the file and
      restarting the unit is a real, reversible fix.
    * **Hardware-suspected:** /dev/sdb (the disk /var lives on) reports 486 reallocated
      sectors, 24 current-pending and 24 offline-uncorrectable, 1642 reported-uncorrect,
      and the kernel is logging UNC read errors at a specific LBA. No software change
      fixes that; the honest output is a hardware finding with a physical next step.

    The overlap is the point: both faults produce errors mentioning sdb and /var, so the
    two have to be told apart on evidence rather than on which one was noticed first.
    """
    script = [
        # --- disk usage, before and after the fix ------------------------------------
        MockResponse(r"^\s*(sudo\s+)?df\b", stdout=_DF_FULL, forbids_state={"log_truncated"}),
        MockResponse(r"^\s*(sudo\s+)?df\b", stdout=_DF_FREED, requires_state={"log_truncated"}),
        MockResponse(r"\bdu\b.*(/var/log|/var\b)", stdout=_DU_VAR_LOG, forbids_state={"log_truncated"}),
        MockResponse(r"\bdu\b.*(/var/log|/var\b)", stdout="6.2G    /var/log\n", requires_state={"log_truncated"}),
        MockResponse(r"\bls\b.*/var/log/ledger", stdout=_LS_LEDGER_LOG, forbids_state={"log_truncated"}),
        MockResponse(
            r"\bls\b.*/var/log/ledger",
            stdout="total 18M\n-rw-r--r-- 1 ledger ledger    0 Aug 13 09:05 debug.log\n"
            "-rw-r--r-- 1 ledger ledger  18M Jun 02 04:00 debug.log.1.gz\n",
            requires_state={"log_truncated"},
        ),
        MockResponse(r"\bcat\b.*logrotate.*ledger", stdout=_LOGROTATE_CONF),
        # --- the applied remediations -------------------------------------------------
        MockResponse(
            r"truncate\b.*-s\s*0.*debug\.log",
            stdout="",
            sets_state={"log_truncated"},
        ),
        MockResponse(
            r"systemctl\s+(restart|start)\s+ledger",
            stdout="",
            sets_state={"ledger_restarted"},
        ),
        # --- systemd -------------------------------------------------------------------
        MockResponse(
            r"systemctl\s+.*--failed", stdout=_FAILED_UNITS, forbids_state={"ledger_restarted"}
        ),
        MockResponse(
            r"systemctl\s+.*--failed", stdout=_NO_FAILED_UNITS, requires_state={"ledger_restarted"}
        ),
        MockResponse(
            r"systemctl\s+status\s+ledger",
            stdout=_UNIT_STATUS_FAILED,
            exit_code=3,
            forbids_state={"ledger_restarted"},
        ),
        MockResponse(
            r"systemctl\s+status\s+ledger",
            stdout=_UNIT_STATUS_OK,
            requires_state={"ledger_restarted"},
        ),
        MockResponse(
            r"systemctl\s+cat\s+ledger",
            stdout="# /etc/systemd/system/ledger.service\n[Service]\n"
            "ExecStart=/usr/local/bin/ledger-ingest\nRestart=on-failure\nRestartSec=30\n",
        ),
        # --- logs and kernel -----------------------------------------------------------
        MockResponse(r"journalctl", stdout=_JOURNAL_ERRORS),
        MockResponse(r"dmesg", stdout=_DMESG),
        # --- storage health --------------------------------------------------------------
        MockResponse(r"smartctl.*\bsda\b", stdout=_SMART_SDA),
        MockResponse(r"smartctl.*\bsdb\b", stdout=_SMART_SDB),
        MockResponse(r"smartctl", stderr="smartctl: please specify a device\n", exit_code=1),
        MockResponse(r"\blsblk\b", stdout=_LSBLK),
        MockResponse(
            r"\bfindmnt\b",
            stdout="TARGET SOURCE    FSTYPE OPTIONS\n/      /dev/sda2 ext4   rw,relatime\n"
            "/var   /dev/sdb1 ext4   rw,relatime\n",
        ),
        MockResponse(r"\bblkid\b", stdout='/dev/sdb1: LABEL="varlog" UUID="8f2a-4c1d" TYPE="ext4"\n'),
        # --- general system state ----------------------------------------------------------
        MockResponse(r"\bfree\b", stdout=_FREE),
        MockResponse(r"\bsensors\b", stdout=_SENSORS),
        MockResponse(r"\buptime\b", stdout=" 09:02:14 up 8 days,  1:47,  2 users,  load average: 0.41, 0.55, 0.61\n"),
        MockResponse(r"\buname\b", stdout="Linux ws-14 6.8.0-40-generic #40-Ubuntu SMP x86_64 GNU/Linux\n"),
        MockResponse(r"dmidecode.*memory", stdout=_MEMORY_DMI),
        MockResponse(r"\bhostnamectl\b", stdout="Static hostname: ws-14\n  Hardware Model: OptiPlex 7080\n"),
        MockResponse(r"edac|mce", stdout=_EDAC),
        MockResponse(r"\bip\s+(a|addr|link|route)", stdout="2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP> state UP\n"),
        MockResponse(r"\bps\b", stdout="  PID TTY          TIME CMD\n 5120 ?        00:00:01 ledger-ingest\n"),
    ]
    return MockTransport(script=script, target="ws-14 (mock faulty workstation)", **kwargs)
