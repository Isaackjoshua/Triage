"""The gate is the read/touch boundary, so its failure modes matter more than its successes.

A false WRITE costs a round trip. A false READ runs an unapproved mutation on a machine
with no backup — so the cases below lean hard on the ways a read-only binary can be turned
into a write, and on the ways an unclassifiable command must fail closed.
"""

from __future__ import annotations

import pytest

from triage.core.gate import CommandGate
from triage.core.models import Classification


@pytest.mark.parametrize(
    "command",
    [
        "df -h",
        "free -h",
        "lsblk -f",
        "findmnt -T /var",
        "uptime",
        "sensors",
        "smartctl -H /dev/sda",
        "smartctl -a /dev/nvme0n1",
        "sudo smartctl -x /dev/sdb",
        "journalctl -p err -b",
        "dmesg -T",
        "systemctl --failed",
        "systemctl status ledger.service",
        "systemctl",  # bare systemctl lists units
        "systemctl cat ledger.service",
        "ip a",
        "ip route",
        "cat /proc/mdstat",
        "cat /etc/fstab",
        "head -50 /var/log/syslog",
        "zpool status -v",
        "zfs list",
        "btrfs filesystem show",
        "dmidecode -t memory",
        "sysctl -a",
        "fdisk -l",
        "apt list --installed",
        "dpkg -l",
        "rpm -qa",
        "find /var/log -name '*.log' -size +1G",
        "mount",  # bare mount lists; with arguments it mounts
        "dmesg -T | grep -i 'i/o error'",
        "journalctl -b | grep -c oom",
        "cat /var/log/syslog | tail -100 | wc -l",
        'grep "a|b" /etc/hosts',  # a quoted pipe is data, not a pipeline
    ],
)
def test_genuine_reads_are_permitted(gate: CommandGate, command: str) -> None:
    decision = gate.classify(command)
    assert decision.classification is Classification.READ, decision.reason


@pytest.mark.parametrize(
    "command",
    [
        # Mutating flags on otherwise read-only binaries.
        "smartctl -t short /dev/sda",
        "smartctl -s on /dev/sda",
        "dmesg -C",
        "dmesg --clear",
        "journalctl --vacuum-size=1G",
        "journalctl --rotate",
        "sysctl -w vm.swappiness=10",
        "sysctl vm.swappiness=10",  # assignment form, no flag
        "find /var/log -name '*.log' -delete",
        "find / -name core -exec rm {} +",
        "sort -o /etc/hosts /etc/hosts",
        "ss -K dst 10.0.0.1",
        # Mutating subcommands.
        "systemctl restart ledger.service",
        "systemctl enable ledger.service",
        "ip link set eth0 down",
        "ip addr add 10.0.0.1/24 dev eth0",
        "zpool scrub tank",
        "zfs destroy tank/data",
        "btrfs subvolume delete /mnt/sub",
        "apt install nginx",
        "dnf remove httpd",
        # Unambiguously destructive binaries.
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "rm -rf /var/log/ledger",
        "mkfs.ext4 /dev/sdb1",
        "fsck -y /dev/sdb1",
        "reboot",
        "grub-install /dev/sda",
        # A read-only binary used as a vehicle for a write.
        "cat /etc/fstab > /etc/fstab.bak",
        "echo 'x' >> /etc/fstab",
        "dmesg | tee /tmp/out",
        "df -h > /tmp/df.txt",
    ],
)
def test_writes_are_refused(gate: CommandGate, command: str) -> None:
    decision = gate.classify(command)
    assert decision.classification is Classification.WRITE, decision.reason
    assert decision.needs_approval


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "frobnicate --everything",  # not in the catalog
        "df -h; rm -rf /",  # chaining
        "df -h && rm -rf /",
        "df -h || true",
        "cat $(ls /etc)",  # command substitution
        "cat `ls /etc`",
        "cat ${HOME}/.ssh/id_rsa",
        "df -h &",  # background
        "sudo -u root rm -rf /",  # sudo option that changes what runs
        "FOO=bar df -h",  # env prefix
        "/tmp/smartctl -a /dev/sda",  # not the catalogued binary of that name
        "journalctl -f",  # would never return
        "tail -f /var/log/syslog",
        "dmesg -w",
        "df -h\nrm -rf /",  # multi-line
    ],
)
def test_unclassifiable_fails_closed(gate: CommandGate, command: str) -> None:
    """UNKNOWN is the fail-safe verdict, and it is handled exactly as WRITE."""
    decision = gate.classify(command)
    assert decision.classification is Classification.UNKNOWN, decision.reason
    assert decision.needs_approval


def test_pipeline_is_only_as_read_only_as_its_worst_segment(gate: CommandGate) -> None:
    assert gate.classify("dmesg | grep -i error").is_read
    assert not gate.classify("dmesg | grep -i error | tee /tmp/x").is_read
    assert not gate.classify("cat /etc/fstab | sed -i 's/a/b/' /etc/fstab").is_read


def test_read_subcommand_discipline_is_positional(gate: CommandGate) -> None:
    """A unit *named* like a read subcommand must not launder a write.

    `systemctl start status` has a read token in it, but not in the verb position.
    """
    assert gate.classify("systemctl status ledger").is_read
    assert not gate.classify("systemctl start status").is_read
    assert not gate.classify("ip link set eth0 down").is_read


def test_refusals_explain_themselves(gate: CommandGate) -> None:
    """The reason is journaled and shown to the model, so it has to be actionable."""
    decision = gate.classify("systemctl restart ledger.service")
    assert "propose_remediation" in decision.reason

    decision = gate.classify("cat /etc/fstab > /tmp/x")
    assert "Redirection" in decision.reason

    decision = gate.classify("frobnicate")
    assert "not in the classified command catalog" in decision.reason


def test_sudo_is_transparent_but_recorded(gate: CommandGate) -> None:
    decision = gate.classify("sudo dmidecode -t memory")
    assert decision.is_read
    assert decision.requires_sudo


def test_catalog_can_be_extended_without_touching_code(tmp_path) -> None:
    """The catalog is data. Adding a read-only binary is a JSON edit."""
    from triage.agent.catalog import Catalog

    assert CommandGate().classify("hdparm -I /dev/sda").classification is Classification.UNKNOWN

    override = tmp_path / "extra.json"
    override.write_text(
        '{"version": 1, "commands": [{"name": "hdparm", "classification": "READ",'
        ' "summary": "Drive identification.", "write_flags": ["-W", "-B", "--security-erase"]}]}'
    )
    gate = CommandGate(Catalog.load(override))
    assert gate.classify("hdparm -I /dev/sda").is_read
    assert not gate.classify("hdparm --security-erase NULL /dev/sda").is_read
