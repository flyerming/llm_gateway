#!/usr/bin/env python3
"""Helper for the mihomo proxy shell scripts.

Reads the payload of `GET /proxies` from stdin and prints node information in a
locale-safe way. Node names contain emoji and Chinese characters, so a plain
`print()` crashes with UnicodeEncodeError when the server shell runs under the
C/POSIX locale. Every stream here is forced to UTF-8 with an escaping fallback,
so the node list is always visible.

Commands:
  list <group>                      numbered options of one proxy group
  list-all                          every proxy group and every single node
  now <group>                       current node of one proxy group
  groups                            names of all proxy groups
  resolve <group> <query> <bodyfile>  resolve index/name/substring, write PUT body
  urlencode <text>                  percent-encode text for the controller path

Set MIHOMO_ASCII=1 to escape non-ASCII characters, useful for terminals that
cannot render emoji.
"""

import json
import os
import sys
from urllib.parse import quote


def _force_utf8(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        return stream
    except AttributeError:  # Python < 3.7
        import io

        return io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                errors="backslashreplace", line_buffering=True)


sys.stdout = _force_utf8(sys.stdout)
sys.stderr = _force_utf8(sys.stderr)

ASCII_ONLY = os.environ.get("MIHOMO_ASCII", "") not in ("", "0", "false", "no")


def die(message, code=1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def fix_arg(value):
    """Re-decode argv, which may carry raw UTF-8 bytes under the C locale."""
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def show(name):
    if ASCII_ONLY:
        return name.encode("ascii", "backslashreplace").decode("ascii")
    return name


def load_proxies():
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        die("empty response from the mihomo controller")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        die("controller response is not valid JSON: %s" % raw[:200])
    proxies = payload.get("proxies")
    if not isinstance(proxies, dict):
        die("controller response has no 'proxies' map, got: %s" % sorted(payload)[:5])
    return proxies


def is_group(entry):
    return isinstance(entry, dict) and isinstance(entry.get("all"), list)


def delay_text(entry):
    if not isinstance(entry, dict):
        return ""
    history = entry.get("history") or []
    if not history:
        return ""
    delay = history[-1].get("delay")
    if not delay:
        return "  [timeout]"
    return "  [%d ms]" % delay


def group_names(proxies):
    return [name for name, entry in proxies.items() if is_group(entry)]


def get_group(proxies, name):
    entry = proxies.get(name)
    if is_group(entry):
        return entry
    print("proxy group not found: %s" % show(name), file=sys.stderr)
    print("", file=sys.stderr)
    print("available groups:", file=sys.stderr)
    for candidate in group_names(proxies):
        print("  - %s" % show(candidate), file=sys.stderr)
    print("", file=sys.stderr)
    print("select another group with: -g \"group name\"", file=sys.stderr)
    raise SystemExit(3)


def print_group(proxies, name, entry, indent=""):
    now = entry.get("now") or ""
    print("%sgroup: %s  [type=%s, nodes=%d]"
          % (indent, show(name), entry.get("type", "?"), len(entry["all"])))
    if now:
        print("%scurrent: %s" % (indent, show(now)))
    for index, member in enumerate(entry["all"], 1):
        mark = "*" if member == now else " "
        print("%s%4d %s %s%s"
              % (indent, index, mark, show(member), delay_text(proxies.get(member))))


def cmd_list(argv):
    if len(argv) != 1:
        die("usage: proxy-nodes.py list <group>", 2)
    proxies = load_proxies()
    name = fix_arg(argv[0])
    print_group(proxies, name, get_group(proxies, name), indent="  ")
    return 0


def cmd_list_all(argv):
    if argv:
        die("usage: proxy-nodes.py list-all", 2)
    proxies = load_proxies()
    groups = group_names(proxies)
    print("=== Proxy Groups (%d) ===" % len(groups))
    for name in groups:
        print("")
        print_group(proxies, name, proxies[name], indent="  ")
    singles = [name for name, entry in proxies.items() if not is_group(entry)]
    print("")
    print("=== Single Nodes (%d) ===" % len(singles))
    for index, name in enumerate(singles, 1):
        entry = proxies[name]
        node_type = entry.get("type", "?") if isinstance(entry, dict) else "?"
        print("  %4d   %s  [%s]%s"
              % (index, show(name), node_type, delay_text(entry)))
    return 0


def cmd_now(argv):
    if len(argv) != 1:
        die("usage: proxy-nodes.py now <group>", 2)
    proxies = load_proxies()
    name = fix_arg(argv[0])
    entry = get_group(proxies, name)
    print(show(entry.get("now") or "unknown"))
    return 0


def cmd_groups(argv):
    if argv:
        die("usage: proxy-nodes.py groups", 2)
    proxies = load_proxies()
    for name in group_names(proxies):
        print(show(name))
    return 0


def match_query(members, query):
    """Resolve an index, an exact name, or a unique substring."""
    if query.isdigit():
        index = int(query)
        if 1 <= index <= len(members):
            return members[index - 1], []
        die("index out of range: %s (group has %d nodes)" % (query, len(members)), 2)
    if query in members:
        return query, []
    folded = query.casefold()
    exact = [m for m in members if m.casefold() == folded]
    if len(exact) == 1:
        return exact[0], []
    partial = []
    for member in members:
        if folded in member.casefold() and member not in partial:
            partial.append(member)
    if len(partial) == 1:
        return partial[0], []
    return None, partial


def cmd_resolve(argv):
    if len(argv) != 3:
        die("usage: proxy-nodes.py resolve <group> <query> <body_file>", 2)
    proxies = load_proxies()
    group_name = fix_arg(argv[0])
    query = fix_arg(argv[1])
    body_file = argv[2]
    entry = get_group(proxies, group_name)
    members = entry["all"]
    resolved, partial = match_query(members, query)
    if resolved is None:
        if partial:
            print("ambiguous node name: %s" % show(query), file=sys.stderr)
            print("matching nodes:", file=sys.stderr)
        else:
            print("no node matches: %s" % show(query), file=sys.stderr)
            print("available nodes:", file=sys.stderr)
            partial = members
        for index, member in enumerate(members, 1):
            if member in partial:
                print("  %4d   %s" % (index, show(member)), file=sys.stderr)
        print("", file=sys.stderr)
        print("tip: pass the leading index number instead of the full name",
              file=sys.stderr)
        raise SystemExit(2)
    with open(body_file, "wb") as handle:
        handle.write(json.dumps({"name": resolved}, ensure_ascii=False).encode("utf-8"))
    print(resolved)
    return 0


def cmd_urlencode(argv):
    if len(argv) != 1:
        die("usage: proxy-nodes.py urlencode <text>", 2)
    print(quote(fix_arg(argv[0]), safe=""))
    return 0


COMMANDS = {
    "list": cmd_list,
    "list-all": cmd_list_all,
    "now": cmd_now,
    "groups": cmd_groups,
    "resolve": cmd_resolve,
    "urlencode": cmd_urlencode,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        die("usage: proxy-nodes.py {%s} [args]" % "|".join(COMMANDS), 2)
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
