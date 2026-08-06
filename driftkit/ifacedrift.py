#!/usr/bin/env python3
"""ifacedrift.py: two descriptions of one API compared against each other.

protobuf (.proto) against OpenAPI 3 (openapi.json) or Swagger 2 (definitions,
including those from grpc-gateway).

Hard findings:
  missing-in-openapi / missing-in-proto  a field exists in one interface and is
      absent from the other while the message and the schema do correspond;
  default-mismatch     both sides name a default value and they differ;
  bound-mismatch       a declared minimum or maximum differs;
  required-mismatch    REST requires the field, gRPC allows omitting it;
  cardinality-mismatch a list on one side, a single value on the other.

Soft findings (they need reading by a human and are no findings by themselves):
  name-mismatch      one field looks like it is named two ways;
  structure-mismatch the name is the same and not a single field is shared;
  unit-mismatch      the descriptions name different units.

KNOWN BLIND SPOTS: the tool does not see anything outside the two documents it
is given, it does not read the server code, and it never judges a pair whose
schema holds no properties (enum and oneOf) because there is nothing to
compare there.

What the tool does NOT call a mismatch, because it is a difference of formats:
  - naming conventions: sparse_vectors_config against sparse_vectors,
    vector_name against vector, vectors against vector;
  - fields REST carries in the path or the query string (collection_name, timeout);
  - the response envelope: REST keeps time and usage around result;
  - a type discriminator: "type": "text" is needed by JSON while protobuf tells
    the variants apart by message type;
  - different depth: points_selector in gRPC against points and filter in REST;
  - a union member: DatetimeRange as its own field in gRPC and inside
    RangeInterface in REST.
Everything dismissed is counted and printed as a number, and by name with -v.

The .proto side is parsed through a real descriptor from protoc rather than with
regular expressions: a regex breaks on `map<K, V>` because of the comma inside
angle brackets. The JSON side goes through a parser that remembers line numbers,
since otherwise the coordinate of a property in openapi.json cannot be named.

Dependencies: protoc (brew install protobuf) and the protobuf package for Python.

WHICH PROJECTS THIS SUITS (checked on three of them)

Exactly one case suits it: **both interfaces are written by hand and are supposed
to agree**. That is how qdrant is built, and there the tool produced nine real
mismatches.

Two classes do not suit it, and spending time on them is waste:

  1. **One interface is generated from the other.** etcd and the whole
     grpc-gateway family: the swagger file is built from the same .proto files
     and no mismatch can exist by construction. A run gives zero, which is
     valuable as a negative control and useless as a search for defects.
     The signal: the repository holds protoc-gen-openapiv2, protoc-gen-swagger,
     a buf.gen.yaml with an openapi plugin, or the spec sits in a directory like
     apispec/generated.
  2. **The interfaces are built on fundamentally different shapes.** weaviate:
     REST describes a reference with one URI field
     (weaviate://localhost/objects/<uuid>/<property>) while gRPC splits the same
     thing into five fields. Seven findings, all seven false.
     The signal: after a run almost everything sits in structure-mismatch, or the
     findings gather around one or two nodes of the model.

Run:
  python3 ifacedrift.py --proto-dir <dir with .proto> --openapi <openapi.json>
  python3 ifacedrift.py ... -v            # list what was dismissed, by name
  python3 ifacedrift.py ... --json out.json

Tests: test_ifacedrift.py next to this file. Every finding is printed with a
file:line coordinate on both sides. An empty result is a good result.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# JSON with coordinates
# --------------------------------------------------------------------------


class JsonPos:
    """A JSON parse that remembers the line number of every node.

    Returns the same values as json.loads (a test checks the equality) plus a
    mapping path -> line, where a path is a tuple of keys and indices.
    """

    WS = " \t\n\r"

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.newlines = [m.start() for m in re.finditer("\n", text)]
        self.lines: Dict[Tuple[Any, ...], int] = {}

    def line_of(self, off: int) -> int:
        return bisect.bisect_right(self.newlines, off - 1) + 1

    def parse(self) -> Tuple[Any, Dict[Tuple[Any, ...], int]]:
        self._ws()
        val = self._value(())
        self._ws()
        if self.i != len(self.s):
            raise ValueError(f"garbage after the end of the JSON at offset {self.i}")
        return val, self.lines

    def _ws(self) -> None:
        s, n = self.s, len(self.s)
        i = self.i
        while i < n and s[i] in self.WS:
            i += 1
        self.i = i

    def _value(self, path: Tuple[Any, ...]) -> Any:
        self.lines[path] = self.line_of(self.i)
        c = self.s[self.i]
        if c == "{":
            return self._object(path)
        if c == "[":
            return self._array(path)
        if c == '"':
            return self._string()
        if self.s.startswith("true", self.i):
            self.i += 4
            return True
        if self.s.startswith("false", self.i):
            self.i += 5
            return False
        if self.s.startswith("null", self.i):
            self.i += 4
            return None
        return self._number()

    def _object(self, path: Tuple[Any, ...]) -> Dict[str, Any]:
        self.i += 1  # {
        out: Dict[str, Any] = {}
        self._ws()
        if self.s[self.i] == "}":
            self.i += 1
            return out
        while True:
            self._ws()
            key_off = self.i
            key = self._string()
            self._ws()
            if self.s[self.i] != ":":
                raise ValueError(f"expected ':' at offset {self.i}")
            self.i += 1
            self._ws()
            sub = path + (key,)
            out[key] = self._value(sub)
            # the key line matters more than the value line: edits go by key
            self.lines[sub] = self.line_of(key_off)
            self._ws()
            c = self.s[self.i]
            self.i += 1
            if c == ",":
                continue
            if c == "}":
                return out
            raise ValueError(f"expected ',' or '}}' at offset {self.i - 1}")

    def _array(self, path: Tuple[Any, ...]) -> List[Any]:
        self.i += 1  # [
        out: List[Any] = []
        self._ws()
        if self.s[self.i] == "]":
            self.i += 1
            return out
        idx = 0
        while True:
            self._ws()
            out.append(self._value(path + (idx,)))
            idx += 1
            self._ws()
            c = self.s[self.i]
            self.i += 1
            if c == ",":
                continue
            if c == "]":
                return out
            raise ValueError(f"expected ',' or ']' at offset {self.i - 1}")

    _ESC = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

    def _string(self) -> str:
        s = self.s
        if s[self.i] != '"':
            raise ValueError(f"expected a string at offset {self.i}")
        i = self.i + 1
        buf: List[str] = []
        while True:
            c = s[i]
            if c == '"':
                self.i = i + 1
                return "".join(buf)
            if c == "\\":
                e = s[i + 1]
                if e == "u":
                    code = int(s[i + 2 : i + 6], 16)
                    i += 6
                    # a surrogate pair
                    if 0xD800 <= code <= 0xDBFF and s[i : i + 2] == "\\u":
                        low = int(s[i + 2 : i + 6], 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            i += 6
                    buf.append(chr(code))
                    continue
                if e not in self._ESC:
                    raise ValueError(f"unknown escape sequence \\{e}")
                buf.append(self._ESC[e])
                i += 2
                continue
            buf.append(c)
            i += 1

    _NUM = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?")

    def _number(self) -> Any:
        m = self._NUM.match(self.s, self.i)
        if not m:
            raise ValueError(f"not a number at offset {self.i}")
        self.i = m.end()
        txt = m.group(0)
        if "." in txt or "e" in txt or "E" in txt:
            return float(txt)
        return int(txt)


def load_json_with_lines(path: str) -> Tuple[Any, Dict[Tuple[Any, ...], int]]:
    with open(path, encoding="utf-8") as fh:
        return JsonPos(fh.read()).parse()


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class PField:
    name: str
    number: int
    line: int
    file: str
    type_str: str
    repeated: bool
    is_map: bool
    optional: bool  # an explicit proto3 optional
    in_oneof: Optional[str]
    deprecated: bool
    comment: str


@dataclass
class PMessage:
    full_name: str
    short_name: str
    file: str
    line: int
    comment: str
    package: str = ""
    fields: List[PField] = dc_field(default_factory=list)


@dataclass
class OProp:
    name: str
    line: int
    description: str
    type_str: str
    ref: Optional[str]
    nullable: bool
    has_default: bool
    default: Any
    minimum: Optional[float]
    maximum: Optional[float]
    deprecated: bool
    required: bool
    union_refs: List[str] = dc_field(default_factory=list)


@dataclass
class OSchema:
    name: str
    line: int
    description: str
    props: Dict[str, OProp]
    required: Set[str]
    has_properties: bool
    kind: str  # object | enum | union | opaque
    enum_values: List[Any] = dc_field(default_factory=list)
    union_refs: List[str] = dc_field(default_factory=list)


@dataclass
class Finding:
    kind: str
    hard: bool
    subject: str
    message: str
    proto_ref: str
    openapi_ref: str
    detail: List[str] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# Parsing protobuf through protoc
# --------------------------------------------------------------------------

_SCALARS = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}
_SCALAR_NAMES = frozenset(_SCALARS.values())


def _protoc_binary() -> str:
    exe = os.environ.get("PROTOC") or shutil.which("protoc")
    if not exe:
        sys.exit(
            "protoc not found. Install it: brew install protobuf\n"
            "or point at it through the PROTOC environment variable."
        )
    return exe


def build_descriptor_set(
    proto_dir: str, includes: Sequence[str], out_path: str, skip: str = ""
) -> None:
    proto_dir = os.path.abspath(proto_dir)
    includes = [os.path.abspath(i) for i in includes]

    # The file name given to protoc has to match how the file is written in an
    # import, otherwise the same file enters twice under two names and everything
    # falls apart. When an explicit -I sits above the .proto directory, names are
    # counted from it.
    naming_root = proto_dir
    for inc in includes:
        if os.path.commonpath([inc, proto_dir]) == inc:
            naming_root = inc
            break

    skip_re = re.compile(skip) if skip else None
    files: List[str] = []
    for root, _dirs, names in os.walk(proto_dir):
        for n in sorted(names):
            if not n.endswith(".proto"):
                continue
            rel = os.path.relpath(os.path.join(root, n), naming_root)
            if skip_re and skip_re.search(rel):
                continue
            files.append(rel)
    if not files:
        sys.exit(f"no .proto files in {proto_dir}")

    cmd = [_protoc_binary(), "--include_source_info", f"--descriptor_set_out={out_path}"]
    for inc in ([naming_root] if naming_root != proto_dir else [proto_dir]) + [
        i for i in includes if i != naming_root
    ]:
        cmd += ["-I", inc]
    cmd += files
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("protoc could not parse the .proto files:\n" + res.stderr)


class MissingDependency(RuntimeError):
    """A check cannot run here, and that is not the same as finding nothing."""


def parse_proto(proto_dir: str, includes: Sequence[str], skip: str = "") -> Dict[str, PMessage]:
    # Same rule as in assertdrift: a missing dependency stops this check, not
    # the process. In a sweep a `sys.exit` here kills every check that would
    # have run afterwards, and in the log a dead tool looks exactly like a
    # clean one.
    try:
        from google.protobuf import descriptor_pb2
    except ImportError:
        raise MissingDependency(
            "the protobuf package for Python is missing. Install: pip3 install protobuf"
        )

    with tempfile.TemporaryDirectory() as td:
        pb = os.path.join(td, "set.pb")
        build_descriptor_set(proto_dir, includes, pb, skip)
        fds = descriptor_pb2.FileDescriptorSet()
        with open(pb, "rb") as fh:
            fds.ParseFromString(fh.read())

    # 1) collect every map entry so that the map<K, V> type can be restored
    map_entries: Dict[str, Any] = {}

    def collect_entries(pkg: str, msg, ) -> None:
        full = f"{pkg}.{msg.name}" if pkg else msg.name
        if msg.options.map_entry:
            map_entries[full] = msg
        for n in msg.nested_type:
            collect_entries(full, n)

    for f in fds.file:
        for m in f.message_type:
            collect_entries(f.package, m)

    def type_of(fld) -> Tuple[str, bool]:
        """Returns (human-readable type, is it a map)."""
        if fld.type == 11 and fld.label == 3:
            entry = map_entries.get(fld.type_name.lstrip("."))
            if entry is not None:
                k = type_of(entry.field[0])[0]
                v = type_of(entry.field[1])[0]
                return f"map<{k}, {v}>", True
        if fld.type in (11, 14):
            return fld.type_name.lstrip(".").split(".")[-1], False
        return _SCALARS.get(fld.type, f"type{fld.type}"), False

    out: Dict[str, PMessage] = {}

    def walk(f, prefix: Sequence[int], msg, name_prefix: str, loc: Dict[Tuple[int, ...], Any]) -> None:
        if msg.options.map_entry:
            return
        full = name_prefix + msg.name
        mloc = loc.get(tuple(prefix))
        pm = PMessage(
            full_name=full,
            short_name=msg.name,
            file=f.name,
            line=(mloc.span[0] + 1) if mloc else 0,
            comment=_clean_comment(mloc.leading_comments if mloc else ""),
            package=f.package,
        )
        for j, fld in enumerate(msg.field):
            floc = loc.get(tuple(prefix) + (2, j))
            type_str, is_map = type_of(fld)
            in_oneof = None
            if fld.HasField("oneof_index") and not fld.proto3_optional:
                in_oneof = msg.oneof_decl[fld.oneof_index].name
            comment = ""
            if floc:
                comment = _clean_comment(floc.leading_comments) or _clean_comment(floc.trailing_comments)
            pm.fields.append(
                PField(
                    name=fld.name,
                    number=fld.number,
                    line=(floc.span[0] + 1) if floc else 0,
                    file=f.name,
                    type_str=type_str,
                    repeated=fld.label == 3 and not is_map,
                    is_map=is_map,
                    optional=bool(fld.proto3_optional),
                    in_oneof=in_oneof,
                    deprecated=bool(fld.options.deprecated),
                    comment=comment,
                )
            )
        out[full] = pm
        for k, n in enumerate(msg.nested_type):
            walk(f, list(prefix) + [3, k], n, full + ".", loc)

    for f in fds.file:
        loc = {tuple(l.path): l for l in f.source_code_info.location}
        for i, m in enumerate(f.message_type):
            walk(f, [4, i], m, "", loc)
    return out


def _clean_comment(raw: str) -> str:
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln.startswith("*"):
            ln = ln[1:].strip()
        lines.append(ln)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Parsing OpenAPI
# --------------------------------------------------------------------------


def _ref_name(ref: str) -> Optional[str]:
    """Schema name out of a reference. Understands OpenAPI 3 and Swagger 2."""
    if isinstance(ref, str) and (
        ref.startswith("#/components/schemas/") or ref.startswith("#/definitions/")
    ):
        return ref.rsplit("/", 1)[-1]
    return None


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in src.items():
        if k not in dst:
            dst[k] = v


def _unwrap_prop(node: Dict[str, Any]) -> Dict[str, Any]:
    """Strips schemars wrappers: anyOf with {"nullable": true}, single-member allOf."""
    node = dict(node)
    for _ in range(4):
        changed = False
        for key in ("anyOf", "oneOf", "allOf"):
            members = node.get(key)
            if not isinstance(members, list):
                continue
            real = [m for m in members if isinstance(m, dict) and set(m) - {"nullable"}]
            nullable = any(isinstance(m, dict) and m.get("nullable") for m in members)
            if nullable:
                node["nullable"] = True
            if len(real) == 1:
                node.pop(key)
                _merge(node, real[0])
                changed = True
            elif len(real) > 1:
                node.pop(key)
                node["__union__"] = real
                changed = True
            else:
                node.pop(key)
                changed = True
        if not changed:
            break
    return node


def _type_str(node: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    ref = _ref_name(node.get("$ref", ""))
    if ref:
        return ref, ref
    if "__union__" in node:
        return "union", None
    t = node.get("type")
    if t == "array":
        items = _unwrap_prop(node.get("items") or {})
        inner, _ = _type_str(items)
        return f"array<{inner}>", None
    if t == "object" and "additionalProperties" in node:
        ap = node["additionalProperties"]
        if isinstance(ap, dict):
            inner, _ = _type_str(_unwrap_prop(ap))
            return f"map<string, {inner}>", None
    if "enum" in node and not t:
        return "enum", None
    fmt = node.get("format")
    if t and fmt:
        return f"{t}/{fmt}", None
    return t or "?", None


def parse_openapi(path: str) -> Tuple[Dict[str, OSchema], Set[str], Set[str], str]:
    data, lines = load_json_with_lines(path)
    if isinstance(data.get("definitions"), dict) and "components" not in data:
        schemas = data["definitions"]  # Swagger 2.0, including grpc-gateway output
        base: Tuple[Any, ...] = ("definitions",)
    else:
        schemas = data.get("components", {}).get("schemas", {})
        base = ("components", "schemas")

    # names REST carries in the path and the query string while gRPC has them as fields
    transport: Set[str] = set()
    # response envelope names: REST puts them around result, gRPC inside the message
    envelope: Set[str] = set()

    def scan_responses(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and "result" in props:
                envelope.update(k for k in props if k != "result")
            for v in node.values():
                scan_responses(v)
        elif isinstance(node, list):
            for v in node:
                scan_responses(v)

    for p, ops in (data.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for _m, op in ops.items():
            if not isinstance(op, dict):
                continue
            for pr in op.get("parameters") or []:
                if isinstance(pr, dict) and pr.get("name"):
                    transport.add(pr["name"])
            scan_responses(op.get("responses"))

    out: Dict[str, OSchema] = {}
    for name, raw in schemas.items():
        if not isinstance(raw, dict):
            continue
        spath = base + (name,)
        merged = dict(raw)
        # allOf composition at schema level: gather the properties of every member
        prop_src: List[Tuple[Dict[str, Any], Tuple[Any, ...]]] = [(merged, spath)]
        seen_refs: Set[str] = set()
        queue = list(enumerate(raw.get("allOf") or []))
        while queue:
            idx, member = queue.pop(0)
            if not isinstance(member, dict):
                continue
            r = _ref_name(member.get("$ref", ""))
            if r and r not in seen_refs and isinstance(schemas.get(r), dict):
                seen_refs.add(r)
                prop_src.append((schemas[r], base + (r,)))
            else:
                prop_src.append((member, spath + ("allOf", idx)))

        required: Set[str] = set()
        props: Dict[str, OProp] = {}
        has_properties = False
        for src, src_path in prop_src:
            for r in src.get("required") or []:
                required.add(r)
            pr = src.get("properties")
            if isinstance(pr, dict):
                has_properties = True
                for pname, pnode in pr.items():
                    if pname in props or not isinstance(pnode, dict):
                        continue
                    u = _unwrap_prop(pnode)
                    ts, ref = _type_str(u)
                    props[pname] = OProp(
                        name=pname,
                        line=lines.get(src_path + ("properties", pname), 0),
                        description=str(u.get("description") or u.get("title") or ""),
                        type_str=ts,
                        ref=ref,
                        nullable=bool(u.get("nullable") or u.get("x-nullable")),
                        has_default="default" in u and u["default"] is not None,
                        default=u.get("default"),
                        minimum=_as_num(u.get("minimum")),
                        maximum=_as_num(u.get("maximum")),
                        deprecated=bool(u.get("deprecated")),
                        required=False,
                        union_refs=[
                            r
                            for m in (u.get("__union__") or [])
                            for r in [_ref_name((m or {}).get("$ref", ""))]
                            if r
                        ],
                    )
        for pname, p in props.items():
            p.required = pname in required

        if has_properties:
            kind = "object"
        elif "enum" in raw:
            kind = "enum"
        elif any(k in raw for k in ("oneOf", "anyOf")):
            kind = "union"
        else:
            kind = "opaque"

        out[name] = OSchema(
            name=name,
            line=lines.get(spath, 0),
            description=str(raw.get("description") or ""),
            props=props,
            required=required,
            has_properties=has_properties,
            kind=kind,
            enum_values=list(raw.get("enum") or []),
            union_refs=[
                r
                for key in ("anyOf", "oneOf")
                for m in (raw.get(key) or [])
                for r in [_ref_name((m or {}).get("$ref", "")) if isinstance(m, dict) else None]
                if r
            ],
        )
    return out, transport, envelope, path


def _as_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


# --------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------

_NAME_SUFFIXES = (
    "config", "conf", "params", "param", "settings", "setting", "selector", "name",
)


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def norm_field(s: str) -> str:
    """A field name with convention differences removed: case, underscores, the
    -config suffix.

    protobuf writes `sparse_vectors_config` where JSON writes `sparse_vectors`,
    and that is the norm of two formats.
    """
    n = norm_name(s)
    for suf in _NAME_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return n


def norm_plural(s: str) -> str:
    """The same plus singular form: gRPC `vectors` against REST `vector`."""
    n = norm_field(s)
    if len(n) > 1 and n.endswith("s"):
        return n[:-1]
    return n


# --------------------------------------------------------------------------
# Default values taken from prose
# --------------------------------------------------------------------------

_VALUE = r"""(?:`[^`]+`|"[^"]*"|'[^']*'|[-+]?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_.]*)"""

_DEFAULT_PATTERNS = [
    re.compile(r"\bdefaults?\s+(?:is|are|to|=|:)\s*(" + _VALUE + r")", re.I),
    re.compile(r"\bdefault\s*[:=]\s*(" + _VALUE + r")", re.I),
    re.compile(r"\(\s*default\s*[:=]?\s*(" + _VALUE + r")\s*\)", re.I),
    re.compile(r"\bby default\s+(?:it\s+)?(?:is\s+)?(" + _VALUE + r")", re.I),
]

# words that follow "default" and are no value
_DEFAULT_STOP = {
    "value", "values", "is", "are", "the", "a", "an", "will", "would", "behaviour",
    "behavior", "setting", "settings", "config", "configuration", "parameter",
    "parameters", "param", "params", "if", "it", "this", "used", "use", "uses",
    "taken", "from", "not", "no", "for", "in", "of", "and", "or", "one", "same",
    "equal", "based", "defined", "set", "unset", "when", "unless", "otherwise",
    "depends", "depending", "empty", "applies", "chosen", "selected", "each",
}

_NULLISH = {"null", "none", "nil", "unset", "nothing"}


def parse_default(text: str) -> Optional[Tuple[str, Any, str]]:
    """Returns (kind, value, original snippet) or None.

    Kind: number | bool | null | word.
    """
    if not text:
        return None
    for pat in _DEFAULT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip().strip("`\"'")
        kind, val = _classify(raw)
        if kind == "word" and raw.lower() in _DEFAULT_STOP:
            continue
        if kind is None:
            continue
        return kind, val, text[max(0, m.start() - 10) : m.end() + 20].strip()
    return None


