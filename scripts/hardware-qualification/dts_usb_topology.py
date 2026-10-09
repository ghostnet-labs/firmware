#!/usr/bin/env python3
"""Report CP11x xHCI/COMPHY/UTMI wiring from device tree sources (GHO-65).

Read-only and offline. With dtc and cpp available, each DTS is preprocessed and
compiled the way the kernel build does; otherwise (or with --static) the source
nodes are parsed directly and nodes from unavailable include files are reported
as unset. Neither mode proves hardware routing; see cn9130-usb/README.md.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

# Linux v6.6 drivers/phy/marvell/phy-mvebu-cp110-comphy.c mvebu_comphy_cp110_modes:
# PHY_MODE_USB_HOST_SS entries as (lane, port). The port argument of a phys
# specifier selects the USB3 host the lane mux connects to; the driver cannot
# see which controller node holds the reference.
USB_HOST_SS_PORTS = {1: {0}, 2: {0}, 3: {1}, 4: {1}}

USB_LABEL = re.compile(r"^cp(\d+)_usb3_(\d+)$")
COMPHY_REF = re.compile(r"^&?cp(\d+)_comphy(\d+)(?:\s+(\S+))?$")
UTMI_REF = re.compile(r"^&?cp(\d+)_utmi(\d+)$")
NAME = r"[A-Za-z0-9,._+@#?-]+"
TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<directive>\#(?:include|define|undef|ifdef|ifndef|if|elif|else|endif|pragma|error|line)\b[^\n]*|\#\ \d+[^\n]*)
  | (?P<keyword>/dts-v1/|/plugin/|/memreserve/|/delete-node/|/delete-property/|/omit-if-no-ref/)
  | (?P<ref>&\{[^}]*\}|&[A-Za-z_][A-Za-z0-9_]*)
  | (?P<label>[A-Za-z_][A-Za-z0-9_]*:)
  | (?P<root>/(?=\s*\{))
  | (?P<name>""" + NAME + r""")
  | (?P<punct>[{};=])
""", re.S | re.X)


class ParseError(ValueError):
    pass


class Node:
    def __init__(self, name):
        self.name = name
        self.labels = []
        self.props = {}
        self.children = {}


class Tree:
    def __init__(self):
        self.root = Node("/")
        self.labels = {}
        self.orphans = {}  # &label blocks whose target came from an unavailable include

    def by_label(self, label, create=False):
        if label in self.labels:
            return self.labels[label]
        if label not in self.orphans and create:
            self.orphans[label] = Node("&" + label)
            self.orphans[label].labels.append(label)
        return self.orphans.get(label)

    def add_label(self, label, node):
        if label in self.orphans and self.orphans[label] is not node:
            merge(node, self.orphans.pop(label))
        self.labels[label] = node
        if label not in node.labels:
            node.labels.append(label)


def merge(target, source):
    target.props.update(source.props)
    for name, child in source.children.items():
        if name in target.children:
            merge(target.children[name], child)
        else:
            target.children[name] = child


def read_value(text, pos):
    """Return (raw value, index after the terminating ';')."""
    depth, quote, start = 0, False, pos
    while pos < len(text):
        char = text[pos]
        if quote:
            if char == "\\":
                pos += 1
            elif char == '"':
                quote = False
        elif char == '"':
            quote = True
        elif char in "<[(":
            depth += 1
        elif char in ">])":
            depth -= 1
        elif char == ";" and depth == 0:
            return " ".join(text[start:pos].split()), pos + 1
        pos += 1
    raise ParseError("unterminated property value")


SPACE = re.compile(r"(?:\s+|//[^\n]*|/\*.*?\*/)*", re.S)


