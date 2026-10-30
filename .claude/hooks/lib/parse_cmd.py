#!/usr/bin/env python3
"""Parse a Bash command line for the Claude hooks.

usage: parse_cmd.py <commit|pr|push> [cwd]   (command on stdin)
Prints {"match", "violation", "items"}. For commit/push, `violation` is true
when the target command shares the command line with anything but a short
whitelist of read-only git commands (strict isolation). Never raises:
unparseable input yields {"match": false}, and fields that cannot be
determined are null (hooks allow).
"""
import json
import os
import re
import shlex
import sys

OPS = {"&&", "||", ";", "|", "&", "(", ")", "\n"}
HEREDOC_SUBST = re.compile(
    r"\$\(\s*cat\s*<<-?\s*(['\"]?)(\w+)\1[ \t]*\n(.*?)\n[ \t]*\2[ \t]*(?:\n\s*)?\)",
    re.DOTALL,
)
HEREDOC_STDIN = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?:\n|$)", re.DOTALL
)
GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def split_newlines(cmd):
    """Quote-aware pre-pass for the shell tokenizer.

    Turns unquoted newlines into `;` (shlex treats them as spaces) and drops
    comments (`#` at the start of a word, outside quotes, to end of line) so an
    apostrophe in a comment cannot open a quote. An unterminated quote raises
    ValueError.
    """
    out, quote, i = [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(cmd):
                out.append(c + cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            out.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            # escaped char (backslash-newline is a line continuation)
            out.append(" " if cmd[i + 1] == "\n" else c + cmd[i + 1])
            i += 2
            continue
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (not out or out[-1].isspace() or out[-1] in ";&|()"):
            while i < len(cmd) and cmd[i] != "\n":  # comment: keep the newline
                i += 1
            continue
        elif c == "\n":
            out.append(" ; ")
        else:
            out.append(c)
        i += 1
    if quote:
        raise ValueError("unterminated quote")
    return "".join(out)


def tokenize(cmd):
    cmd = split_newlines(cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def segments(tokens):
    seg = []
    for t in tokens:
        if t in OPS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def git_parts(seg, cwd):
    """Describe a git command segment: (subcommand, args, effective dir, tainted).

    Repeated `-C <path>` compose like git (each relative path is taken relative
    to the previous one). `tainted` is True for an env-var prefix
    (GIT_DIR=...), --git-dir/--work-tree, or -c/--config-env, which retarget
    the repo or its config invisibly.
    Returns None if the segment is not a git command.
    """
    i, tainted = 0, False
    while i < len(seg) and re.match(r"^\w+=", seg[i]):  # env assignments
        i += 1
        tainted = True
    if i >= len(seg) or os.path.basename(seg[i]) != "git":
        return None
    i += 1
    cur = cwd
    while i < len(seg) and seg[i].startswith("-"):
        t = seg[i]
        if t == "-C" and i + 1 < len(seg):
            cur = os.path.normpath(os.path.join(cur, seg[i + 1]))
        if t in ("--git-dir", "--work-tree") or t.startswith(("--git-dir=", "--work-tree=")):
            tainted = True
        # `-c k=v` / `--config-env` change config the gate reads (remote.*.push, ...).
        if t == "-c" or t.startswith(("-c", "--config-env")):
            tainted = True
        i += 2 if t in GIT_OPTS_WITH_ARG else 1
    if i >= len(seg):
        return "", [], cur, tainted
    return seg[i], seg[i + 1 :], cur, tainted


def prepare(cmd):
    """Replace heredocs so the shell tokenizer can cope with them."""
    bodies = {}

    def sub(m):
        key = "__HEREDOC_%d__" % len(bodies)
        bodies[key] = m.group(3)
        return key

    cmd = HEREDOC_SUBST.sub(sub, cmd)
    stdin_body = None
    m = HEREDOC_STDIN.search(cmd)
    if m:
        stdin_body = m.group(4)
        # Keep the rest of the `<<EOF` line (`<<EOF && git push`) and a newline,
        # so a command after the heredoc stays its own segment.
        cmd = cmd[: m.start()] + m.group(3) + "\n" + cmd[m.end() :]
        if HEREDOC_STDIN.search(cmd):
            raise ValueError("more than one stdin heredoc")
    return cmd, bodies, stdin_body


def restore(text, bodies):
    for key, body in bodies.items():
        text = text.replace(key, body)
    return text


# git commit short options that take a value (git commit -h).
COMMIT_SHORT_VALUE = set("mFCct")


def parse_commit(args, cwd, bodies, stdin_body):
    """Return the commit message, or None if it cannot be determined."""
    msgs, file_arg, reuse = [], None, False
    i = 0
    while i < len(args):
        t = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if t in ("-m", "--message"):
            if nxt is not None:
                msgs.append(nxt)
            i += 2
            continue
        if t.startswith("--message="):
            msgs.append(t.split("=", 1)[1])
        elif t in ("-F", "--file"):
            file_arg = nxt
            i += 2
            continue
        elif t.startswith("--file="):
            file_arg = t.split("=", 1)[1]
        elif t in ("-C", "-c", "--reuse-message", "--reedit-message"):
            reuse = True
            i += 2
            continue
        elif t.startswith(("--fixup", "--squash", "--reuse-message=", "--reedit-message=")):
            reuse = True
        elif t.startswith("-") and not t.startswith("--") and len(t) > 1:
            # Short-option cluster (-am, -aminvalid, -vFmsg): the first
            # value-taking letter takes the rest of the token, or the next arg.
            for j, ch in enumerate(t[1:], start=1):
                if ch in COMMIT_SHORT_VALUE:
                    value = t[j + 1 :] or nxt
                    if ch == "m" and value is not None:
                        msgs.append(value)
                    elif ch == "F":
                        file_arg = value
                    elif ch in "Cc":
                        reuse = True
                    if not t[j + 1 :]:
                        i += 1  # the value was the next argument
                    break
        i += 1
    if reuse:
        return None
    if msgs:
        return restore("\n\n".join(msgs), bodies)
    if file_arg:
        if file_arg == "-":
            text = stdin_body
        else:
            path = file_arg if os.path.isabs(file_arg) else os.path.join(cwd, file_arg)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                return None
        if text is None:
            return None
        return "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    return None


# `gh pr create` flags (gh pr create --help), so a flag's value is never read
# as a flag (`--body "--base=dev"`). Anything else is refused (fail closed).
PR_GUARDED = {"--title": "title", "-t": "title", "--base": "base", "-B": "base",
              "--head": "head", "-H": "head"}
PR_VALUE_FLAGS = set(PR_GUARDED) | {
    "--body", "-b", "--body-file", "-F", "--assignee", "-a", "--label", "-l",
    "--milestone", "-m", "--project", "-p", "--reviewer", "-r", "--template", "-T",
    "--recover", "--repo", "-R",
}
PR_BOOL_FLAGS = {"--draft", "-d", "--fill", "-f", "--fill-first", "--fill-verbose",
                 "--web", "-w", "--editor", "-e", "--dry-run", "--no-maintainer-edit"}
PR_BOOL_LETTERS = set("dfwe")


def parse_pr(args):
    out = {"title": None, "base": None, "head": None, "fill": False}
    i = 0
    while i < len(args):
        t = args[i]
        if t == "--":
            raise ValueError("positional arguments after -- in gh pr create")
        if t in PR_VALUE_FLAGS:
            if i + 1 >= len(args):
                raise ValueError("missing value for " + t)
            if t in PR_GUARDED:
                out[PR_GUARDED[t]] = args[i + 1]
            i += 2
            continue
        name, eq, value = t.partition("=")
        if t.startswith("--") and eq and name in PR_VALUE_FLAGS:
            if name in PR_GUARDED:
                out[PR_GUARDED[name]] = value
        elif not t.startswith("--") and len(t) > 2 and t[:2] in PR_VALUE_FLAGS:
            if t[:2] in PR_GUARDED:  # attached short value: -tvalue
                out[PR_GUARDED[t[:2]]] = t[2:]
        elif t in PR_BOOL_FLAGS:
            out["fill"] = out["fill"] or t.startswith(("--fill", "-f"))
        elif re.match(r"^-[a-zA-Z]{2,}$", t) and set(t[1:]) <= PR_BOOL_LETTERS:
            out["fill"] = out["fill"] or "f" in t
        else:
            raise ValueError("unrecognised gh pr create argument: " + t)
        i += 1
    return out


# gh flags that may precede the subcommand (`gh -R owner/repo pr create`).
GH_VALUE_FLAGS = {"-R", "--repo"}


def gh_pr_create_args(seg):
    """Args of a `gh [-R x] pr [-R x] create|new ...` segment, or None if it isn't one.

    An unknown flag before `create` raises, so main() fails closed.
    """
    start = 0
    while start < len(seg) and re.match(r"^\w+=", seg[start]):  # GH_TOKEN=x gh ...
        start += 1
    seg = seg[start:]
    if not seg:
        return None
    # env/command/bash -c/eval ... hiding a gh pr create: refuse (fail closed).
    if os.path.basename(seg[0]) in WRAPPERS and re.search(
        r"\bgh\b.*\bpr\b.*\b(create|new)\b", " ".join(seg), re.S
    ):
        raise ValueError("gh pr create behind a wrapper: " + seg[0])
    if os.path.basename(seg[0]) != "gh":
        return None
    words, inherited, i = [], [], 1
    while i < len(seg) and len(words) < 2:
        t = seg[i]
        if t in GH_VALUE_FLAGS:
            inherited += seg[i:i + 2]
            i += 2
        elif t.startswith("--repo=") or (t.startswith("-R") and len(t) > 2):
            inherited.append(t)
            i += 1
        elif t.startswith("-"):
            if "pr" in seg and ("create" in seg or "new" in seg):
                raise ValueError("unrecognised gh flag before the subcommand: " + t)
            return None
        else:
            words.append(t)
            i += 1
    return inherited + seg[i:] if words in (["pr", "create"], ["pr", "new"]) else None


# Whitelist of `git push` flags; anything else is refused (fail closed).
PUSH_FLAGS = {"-u", "--set-upstream", "--force-if-includes", "-v", "--verbose", "-q",
              "--quiet", "-n", "--dry-run", "--progress", "--no-progress"}
PUSH_FLAG_LETTERS = set("uvqn")


def push_flag_allowed(t):
    if t in PUSH_FLAGS or t in ("-d", "--delete"):
        return True
    if t == "--force-with-lease" or t.startswith("--force-with-lease="):
        return True
    return bool(re.match(r"^-[a-zA-Z]{2,}$", t)) and set(t[1:]) <= PUSH_FLAG_LETTERS


# Strict isolation: a commit/push may only share its command line with these.
ALLOWED_BEFORE = {"add", "status", "diff", "log"}
ALLOWED_AFTER = {"status", "log"}
WRAPPERS = {"bash", "sh", "zsh", "dash", "eval", "source", ".", "env", "xargs",
            "command", "exec", "time", "sudo", "nohup", "watch"}
HIDDEN_TARGET = re.compile(r"\bgit\b.*\b(commit|push)\b", re.S)


def parse_push(args):
    """Return {"delete": bool, "srcs": [...], "all": bool} for `git push` args."""
    delete, all_refs, positional, bad_flag = False, False, [], None
    for t in args:
        if t in ("--delete", "-d"):
            delete = True
        elif t.startswith("-") and t != "-":
            if not push_flag_allowed(t) and bad_flag is None:
                bad_flag = t
        else:
            positional.append(t)
    srcs, deletes = [], 0
    for spec in positional[1:]:  # positional[0] is the remote
        spec = spec.lstrip("+")
        if ":" in spec:
            src, dst = spec.split(":", 1)
            if src == "" and dst != "":  # ":branch" deletes the remote branch
                deletes += 1
                continue
            if src == "":  # bare ":" / "+:" = push all matching branches
                all_refs = True
                continue
            srcs.append(src)
        else:
            srcs.append(spec)
    refspecs = positional[1:]
    if delete:
        srcs = []
    only_deletes = bool(refspecs) and deletes == len(refspecs) and not all_refs
    return {
        "delete": delete or only_deletes,
        "srcs": srcs,
        "all": all_refs,
        "remote": positional[0] if positional else None,
        "has_refspec": bool(refspecs),
        "bad_flag": bad_flag,
    }


def looks_relevant(mode, raw):
    """Does the raw text mention the guarded command as a word?"""
    if mode == "pr":
        return bool(re.search(r"\bpr\s(.*\s)?(create|new)\b|create_pull_request", raw, re.S))
    return bool(re.search(r"\b%s\b" % ("commit" if mode == "commit" else "push"), raw))


def main():
    mode = sys.argv[1]
    cwd = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
    raw = sys.stdin.read()
    try:
        result = run(mode, cwd, raw)
    except Exception:  # fail closed: a parse failure must never mean allow
        if not looks_relevant(mode, raw):
            result = {"match": False, "violation": False, "items": []}
        else:
            result = {"match": True, "violation": True, "unparseable": True, "items": []}
    print(json.dumps(result))


def run(mode, cwd, raw):
    cmd, bodies, stdin_body = prepare(raw)
    tokens = tokenize(cmd)
    segs = list(segments(tokens))

    if mode == "pr":
        items = []
        for seg in segs:
            args = gh_pr_create_args(seg)
            if args is not None:
                res = parse_pr(args)
                for k in ("title", "base", "head"):
                    if res[k] is not None:
                        res[k] = restore(res[k], bodies)
                items.append(res)
        return {"match": bool(items), "violation": False, "items": items}

    target = "commit" if mode == "commit" else "push"
    items = []
    violation = False
    seen_target = False
    for seg in segs:
        parts = git_parts(seg, cwd)
        if parts is None:
            first = next((t for t in seg if not re.match(r"^\w+=", t)), "")
            # Check each token (bash -c "git push") and the argv as a whole
            # (command git push, env git commit).
            if os.path.basename(first) in WRAPPERS and (
                any(HIDDEN_TARGET.search(t) for t in seg)
                or HIDDEN_TARGET.search(" ".join(seg))
            ):
                items.append({"hidden": True})  # git hidden behind bash -c, eval, ...
                seen_target = True
            else:
                items.append({"other": True})
            continue
        sub, args, eff, tainted = parts
        if tainted:
            items.append({"tainted": True, "sub": sub})
        if sub == target:
            if mode == "commit":
                msg = parse_commit(args, eff, bodies, stdin_body)
                items.append({"target": True, "message": msg, "dir": eff})
            else:
                items.append({"target": True, "dir": eff, **parse_push(args)})
            seen_target = True
        elif sub in (ALLOWED_AFTER if seen_target else ALLOWED_BEFORE):
            continue
        else:
            items.append({"other": True, "sub": sub})

    targets = [it for it in items if it.get("target")]
    others = [it for it in items if not it.get("target")]
    has_parens = any(t in ("(", ")") for t in tokens)
    match = bool(targets) or any(it.get("hidden") for it in items)
    violation = bool(match and (others or len(targets) > 1 or has_parens))
    return {"match": match, "violation": violation, "items": targets}


if __name__ == "__main__":
    main()
