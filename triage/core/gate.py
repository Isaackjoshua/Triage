"""CommandGate — the safety classifier that enforces the read/touch split.

Every command the agent asks for passes through here before any transport sees it.
The gate answers one question: *does running this change the machine?*

Two rules make it trustworthy:

* **It fails safe.** Anything the gate cannot confidently prove is read-only comes back
  ``UNKNOWN``, and ``UNKNOWN`` is handled exactly as ``WRITE``: refused on the read path,
  routed to human approval on the write path. There is no "parse and hope" branch.
* **It classifies the whole command line, not just the binary.** Redirection, command
  substitution, chaining, and shell builtins are rejected structurally, so a read-only
  binary cannot be used as a vehicle for a write (``cat x > /etc/fstab``).

The one shell construct allowed through is a pipeline, and only when *every* segment is
independently READ — ``dmesg | grep -i error`` is genuinely a read; ``dmesg | tee f`` is not.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from ..agent.catalog import ALLOWED_BIN_DIRS, Catalog, CommandEntry, default_catalog
from .models import Classification

#: Shell operators the lexer may emit. Only the pipe is ever permitted.
_PIPE = "|"
_REDIRECTION = {">", ">>", "<", "<<", ">&", "<&", "|&"}
_CHAINING = {";", "&&", "||", "&", "(", ")"}

#: Substrings that make a command line unclassifiable no matter how it tokenizes.
_SUBSTITUTION_MARKERS = ("$(", "`", "${", "<(", ">(")

#: sudo flags that do not change which command ultimately runs.
_SAFE_SUDO_FLAGS = {"-n", "--non-interactive", "-H", "--set-home", "-E", "--preserve-env"}


@dataclass
class GateDecision:
    """The gate's verdict, with the reason attached.

    The reason is not decoration: it is journaled, and it is what the model is shown
    when a command is refused, so it has to be specific enough to act on.
    """

    classification: Classification
    reason: str
    command: str
    matched: list[str] = field(default_factory=list)
    requires_sudo: bool = False

    @property
    def is_read(self) -> bool:
        return self.classification is Classification.READ

    @property
    def needs_approval(self) -> bool:
        """WRITE and UNKNOWN are handled identically. This is the fail-safe rule."""
        return not self.is_read


class CommandGate:
    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog or default_catalog()

    # ---------------------------------------------------------------- public API

    def classify(self, command: str) -> GateDecision:
        text = (command or "").strip()
        if not text:
            return self._unknown(command, "The command is empty.")

        for marker in _SUBSTITUTION_MARKERS:
            if marker in text:
                return self._unknown(
                    text,
                    f"Command substitution or process substitution ({marker}) makes the "
                    "effective command impossible to classify. Write out the literal command.",
                )
        if "\n" in text or "\r" in text:
            return self._unknown(text, "Multi-line commands are refused. Send one command.")

        try:
            tokens = _lex(text)
        except ValueError as exc:
            return self._unknown(text, f"The command could not be parsed ({exc}).")

        if not tokens:
            return self._unknown(text, "The command is empty.")

        segments: list[list[str]] = [[]]
        for token in tokens:
            if token in _REDIRECTION:
                return GateDecision(
                    Classification.WRITE,
                    f"Redirection ({token}) writes to a file, so this is a change to the "
                    "machine. Route it through propose_remediation.",
                    text,
                    matched=[token],
                )
            if token in _CHAINING:
                return self._unknown(
                    text,
                    f"Shell chaining/grouping ({token}) is refused so that each command is "
                    "classified and journaled on its own. Send one command at a time.",
                )
            if token == _PIPE:
                segments.append([])
                continue
            segments[-1].append(token)

        if any(not segment for segment in segments):
            return self._unknown(text, "A pipeline segment is empty.")

        matched: list[str] = []
        requires_sudo = False
        for segment in segments:
            decision = self._classify_segment(segment, text)
            matched.extend(decision.matched)
            requires_sudo = requires_sudo or decision.requires_sudo
            if not decision.is_read:
                # Worst verdict in the pipeline wins, and carries its own reason.
                return GateDecision(
                    decision.classification,
                    decision.reason,
                    text,
                    matched=matched,
                    requires_sudo=requires_sudo,
                )

        return GateDecision(
            Classification.READ,
            "Every element of this command is a catalogued read-only operation.",
            text,
            matched=matched,
            requires_sudo=requires_sudo,
        )

    # ------------------------------------------------------------- segment logic

    def _classify_segment(self, segment: list[str], full: str) -> GateDecision:
        argv = list(segment)

        # Environment assignments prefixing a command (FOO=bar cmd) are refused: they
        # change how the command behaves in ways the catalog does not model.
        if "=" in argv[0] and not argv[0].startswith("-") and "/" not in argv[0].split("=")[0]:
            return self._unknown(
                full,
                f"Leading environment assignment ({argv[0]}) is refused. Send the bare command.",
            )

        requires_sudo = False
        if _basename(argv[0]) == "sudo":
            requires_sudo = True
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                if argv[0] not in _SAFE_SUDO_FLAGS:
                    return self._unknown(
                        full,
                        f"sudo option {argv[0]} changes which command runs and is refused.",
                    )
                argv = argv[1:]
            if not argv:
                return self._unknown(full, "sudo was given no command to run.")

        binary = argv[0]
        if "/" in binary:
            directory = binary.rsplit("/", 1)[0] or "/"
            if directory not in ALLOWED_BIN_DIRS:
                return self._unknown(
                    full,
                    f"{binary} is outside the standard system binary directories, so it is "
                    "not the catalogued command of that name.",
                )
        name = _basename(binary)

        entry = self.catalog.lookup(name)
        if entry is None:
            return self._unknown(
                full,
                f"'{name}' is not in the classified command catalog. Unclassified commands are "
                "treated as changes to the machine.",
            )

        return self._classify_entry(entry, argv[1:], full, requires_sudo)

    def _classify_entry(
        self, entry: CommandEntry, args: list[str], full: str, requires_sudo: bool
    ) -> GateDecision:
        def verdict(classification: Classification, reason: str, hit: str = "") -> GateDecision:
            return GateDecision(
                classification,
                reason,
                full,
                matched=[hit or entry.name],
                requires_sudo=requires_sudo or entry.requires_sudo,
            )

        # 1. Mutating flags.
        for arg in args:
            flag = _flag_name(arg)
            if flag in entry.write_flags:
                return verdict(
                    Classification.WRITE,
                    f"{entry.name} {flag} changes state. Route it through propose_remediation.",
                    f"{entry.name} {flag}",
                )

        # 2. NAME=VALUE assignments where the catalog says those are writes (sysctl).
        if entry.write_if_assignment:
            for arg in args:
                if "=" in arg and not arg.startswith("-"):
                    return verdict(
                        Classification.WRITE,
                        f"{entry.name} with an assignment ({arg}) sets a value rather than "
                        "reading one. Route it through propose_remediation.",
                        arg,
                    )

        # 3. Flags refused for reasons other than mutation (they never return).
        for arg in args:
            flag = _flag_name(arg)
            if flag in entry.blocked_flags:
                return verdict(
                    Classification.UNKNOWN,
                    f"{entry.name} {flag} follows output indefinitely and would hang the "
                    "session. Re-run it without that flag.",
                    f"{entry.name} {flag}",
                )

        positionals = [a for a in args if not a.startswith("-")][:2]

        # 4. Mutating subcommands, checked against the verb and its object.
        for positional in positionals:
            if positional in entry.write_subcommands:
                return verdict(
                    Classification.WRITE,
                    f"'{entry.name} {positional}' changes state. Route it through "
                    "propose_remediation.",
                    f"{entry.name} {positional}",
                )

        # 5. Read subcommands: the first positional is the verb.
        if entry.read_subcommands:
            if positionals:
                if positionals[0] in entry.read_subcommands:
                    return verdict(
                        Classification.READ,
                        f"'{entry.name} {positionals[0]}' is a catalogued read-only query.",
                        f"{entry.name} {positionals[0]}",
                    )
                return verdict(
                    entry.classification,
                    f"'{entry.name} {positionals[0]}' is not a catalogued read-only "
                    f"subcommand of {entry.name}, so it is treated as a change to the "
                    "machine. Route it through propose_remediation.",
                    f"{entry.name} {positionals[0]}",
                )

        # 6. Flag-only invocation with an explicitly read-only flag.
        if not positionals and args:
            for arg in args:
                if _flag_name(arg) in entry.read_flags:
                    return verdict(
                        Classification.READ,
                        f"'{entry.name} {_flag_name(arg)}' is a catalogued read-only query.",
                        f"{entry.name} {_flag_name(arg)}",
                    )

        # 7. Bare invocation.
        if not args and entry.bare_classification is not None:
            return verdict(
                entry.bare_classification,
                f"Bare '{entry.name}' reports state without changing it."
                if entry.bare_classification is Classification.READ
                else f"Bare '{entry.name}' is classified {entry.bare_classification.value}.",
            )

        # 8. The entry's default.
        if entry.classification is Classification.READ:
            return verdict(Classification.READ, f"{entry.name} is a catalogued read-only command.")
        summary = entry.summary.rstrip(". ")
        return verdict(
            entry.classification,
            f"{entry.name} is catalogued as {entry.classification.value}"
            + (f" — {summary}" if summary else "")
            + ". Route it through propose_remediation.",
        )

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _unknown(command: str, reason: str) -> GateDecision:
        return GateDecision(Classification.UNKNOWN, reason, command)


def _lex(command: str) -> list[str]:
    """Tokenize, keeping shell operators distinguishable from quoted text.

    ``punctuation_chars`` is what makes ``grep "a|b" f`` (one word) different from
    ``dmesg | grep`` (a pipeline) — without it the gate would refuse legitimate reads
    and, worse, could be talked into treating an operator as data.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _flag_name(arg: str) -> str:
    """``--vacuum-size=1G`` and ``--vacuum-size 1G`` must match the same catalog flag."""
    return arg.split("=", 1)[0] if arg.startswith("-") else arg