def parse(text, tree=None, include=None):
    """Parse DTS source text into tree. include(name) returns text or None."""
    tree = tree or Tree()
    stack, labels = [], []
    pos = 0
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match:
            raise ParseError("unexpected text near: %r" % text[pos:pos + 40])
        kind, value, pos = match.lastgroup, match.group(), match.end()
        if kind == "ws":
            continue
        if kind == "directive":
            found = re.match(r'#include\s+"([^"]+)"', value)
            if found and include:
                body = include(found.group(1))
                if body is not None:
                    parse(body, tree, include)
            continue
        if kind == "keyword":
            raw, pos = read_value(text, pos)
            if value == "/delete-node/" and raw.startswith("&"):
                victim = tree.labels.pop(raw[1:], None)
                for owner in [tree.root] + list(iter_nodes(tree.root)):
                    for key, child in list(owner.children.items()):
                        if child is victim:
                            del owner.children[key]
            elif value == "/delete-node/" and stack:
                stack[-1].children.pop(raw, None)
            elif value == "/delete-property/" and stack:
                stack[-1].props.pop(raw, None)
            continue
        if kind == "label":
            labels.append(value[:-1])
            continue
        if kind == "punct" and value == "}":
            if not stack:
                raise ParseError("unbalanced '}'")
            stack.pop()
            continue
        if kind == "punct" and value == ";":
            continue
        if kind not in ("root", "ref", "name"):
            raise ParseError("unexpected %s %r" % (kind, value))
        after = SPACE.match(text, pos).end()
        nxt = text[after:after + 1]
        if nxt == "{":
            if kind == "root":
                node = tree.root
            elif value.startswith("&{"):
                node = node_by_path(tree, value[2:-1])
            elif kind == "ref":
                node = tree.by_label(value[1:], create=True)
            elif stack:
                node = stack[-1].children.setdefault(value, Node(value))
            else:
                raise ParseError("node %s outside a tree" % value)
            for label in labels:
                tree.add_label(label, node)
            labels = []
            stack.append(node)
            pos = after + 1
        elif kind == "name" and stack and nxt and nxt in "=;":
            if nxt == "=":
                raw, pos = read_value(text, after + 1)
            else:
                raw, pos = "", after + 1
            stack[-1].props[value] = raw
            labels = []
        else:
            raise ParseError("unexpected %s %r" % (kind, value))
    if stack:
        raise ParseError("unterminated node %s" % stack[-1].name)
    return tree


def node_by_path(tree, path):
    node = tree.root
    for part in [p for p in path.split("/") if p]:
        node = node.children.setdefault(part, Node(part))
    return node


def iter_nodes(node):
    for child in node.children.values():
        yield child
        yield from iter_nodes(child)


def strings(raw):
    return re.findall(r'"((?:[^"\\]|\\.)*)"', raw or "")


def cells(raw):
    """Split '<&a 0>, <&b>' into ['a 0', 'b'] with numeric cells normalised."""
    out = []
    for group in re.findall(r"<([^>]*)>", raw or ""):
        current = []
        for item in group.split():
            if item.startswith("&") and current:
                out.append(" ".join(current))
                current = []
            if re.fullmatch(r"0x[0-9a-fA-F]+", item):
                item = str(int(item, 16))
            current.append(item.lstrip("&"))
        if current:
            out.append(" ".join(current))
    return out


def describe_phy(ref):
    comphy = COMPHY_REF.match(ref)
    if comphy:
        return "comphy lane%s port%s" % (comphy.group(2), comphy.group(3) or "?")
    utmi = UTMI_REF.match(ref)
    if utmi:
        return "utmi%s" % utmi.group(2)
    return ref


def topology(tree):
    """Return a list of controller rows and routing notes, ordered by label."""
    found = {}
    for node in list(iter_nodes(tree.root)) + list(tree.orphans.values()):
        for label in node.labels:
            match = USB_LABEL.match(label)
            if match:
                found[label] = (node, match)
    rows, notes = [], []
    for label in sorted(found):
        node, match = found[label]
        controller = int(match.group(2))
        phys = cells(node.props.get("phys"))
        status = strings(node.props.get("status"))
        rows.append({
            "controller": label,
            "node": node.name if not node.name.startswith("&") else "(include not available)",
            "status": status[0] if status else "(unset)",
            "phys": [describe_phy(p) for p in phys],
            "phy_names": strings(node.props.get("phy-names")),
            "dr_mode": (strings(node.props.get("dr_mode")) or ["(unset)"])[0],
            "usb_phy": cells(node.props.get("usb-phy")),
        })
        if status and status[0] not in ("okay", "ok"):
            continue
        for ref in phys:
            comphy = COMPHY_REF.match(ref)
            if not comphy or comphy.group(3) is None or not comphy.group(3).isdigit():
                continue
            lane, port = int(comphy.group(2)), int(comphy.group(3))
            allowed = USB_HOST_SS_PORTS.get(lane, set())
            if port not in allowed:
                notes.append("%s: comphy lane%d port%d has no USB_HOST_SS entry in the v6.6 lane table"
                             % (label, lane, port))
            elif port != controller:
                notes.append("%s: comphy lane%d port%d selects the lane mux toward USB3 host %d, "
                             "but the reference is on controller %d" % (label, lane, port, port, controller))
    return rows, notes