def _classify(raw: str) -> Tuple[Optional[str], Any]:
    low = raw.lower().rstrip(".,;:")
    if low in ("true", "false"):
        return "bool", low == "true"
    if low in _NULLISH:
        return "null", None
    try:
        if re.fullmatch(r"[-+]?\d+", low):
            return "number", float(int(low))
        return "number", float(low)
    except ValueError:
        pass
    if re.fullmatch(r"[a-z_][a-z0-9_.]*", low):
        return "word", low
    return None, None


def classify_value(v: Any) -> Tuple[Optional[str], Any]:
    if isinstance(v, bool):
        return "bool", v
    if isinstance(v, (int, float)):
        return "number", float(v)
    if v is None:
        return "null", None
    if isinstance(v, str):
        return _classify(v)
    return None, None


# --------------------------------------------------------------------------
# Bounds and units taken from prose
# --------------------------------------------------------------------------

_MIN_PATTERNS = [
    re.compile(r"\bminimum\s*(?:is|=|:)?\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bmin\.?\s*(?:is|=|:)\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(?:must|should)\s+be\s+(?:at\s+least|>=)\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bnot\s+(?:be\s+)?less\s+than\s+([-+]?\d+(?:\.\d+)?)", re.I),
]
_MAX_PATTERNS = [
    re.compile(r"\bmaximum\s*(?:is|=|:)?\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bmax\.?\s*(?:is|=|:)\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(?:must|should)\s+be\s+(?:at\s+most|<=)\s*([-+]?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bnot\s+(?:be\s+)?(?:greater|more)\s+than\s+([-+]?\d+(?:\.\d+)?)", re.I),
]


