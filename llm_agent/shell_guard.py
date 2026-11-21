"""Block-list check for commands the assistant wants to run in its chat environment.

This is a SAFETY NET, not a sandbox. It stops mistakes (rm -rf /, killing the server, reading outside the project) and the obvious abuse
(reverse shells, privilege escalation, download-and-run). It cannot stop a program the assistant writes and then runs: real isolation is
the operating-system layer in shell.py (bubblewrap). Keep both.
"""
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional

SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh"}
INTERPRETERS = SHELLS | {"python", "python3", "node", "perl", "ruby", "php", "lua"}
WRAPPERS = {"env", "nice", "time", "timeout", "stdbuf", "exec", "command", "builtin", "xargs", "ionice"}

DENY = {
    # privilege / system
    "sudo": "needs administrator rights", "su": "needs administrator rights", "doas": "needs administrator rights", "pkexec": "needs administrator rights",
    "chroot": "changes the system", "nsenter": "escapes the environment", "unshare": "escapes the environment", "setpriv": "changes privileges",
    "mount": "changes the system", "umount": "changes the system", "losetup": "changes the system", "mknod": "changes the system",
    "dd": "can overwrite disks", "mkfs": "can erase disks", "fdisk": "can erase disks", "parted": "can erase disks", "shutdown": "stops the server",
    "reboot": "stops the server", "halt": "stops the server", "poweroff": "stops the server", "init": "stops the server", "telinit": "stops the server",
    "systemctl": "controls the server", "service": "controls the server", "journalctl": "reads server logs", "crontab": "schedules jobs outside the chat",
    "at": "schedules jobs outside the chat", "batch": "schedules jobs outside the chat", "useradd": "changes accounts", "userdel": "changes accounts",
    "usermod": "changes accounts", "groupadd": "changes accounts", "passwd": "changes accounts", "chpasswd": "changes accounts", "visudo": "changes privileges",
    "chown": "changes ownership", "chgrp": "changes ownership", "setcap": "changes privileges",
    "apt": "installs system software", "apt-get": "installs system software", "dpkg": "installs system software", "yum": "installs system software",
    "dnf": "installs system software", "apk": "installs system software", "snap": "installs system software", "brew": "installs system software",
    # network abuse / remote access
    "nc": "opens raw network connections", "ncat": "opens raw network connections", "netcat": "opens raw network connections", "socat": "opens raw network connections",
    "telnet": "opens raw network connections", "ssh": "connects to other machines", "scp": "connects to other machines", "sftp": "connects to other machines",
    "ftp": "connects to other machines", "rsync": "connects to other machines", "nmap": "scans networks", "tcpdump": "sniffs the network",
    "iptables": "changes the firewall", "ip6tables": "changes the firewall", "nft": "changes the firewall", "ip": "changes the network",
    "ifconfig": "changes the network", "route": "changes the network", "tc": "changes the network",
    # other people's processes and the server itself
    "kill": "could stop the server (use stop_app to stop your app)", "killall": "could stop the server (use stop_app)", "pkill": "could stop the server (use stop_app)",
    "ps": "shows other people's processes", "top": "shows other people's processes", "htop": "shows other people's processes",
    "lsof": "shows other people's files", "netstat": "shows other people's connections", "ss": "shows other people's connections",
    "strace": "inspects other processes", "ltrace": "inspects other processes", "gdb": "inspects other processes",
    # containers
    "docker": "container tools are not available on this server", "podman": "container tools are not available", "kubectl": "container tools are not available",
}
DENY_PREFIX = {"mkfs.": "can erase disks"}
SCRIPT_RUNNERS = SHELLS | {"python", "python3", "node", "perl", "ruby", "php"}
NETWORK_FETCH = {"curl", "wget"}
PRIVATE_NET = re.compile(r"(^|[/@\s])(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0|\[?::1?\]?)([:/\s]|$)|metadata\.google|metadata\.azure", re.I)
READ_ONLY_ROOTS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/opt", "/etc/ssl", "/etc/resolv.conf", "/etc/alternatives", "/etc/mime.types", "/dev/null", "/dev/zero",
                   "/dev/urandom", "/dev/random", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/etc/localtime", "/etc/ld.so.cache")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class Blocked(Exception):
    """The command was refused. str(e) is a sentence the assistant can act on."""