def format_table(title, rows, notes):
    header = ("controller", "node", "status", "phys", "phy-names", "dr_mode", "usb-phy")
    data = [(r["controller"], r["node"], r["status"], ", ".join(r["phys"]) or "-",
             ", ".join(r["phy_names"]) or "-", r["dr_mode"], ", ".join(r["usb_phy"]) or "-")
            for r in rows]
    widths = [max(len(str(x)) for x in col) for col in zip(header, *data)] if data else [len(h) for h in header]
    lines = [title]
    for row in [header] + data:
        lines.append("| " + " | ".join(str(v).ljust(w) for v, w in zip(row, widths)) + " |")
        if row is header:
            lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    if not data:
        lines.append("(no cpN_usb3_N controllers found)")
    lines.extend("note: " + n for n in notes)
    return "\n".join(lines)


def static_tree(path, include_dirs):
    missing = []

    def include(name):
        for directory in [Path(path).parent] + [Path(d) for d in include_dirs]:
            candidate = directory / name
            if candidate.is_file():
                return candidate.read_text(errors="replace")
        missing.append(name)
        return None

    tree = parse(Path(path).read_text(errors="replace"), include=include)
    return tree, missing


def compiled_tree(path, include_dirs, cpp="cpp", dtc="dtc", timeout=60):
    args = [cpp, "-nostdinc", "-undef", "-x", "assembler-with-cpp", "-D__DTS__", "-P"]
    for directory in [Path(path).parent] + [Path(d) for d in include_dirs]:
        args += ["-I", str(directory)]
    with tempfile.TemporaryDirectory() as temp:
        pre = Path(temp) / "pre.dts"
        out = Path(temp) / "out.dts"
        subprocess.run(args + [str(path), "-o", str(pre)], check=True, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = subprocess.run([dtc, "-I", "dts", "-O", "dts", "-o", str(out), str(pre)],
                                check=True, timeout=timeout, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        warnings = len([l for l in result.stderr.decode(errors="replace").splitlines() if "Warning" in l])
        return parse(out.read_text(errors="replace")), warnings


def report(path, include_dirs, mode):
    title = "%s [%s]" % (path, mode)
    if mode == "compiled":
        tree, warnings = compiled_tree(path, include_dirs)
        title += " dtc warnings: %d" % warnings
    else:
        tree, missing = static_tree(path, include_dirs)
        if missing:
            title += " unresolved includes: %s" % ", ".join(sorted(set(missing)))
    rows, notes = topology(tree)
    return format_table(title, rows, notes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dts", nargs="+", help="DTS files to report (e.g. shipped and candidate)")
    parser.add_argument("-I", "--include", action="append", default=[],
                        help="include directory (repeat): kernel arch/arm64/boot/dts/marvell, kernel include/")
    parser.add_argument("--static", action="store_true", help="parse sources without cpp/dtc")
    args = parser.parse_args(argv)
    mode = "static"
    if not args.static:
        if shutil.which("dtc") and shutil.which("cpp"):
            mode = "compiled"
        else:
            print("dtc or cpp not found; falling back to static source parsing "
                  "(install device-tree-compiler for a compiled comparison)", file=sys.stderr)
    status = 0
    for path in args.dts:
        try:
            print(report(path, args.include, mode))
        except (OSError, ParseError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", b"") or b""
            print("%s: %s %s" % (path, error, detail.decode(errors="replace")[-2000:]), file=sys.stderr)
            status = 2
        print()
    return status


if __name__ == "__main__":
    sys.exit(main())