def parse_bound(text: str, patterns) -> Optional[float]:
    if not text:
        return None
    for pat in patterns:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


_UNIT_FAMILIES = {
    "time": {
        "nanosecond": "ns", "nanoseconds": "ns", "ns": "ns",
        "microsecond": "us", "microseconds": "us",
        "millisecond": "ms", "milliseconds": "ms", "ms": "ms", "msec": "ms",
        "second": "s", "seconds": "s", "sec": "s", "secs": "s",
        "minute": "min", "minutes": "min",
        "hour": "h", "hours": "h",
        "day": "d", "days": "d",
    },
    "size": {
        "byte": "b", "bytes": "b",
        "kb": "kb", "kib": "kb", "kilobyte": "kb", "kilobytes": "kb",
        "mb": "mb", "mib": "mb", "megabyte": "mb", "megabytes": "mb",
        "gb": "gb", "gib": "gb", "gigabyte": "gb", "gigabytes": "gb",
    },
}


def units_of(text: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    if not text:
        return out
    words = set(re.findall(r"[A-Za-z]+", text.lower()))
    for family, table in _UNIT_FAMILIES.items():
        found = {table[w] for w in words if w in table}
        if found:
            out[family] = found
    return out


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass
class Coverage:
    proto_messages: int = 0
    openapi_schemas: int = 0
    matched: int = 0
    ambiguous: List[str] = dc_field(default_factory=list)
    ambiguous_fields: List[str] = dc_field(default_factory=list)
    skipped_no_props: List[str] = dc_field(default_factory=list)
    suppressed_transport: List[str] = dc_field(default_factory=list)
    suppressed_deprecated: List[str] = dc_field(default_factory=list)
    suppressed_envelope: List[str] = dc_field(default_factory=list)
    suppressed_nested: List[str] = dc_field(default_factory=list)
    suppressed_discriminator: List[str] = dc_field(default_factory=list)
    suppressed_union: List[str] = dc_field(default_factory=list)
    excluded_files: List[str] = dc_field(default_factory=list)
    unmatched_proto: List[str] = dc_field(default_factory=list)
    unmatched_openapi: List[str] = dc_field(default_factory=list)


def match_messages(
    msgs: Dict[str, PMessage],
    schemas: Dict[str, OSchema],
    aliases: Dict[str, str],
    cov: Coverage,
) -> List[Tuple[PMessage, OSchema]]:
    by_norm: Dict[str, List[PMessage]] = {}
    for m in msgs.values():
        by_norm.setdefault(norm_name(m.short_name), []).append(m)

    # grpc-gateway writes a schema name with the package in front:
    # etcdserverpbRangeRequest. Known packages get stripped, otherwise no pair is
    # ever built.
    packages = sorted(
        {norm_name(p) for m in msgs.values() for p in [m.package] if p},
        key=len,
        reverse=True,
    )
    schema_by_norm: Dict[str, OSchema] = {}
    for s in schemas.values():
        key = norm_name(s.name)
        schema_by_norm.setdefault(key, s)
        for pkg in packages:
            if key.startswith(pkg) and len(key) > len(pkg):
                schema_by_norm.setdefault(key[len(pkg):], s)
                break

    pairs: List[Tuple[PMessage, OSchema]] = []
    used_schemas: Set[str] = set()

    # manual mappings take priority
    for pm_name, os_name in aliases.items():
        m = msgs.get(pm_name)
        s = schemas.get(os_name)
        if m and s:
            pairs.append((m, s))
            used_schemas.add(s.name)

    aliased_msgs = {n for n in aliases if n in msgs}
    for key, group in sorted(by_norm.items()):
        group = [m for m in group if m.full_name not in aliased_msgs]
        if not group:
            continue
        s = schema_by_norm.get(key)
        if s is None or s.name in used_schemas:
            for m in group:
                cov.unmatched_proto.append(f"{m.full_name} ({m.file}:{m.line})")
            continue
        if len(group) > 1:
            cov.ambiguous.append(
                f"{s.name}: this name fits "
                + ", ".join(f"{m.full_name} ({m.file}:{m.line})" for m in group)
            )
            continue
        pairs.append((group[0], s))
        used_schemas.add(s.name)

    for s in schemas.values():
        if s.name not in used_schemas:
            cov.unmatched_openapi.append(f"{s.name} ({s.line})")

    cov.proto_messages = len(msgs)
    cov.openapi_schemas = len(schemas)
    cov.matched = len(pairs)
    return pairs


def match_fields(
    msg: PMessage, schema: OSchema, field_aliases: Dict[str, str]
) -> Tuple[List[Tuple[PField, OProp]], List[PField], List[OProp], List[str]]:
    """Matches fields. Returns (pairs, proto only, openapi only, ambiguous)."""
    pfields = {f.name: f for f in msg.fields}
    oprops = dict(schema.props)
    pairs: List[Tuple[PField, OProp]] = []
    ambiguous: List[str] = []

    # 0) manual mappings
    for pname, oname in field_aliases.items():
        if pname in pfields and oname in oprops:
            pairs.append((pfields.pop(pname), oprops.pop(oname)))

    def index(names, keyfn) -> Dict[str, List[str]]:
        idx: Dict[str, List[str]] = {}
        for n in names:
            idx.setdefault(keyfn(n), []).append(n)
        return idx

    # 1) exact, 2) up to a convention suffix, 3) up to number
    for keyfn in (norm_name, norm_field, norm_plural):
        pidx = index(list(pfields), keyfn)
        oidx = index(list(oprops), keyfn)
        for k, pnames in sorted(pidx.items()):
            onames = oidx.get(k)
            # ambiguity (several names reduced to one key) is never resolved by guessing
            if not onames or len(pnames) != 1 or len(onames) != 1:
                continue
            pairs.append((pfields.pop(pnames[0]), oprops.pop(onames[0])))

    # whatever reduces to one key on both sides after three stages without
    # resolving uniquely goes into the ambiguous list and is NOT a finding:
    # reporting two "missing fields" here would be lying in our own favour
    pidx = index(list(pfields), norm_plural)
    oidx = index(list(oprops), norm_plural)
    for k in sorted(set(pidx) & set(oidx)):
        ambiguous.append(
            f"{msg.full_name} <-> {schema.name}: gRPC {', '.join(pidx[k])} "
            f"against REST {', '.join(oidx[k])}: which maps to which is unresolved"
        )
        for n in pidx[k]:
            pfields.pop(n, None)
        for n in oidx[k]:
            oprops.pop(n, None)

    return pairs, list(pfields.values()), list(oprops.values()), ambiguous


@dataclass
class Context:
    openapi_file: str
    transport: Set[str]
    envelope: Set[str]
    schemas: Dict[str, OSchema]
    proto_by_short: Dict[str, List[PMessage]]
    aliases: Dict[str, Dict[str, str]]
    cov: Coverage


def _nested_proto_names(msg: PMessage, ctx: Context) -> Set[str]:
    """Field names sitting one level deeper inside gRPC messages.

    REST often flattens a nested message into a plain set of properties:
    `points_selector` in gRPC against `points` and `filter` in REST.
    """
    out: Set[str] = set()
    for f in msg.fields:
        group = ctx.proto_by_short.get(f.type_str.split("<")[0].strip())
        if group and len(group) == 1:
            out.update(norm_plural(x.name) for x in group[0].fields)
    return out


def _nested_rest_names(schema: OSchema, ctx: Context) -> Set[str]:
    out: Set[str] = set()
    for p in schema.props.values():
        target = ctx.schemas.get(p.ref or "")
        if target is not None:
            out.update(norm_plural(x) for x in target.props)
    return out


def _rest_union_members(schema: OSchema, ctx: Context) -> Set[str]:
    """Types REST folds into a union under one name.

    `datetime_range` is its own field in gRPC while in REST it is a member of the
    `RangeInterface` union inside the `range` field. Both offer the capability and
    the shape differs.
    """
    out: Set[str] = set()
    for p in schema.props.values():
        out.update(p.union_refs)
        target = ctx.schemas.get(p.ref or "")
        if target is not None and target.kind == "union":
            out.update(target.union_refs)
    return out


def _is_discriminator(p: OProp, ctx: Context) -> bool:
    """A type discriminator property: JSON cannot tell variants apart without it,
    protobuf tells them apart by type."""
    target = ctx.schemas.get(p.ref or "")
    return target is not None and target.kind == "enum" and len(target.enum_values) == 1


def compare_pair(msg: PMessage, schema: OSchema, ctx: Context) -> List[Finding]:
    cov = ctx.cov
    out: List[Finding] = []
    pairs, only_proto, only_openapi, ambiguous = match_fields(
        msg, schema, ctx.aliases.get(msg.full_name, {})
    )
    cov.ambiguous_fields.extend(ambiguous)

    sref = f"{ctx.openapi_file}:{schema.line}"

    # Not a single shared field while both sides do have fields is one trouble
    # (different nesting depth or namesakes) rather than N losses.
    # Printing a list of "missing fields" here is lying in our own favour.
    if not pairs and msg.fields and schema.props:
        return [
            Finding(
                kind="structure-mismatch",
                hard=False,
                subject=f"{msg.full_name} <-> {schema.name}",
                message=(
                    "the name is the same and no field is shared: either the two "
                    "interfaces put this at different depths, or the name collided"
                ),
                proto_ref=f"{msg.file}:{msg.line}",
                openapi_ref=sref,
                detail=[
                    "gRPC:  " + ", ".join(f.name for f in msg.fields),
                    "REST:  " + ", ".join(sorted(schema.props)),
                ],
            )
        ]

    nested_rest = _nested_rest_names(schema, ctx)
    nested_proto = _nested_proto_names(msg, ctx)
    union_members = _rest_union_members(schema, ctx)

    for f in only_proto:
        where = f"{f.file}:{f.line}"
        key = f"{msg.full_name}.{f.name}"
        if f.deprecated or f.comment.lower().startswith("deprecated") or f.name.startswith("deprecated"):
            cov.suppressed_deprecated.append(f"{key} ({where})")
            continue
        if f.name in ctx.transport:
            cov.suppressed_transport.append(f"{key} ({where})")
            continue
        if msg.short_name.endswith("Response") and f.name in ctx.envelope:
            cov.suppressed_envelope.append(f"{key} ({where}): REST keeps this around result")
            continue
        if norm_plural(f.name) in nested_rest:
            cov.suppressed_nested.append(f"{key} ({where}): sits one level deeper in REST")
            continue
        if f.type_str in union_members:
            cov.suppressed_union.append(
                f"{key} ({where}): REST keeps the type {f.type_str} as a union member"
            )
            continue
        out.append(
            Finding(
                kind="missing-in-openapi",
                hard=f.in_oneof is None,
                subject=key,
                message=(
                    f"the field exists in gRPC ({f.type_str}) and not in the OpenAPI schema {schema.name}"
                    + (f"; the field sits in oneof {f.in_oneof}, REST may have shaped it differently" if f.in_oneof else "")
                ),
                proto_ref=where,
                openapi_ref=sref,
                detail=[f"proto: {f.type_str} {f.name} = {f.number}"] + _quote(f.comment),
            )
        )

    for p in only_openapi:
        where = f"{ctx.openapi_file}:{p.line}"
        key = f"{schema.name}.{p.name}"
        if p.deprecated:
            cov.suppressed_deprecated.append(f"{key} ({where})")
            continue
        if _is_discriminator(p, ctx):
            cov.suppressed_discriminator.append(f"{key} ({where}): a type discriminator for JSON")
            continue
        if norm_plural(p.name) in nested_proto:
            cov.suppressed_nested.append(f"{key} ({where}): sits one level deeper in gRPC")
            continue
        out.append(
            Finding(
                kind="missing-in-proto",
                hard=True,
                subject=key,
                message=f"the property exists in REST ({p.type_str}) and not in the gRPC message {msg.full_name}",
                proto_ref=f"{msg.file}:{msg.line}",
                openapi_ref=where,
                detail=[f"openapi: {p.name}: {p.type_str}"] + _quote(p.description),
            )
        )

    out = _fold_renames(out, msg, schema)

    for f, p in pairs:
        out.extend(compare_field(msg, schema, f, p, ctx.openapi_file))
    return out


def _fold_renames(findings: List[Finding], msg: PMessage, schema: OSchema) -> List[Finding]:
    """Two losses with nearly the same name are one name spelled two ways (the
    `AFL_GCC_ONLY_FSRV` against `FRSV` species) rather than two losses."""
    import difflib

    rest_only = [f for f in findings if f.kind == "missing-in-proto"]      # known to REST only
    grpc_only = [f for f in findings if f.kind == "missing-in-openapi"]    # known to gRPC only
    if not rest_only or not grpc_only:
        return findings

    used: Set[int] = set()
    folded: List[Finding] = []
    for a in grpc_only:
        a_name = a.subject.rsplit(".", 1)[1]
        best_i, best_r = -1, 0.0
        for i, b in enumerate(rest_only):
            if i in used:
                continue
            r = difflib.SequenceMatcher(
                None, norm_plural(a_name), norm_plural(b.subject.rsplit(".", 1)[1])
            ).ratio()
            if r > best_r:
                best_i, best_r = i, r
        if best_i >= 0 and best_r >= 0.75:
            b = rest_only[best_i]
            used.add(best_i)
            b_name = b.subject.rsplit(".", 1)[1]
            folded.append(
                Finding(
                    kind="name-mismatch",
                    hard=False,
                    subject=f"{msg.full_name}.{a_name}",
                    message=(
                        f"one field looks named two ways: gRPC {a_name}, "
                        f"REST {b_name} (similarity {best_r:.2f})"
                    ),
                    proto_ref=a.proto_ref,
                    openapi_ref=b.openapi_ref,
                    detail=a.detail + b.detail,
                )
            )
        else:
            folded.append(a)
    folded.extend(b for i, b in enumerate(rest_only) if i not in used)
    folded.extend(f for f in findings if f.kind not in ("missing-in-proto", "missing-in-openapi"))
    return folded


def compare_field(
    msg: PMessage, schema: OSchema, f: PField, p: OProp, openapi_file: str
) -> List[Finding]:
    out: List[Finding] = []
    pref = f"{f.file}:{f.line}"
    oref = f"{openapi_file}:{p.line}"
    subject = f"{msg.full_name}.{f.name}"

    # --- default value ----------------------------------------------------
    proto_def = parse_default(f.comment)
    if p.has_default:
        k, v = classify_value(p.default)
        rest_def = (k, v, f"default: {json.dumps(p.default)}") if k else None
    else:
        rest_def = parse_default(p.description)

    if proto_def and rest_def:
        pk, pv, ptxt = proto_def
        rk, rv, rtxt = rest_def
        same_kind = pk == rk
        equal = same_kind and _values_equal(pk, pv, rv)
        if not equal:
            out.append(
                Finding(
                    kind="default-mismatch",
                    hard=same_kind,
                    subject=subject,
                    message=(
                        f"the default value differs: gRPC says {_fmt(pk, pv)}, "
                        f"REST says {_fmt(rk, rv)}"
                        + ("" if same_kind else " (of different kinds, needs reading by a human)")
                    ),
                    proto_ref=pref,
                    openapi_ref=oref,
                    detail=[f"proto: ...{ptxt}...", f"openapi: ...{rtxt}..."],
                )
            )

    # --- bounds -----------------------------------------------------------
    proto_min = parse_bound(f.comment, _MIN_PATTERNS)
    proto_max = parse_bound(f.comment, _MAX_PATTERNS)
    rest_min = p.minimum if p.minimum is not None else parse_bound(p.description, _MIN_PATTERNS)
    rest_max = p.maximum if p.maximum is not None else parse_bound(p.description, _MAX_PATTERNS)
    for label, a, b in (("minimum", proto_min, rest_min), ("maximum", proto_max, rest_max)):
        if a is not None and b is not None and a != b:
            out.append(
                Finding(
                    kind="bound-mismatch",
                    hard=True,
                    subject=subject,
                    message=f"the {label} differs: gRPC promises {_num(a)}, REST promises {_num(b)}",
                    proto_ref=pref,
                    openapi_ref=oref,
                    detail=_quote(f.comment) + _quote(p.description),
                )
            )

    # --- required ---------------------------------------------------------
    # only one direction is reliable: REST requires the field while proto3 allows
    # omitting it
    if p.required and f.optional:
        out.append(
            Finding(
                kind="required-mismatch",
                hard=True,
                subject=subject,
                message=(
                    f"REST treats the field as required (required in schema {schema.name}) "
                    "while gRPC marks it optional and allows omitting it"
                ),
                proto_ref=pref,
                openapi_ref=oref,
                detail=[f"proto: optional {f.type_str} {f.name} = {f.number}"],
            )
        )

    # --- cardinality ------------------------------------------------------
    # Deliberately narrow. The comparison happens only when the REST side is named
    # plainly as a scalar or an array and the gRPC side is a scalar. When gRPC
    # holds a message, the list almost always sits one level deeper
    # (VectorsSelector { names }), which is a different shape rather than a
    # different cardinality.
    rest_array = p.type_str.startswith("array<")
    rest_scalar = bool(re.match(r"^(integer|number|string|boolean)(/|$)", p.type_str))
    if f.type_str not in _SCALAR_NAMES:
        rest_array = rest_scalar = False
    if f.repeated and rest_scalar:
        out.append(
            Finding(
                kind="cardinality-mismatch",
                hard=True,
                subject=subject,
                message=f"the field is repeated in gRPC (repeated {f.type_str}) and a single {p.type_str} in REST",
                proto_ref=pref,
                openapi_ref=oref,
                detail=_quote(f.comment) + _quote(p.description),
            )
        )
    elif rest_array and not f.repeated and not f.is_map:
        out.append(
            Finding(
                kind="cardinality-mismatch",
                hard=True,
                subject=subject,
                message=f"the field is an array in REST ({p.type_str}) and a single {f.type_str} in gRPC",
                proto_ref=pref,
                openapi_ref=oref,
                detail=_quote(f.comment) + _quote(p.description),
            )
        )

    # --- units (a soft check) ---------------------------------------------
    pu = units_of(f.comment)
    ru = units_of(p.description)
    for family in set(pu) & set(ru):
        if pu[family].isdisjoint(ru[family]):
            out.append(
                Finding(
                    kind="unit-mismatch",
                    hard=False,
                    subject=subject,
                    message=(
                        f"the descriptions name different units ({family}): "
                        f"gRPC {'/'.join(sorted(pu[family]))}, REST {'/'.join(sorted(ru[family]))}"
                    ),
                    proto_ref=pref,
                    openapi_ref=oref,
                    detail=_quote(f.comment) + _quote(p.description),
                )
            )
    return out


def _values_equal(kind: str, a: Any, b: Any) -> bool:
    if kind == "number":
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _fmt(kind: str, v: Any) -> str:
    if kind == "number":
        return _num(v)
    if kind == "null":
        return "null"
    return str(v)


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _quote(text: str, limit: int = 160) -> List[str]:
    if not text:
        return []
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return [f'    "{flat}"']


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run(
    proto_dir: str,
    openapi_path: str,
    includes: Sequence[str] = (),
    aliases: Optional[Dict[str, Any]] = None,
    exclude_proto: str = r"internal",
    skip_proto_file: str = "",
) -> Tuple[List[Finding], Coverage]:
    aliases = aliases or {}
    msgs = parse_proto(proto_dir, includes, skip_proto_file)
    schemas, transport, envelope, openapi_file = parse_openapi(openapi_path)
    cov = Coverage()

    # internal interfaces (node-to-node RPC) are not described by the public REST
    # surface, and namesakes from there build false pairs
    if exclude_proto:
        pat = re.compile(exclude_proto)
        dropped = {n: m for n, m in msgs.items() if pat.search(m.file)}
        if dropped:
            cov.excluded_files.append(
                f"{len(dropped)} messages from files matching /{exclude_proto}/: "
                + ", ".join(sorted({m.file for m in dropped.values()}))
            )
        msgs = {n: m for n, m in msgs.items() if n not in dropped}

    pairs = match_messages(msgs, schemas, aliases.get("messages", {}), cov)
    proto_by_short: Dict[str, List[PMessage]] = {}
    for m in msgs.values():
        proto_by_short.setdefault(m.short_name, []).append(m)

    ctx = Context(
        openapi_file=openapi_file,
        transport=transport,
        envelope=envelope,
        schemas=schemas,
        proto_by_short=proto_by_short,
        aliases=aliases.get("fields", {}),
        cov=cov,
    )

    findings: List[Finding] = []
    real_pairs = 0
    for m, s in pairs:
        if not s.has_properties:
            cov.skipped_no_props.append(f"{m.full_name} <-> {s.name} ({s.kind} schema, no properties)")
            continue
        real_pairs += 1
        findings.extend(compare_pair(m, s, ctx))
    cov.matched = real_pairs
    order = {
        "missing-in-proto": 0,
        "missing-in-openapi": 1,
        "default-mismatch": 2,
        "bound-mismatch": 3,
        "required-mismatch": 4,
        "cardinality-mismatch": 5,
        "name-mismatch": 6,
        "structure-mismatch": 7,
        "unit-mismatch": 8,
    }
    findings.sort(key=lambda f: (not f.hard, order.get(f.kind, 9), f.subject))
    return findings, cov


def print_report(findings: List[Finding], cov: Coverage, args) -> None:
    hard = [f for f in findings if f.hard]
    soft = [f for f in findings if not f.hard]

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.kind}] {f.subject}")
            print(f"  {f.message}")
            print(f"  proto:   {f.proto_ref}")
            print(f"  openapi: {f.openapi_ref}")
            for d in f.detail:
                print(f"  {d}")

    block("Mismatches", hard)
    block("Needs reading by a human", soft)

    print("\n=== Coverage ===")
    print(f"  messages in proto:      {cov.proto_messages}")
    print(f"  schemas in OpenAPI:     {cov.openapi_schemas}")
    print(f"  pairs compared:         {cov.matched}")
    print(f"  pairs with no fields:   {len(cov.skipped_no_props)} (enum/oneOf, nothing to compare)")
    print(f"  ambiguous message name: {len(cov.ambiguous)}")
    print(f"  ambiguous field name:   {len(cov.ambiguous_fields)}")
    print(f"  unpaired in OpenAPI:    {len(cov.unmatched_proto)}")
    print(f"  unpaired in proto:      {len(cov.unmatched_openapi)}")
    print(f"  dismissed as transport: {len(cov.suppressed_transport)} (REST carries it in the path or query)")
    print(f"  dismissed as envelope:  {len(cov.suppressed_envelope)}")
    print(f"  dismissed as nesting:   {len(cov.suppressed_nested)}")
    print(f"  dismissed as type tag:  {len(cov.suppressed_discriminator)}")
    print(f"  dismissed as union:     {len(cov.suppressed_union)}")
    print(f"  dismissed as deprecated:{len(cov.suppressed_deprecated)}")
    for note in cov.excluded_files:
        print(f"  excluded from the parse:{note}")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, []))

    if args.verbose:
        for title, items in (
            ("Ambiguous message name, no pair built", cov.ambiguous),
            ("Ambiguous field name, no comparison made", cov.ambiguous_fields),
            ("Dismissed as transport", cov.suppressed_transport),
            ("Dismissed as response envelope", cov.suppressed_envelope),
            ("Dismissed as different nesting", cov.suppressed_nested),
            ("Dismissed as type discriminator", cov.suppressed_discriminator),
            ("Dismissed as union member", cov.suppressed_union),
            ("Dismissed as deprecated", cov.suppressed_deprecated),
            ("proto messages with no pair", cov.unmatched_proto),
            ("OpenAPI schemas with no pair", cov.unmatched_openapi),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items:
                    print(f"  {i}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="protobuf against OpenAPI")
    ap.add_argument("--proto-dir", required=True, help="directory holding the .proto files")
    ap.add_argument("--openapi", required=True, help="openapi.json")
    ap.add_argument("-I", "--include", action="append", default=[], help="an extra include path for protoc")
    ap.add_argument("--aliases", help="JSON of manual message and field mappings")
    ap.add_argument(
        "--exclude-proto",
        default=r"internal",
        help="pattern of .proto files not to compare (internal by default; "
        "an empty string turns the exclusion off)",
    )
    ap.add_argument(
        "--skip-proto-file",
        default="",
        help="pattern of .proto files never handed to protoc at all "
        "(for those that do not build outside their own build script)",
    )
    ap.add_argument("--json", help="write findings to JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="list what was dismissed and what went unmatched, by name")
    args = ap.parse_args(argv)

    aliases = {}
    if args.aliases:
        with open(args.aliases, encoding="utf-8") as fh:
            aliases = json.load(fh)

    try:
        findings, cov = run(
            args.proto_dir, args.openapi, args.include, aliases,
            args.exclude_proto, args.skip_proto_file
        )
    except MissingDependency as exc:
        # Exit code 2 says "could not check", distinct from 0 "clean" and
        # 1 "found something". A sweep can tell the three apart; a dead
        # process could not be told from a clean one.
        print(f"\n=== NOT RUN ===\n  {exc}")
        return 2
    print_report(findings, cov, args)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "kind": f.kind,
                        "hard": f.hard,
                        "subject": f.subject,
                        "message": f.message,
                        "proto": f.proto_ref,
                        "openapi": f.openapi_ref,
                        "detail": f.detail,
                    }
                    for f in findings
                ],
                fh,
                ensure_ascii=False,
                indent=1,
            )
    return 1 if any(f.hard for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