def _tokens(command: str) -> List[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lex.whitespace_split = True
    lex.commenters = ""
    lex.whitespace = " \t\r"
    try:
        return list(lex)
    except ValueError as e:
        raise Blocked(f"The command could not be read ({e}). Check the quotes.")


def _split(tokens: Iterable[str]) -> List[List[str]]:
    out, cur = [], []
    for t in tokens:
        if t in SEPARATORS or (t and set(t) <= set(";&|()\n") and t not in (">", "<")):
            if cur:
                out.append(cur)
            cur = []
            if t in ("|", "|&"):
                out.append(["|"])  # keep pipes visible to the download-and-run check
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


class Guard:
    def __init__(self, roots: Iterable[Path], home: Path, tmp: Path):
        """roots: folders the command may read and write (the user's project area, the chat environment)."""
        self.roots = [Path(r).resolve() for r in roots]
        self.home, self.tmp = Path(home).resolve(), Path(tmp).resolve()
        self.roots += [self.home, self.tmp]

    # ---- paths -----------------------------------------------------------------------------------
    def _path_ok(self, token: str, cwd: Path, write: bool = True) -> Optional[str]:
        if token in ("", "-"):
            return None
        if "~" in token[:1]:
            if token != "~" and not token.startswith("~/"):
                return f"“{token}” points at another account's folder"
            token = str(self.home) + token[1:]
        token = token.replace("${HOME}", str(self.home)).replace("$HOME", str(self.home)).replace("${TMPDIR}", str(self.tmp)).replace("$TMPDIR", str(self.tmp))
        if ".." in token.replace("\\", "/").split("/"):
            return f"“{token}” goes up out of the project folder (..)"
        if "$" in token or "`" in token:
            return None if not token.startswith("/") else f"“{token}” builds a path from a variable outside the project"
        if not token.startswith("/"):
            return None
        p = Path(os.path.normpath(token))
        if any(p == r or r in p.parents for r in self.roots):
            return None
        if not write and any(str(p) == r or str(p).startswith(r + "/") for r in READ_ONLY_ROOTS):
            return None
        return f"“{token}” is outside your project folder"

    # ---- the check -------------------------------------------------------------------------------
    def check(self, command: str, cwd: Path, depth: int = 0) -> None:
        if not isinstance(command, str) or not command.strip():
            raise Blocked("The command is empty.")
        if len(command) > 8000:
            raise Blocked("The command is too long. Put long scripts in a file with write_file and run the file.")
        if "\x00" in command:
            raise Blocked("The command contains an invalid character.")
        if re.search(r":\s*\(\s*\)\s*\{", command):
            raise Blocked("That looks like a fork bomb.")
        if re.search(r"/dev/(tcp|udp)/", command):
            raise Blocked("Opening raw network sockets from the shell is not allowed.")
        stripped = HEREDOC.sub("<<EOF_BODY", command)
        segments = _split(_tokens(stripped))
        prev_fetch = False
        for seg in segments:
            if seg == ["|"]:
                continue
            prog, args = self._unwrap(seg)
            if prog is None:
                continue
            self._program(prog, args, cwd, prev_fetch, depth)
            prev_fetch = prog in NETWORK_FETCH or prog in {"base64", "xxd", "openssl"}
            # a pipe after a fetch-like program feeding an interpreter is caught in _program via prev_fetch
        # redirect targets and every path-like word
        toks = _tokens(stripped)
        for i, t in enumerate(toks):
            if t in (">", ">>", ">&", "<", "<>") and i + 1 < len(toks):
                bad = self._path_ok(toks[i + 1], cwd, write=t != "<")
                if bad:
                    raise Blocked(f"Redirect refused: {bad}.")

    def _unwrap(self, seg: List[str]):
        i = 0
        while i < len(seg) and (ASSIGN.match(seg[i]) or seg[i] in ("!", "{", "}", "then", "do", "else", "if", "while", "until", "for", "in", "done", "fi", "elif", "case", "esac")):
            i += 1
        while i < len(seg) and os.path.basename(seg[i]) in WRAPPERS:
            w = os.path.basename(seg[i])
            i += 1
            while i < len(seg) and (seg[i].startswith("-") or ASSIGN.match(seg[i]) or (w == "timeout" and re.match(r"^\d+[smhd]?$", seg[i])) or (w == "nice" and re.match(r"^-?\d+$", seg[i]))):
                i += 1
        if i >= len(seg):
            return None, []
        return os.path.basename(seg[i]), seg[i + 1:]

    def _program(self, prog: str, args: List[str], cwd: Path, after_fetch: bool, depth: int) -> None:
        name = prog.lower()
        if name in DENY:
            raise Blocked(f"`{prog}` is not allowed here ({DENY[name]}).")
        for pre, why in DENY_PREFIX.items():
            if name.startswith(pre):
                raise Blocked(f"`{prog}` is not allowed here ({why}).")
        if name in ("nohup", "setsid", "disown", "screen", "tmux", "daemonize"):
            raise Blocked("Background processes started from the shell are not tracked. Use the background option of this tool to run a server, then stop_app.")
        if name in INTERPRETERS and after_fetch:
            raise Blocked("Piping downloaded or decoded data straight into an interpreter is not allowed. Save it to a file, read it, then run it.")
        if name in NETWORK_FETCH:
            joined = " ".join(args)
            if re.search(r"file://|ftp://|gopher://", joined, re.I):
                raise Blocked("Only http(s) addresses may be fetched.")
            if PRIVATE_NET.search(joined) and not re.search(r"(localhost|127\.0\.0\.1)", joined):
                raise Blocked("Fetching private-network or cloud-metadata addresses is not allowed.")
        if name in ("eval",) or (name in SHELLS and "-c" in args):
            idx = args.index("-c") + 1 if "-c" in args else 0
            inner = args[idx] if idx < len(args) else " ".join(args)
            if depth >= 3:
                raise Blocked("Commands are nested too deeply.")
            self.check(inner, cwd, depth + 1)
        if name == "rm":
            flags = "".join(a for a in args if a.startswith("-") and not a.startswith("--"))
            recursive = "r" in flags or "R" in flags or "--recursive" in args
            targets = [a for a in args if not a.startswith("-")]
            if recursive and any(t in (".", "./", "*", "./*", "..", "/", "~", "~/") or t.rstrip("/") in ("", ".") for t in targets) and cwd.resolve() in [r for r in self.roots[:-2]]:
                raise Blocked("That would delete everything in your project. Name the files or folders to remove.")
        if name in ("chmod",):
            if any(re.match(r"^[0-7]*[2367][0-7]{2}$|^[ugoa]*\+[rwx]*s", a) for a in args if not a.startswith("-")) and any(re.match(r"^[0-7]{4}$|\+s", a) for a in args):
                raise Blocked("Setting special permission bits is not allowed.")
        for a in args:
            if a.startswith("-") and "=" in a:
                a = a.split("=", 1)[1]
            elif a.startswith("-"):
                continue
            if re.match(r"^[a-z]+://", a):
                continue
            bad = self._path_ok(a, cwd, write=name not in ("cat", "ls", "head", "tail", "grep", "find", "wc", "less", "file", "stat", "diff", "sort", "python", "python3", "node", "pip", "pip3"))
            if bad:
                raise Blocked(f"{bad[0].upper() + bad[1:]}. You can only work inside your project folder and the home folder (~) of this chat.")
