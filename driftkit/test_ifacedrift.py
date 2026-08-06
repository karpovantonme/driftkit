#!/usr/bin/env python3
"""Tests for ifacedrift.py.

The working order here is tests on known cases first, code afterwards. Every
test below is either a row from the "the tool lies in its own favour" table
carried over to interface comparison, or a known truth about qdrant.

Run: python3 test_ifacedrift.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ifacedrift as ifd  # noqa: E402

# protobuf is not installed here, and without it every test in this file fails
# for a reason unrelated to the code. Skipping says so; the tool itself now
# returns exit code 2 instead of killing a sweep.
try:
    from google.protobuf import descriptor_pb2 as _pb  # noqa: F401
    _HAS_PROTOBUF = True
except ImportError:
    _HAS_PROTOBUF = False
needs_protobuf = unittest.skipIf(not _HAS_PROTOBUF, "protobuf is not installed")

QDRANT = os.path.expanduser("~/Projects/oss/qdrant")
QDRANT_PROTO = os.path.join(QDRANT, "lib/api/src/grpc/proto")
QDRANT_OPENAPI = os.path.join(QDRANT, "docs/redoc/master/openapi.json")
HAS_QDRANT = os.path.isdir(QDRANT_PROTO) and os.path.isfile(QDRANT_OPENAPI)


def scenario(proto_text: str, openapi: dict):
    """Builds a pair of files in a temporary directory and compares them."""
    td = tempfile.mkdtemp()
    pdir = os.path.join(td, "proto")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "a.proto"), "w", encoding="utf-8") as fh:
        fh.write(proto_text)
    opath = os.path.join(td, "openapi.json")
    with open(opath, "w", encoding="utf-8") as fh:
        json.dump(openapi, fh, indent=1)
    return ifd.run(pdir, opath)


def obj(props: dict, required=None, **extra):
    d = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    d.update(extra)
    return d


def kinds(findings):
    return sorted({f.kind for f in findings})


def subjects(findings, kind=None):
    return sorted(f.subject for f in findings if kind is None or f.kind == kind)


# --------------------------------------------------------------------------
# Parsing JSON with coordinates
# --------------------------------------------------------------------------


@needs_protobuf
class TestJsonPos(unittest.TestCase):
    def test_equals_stdlib_on_tricky_input(self):
        tricky = (
            '{"a":[1,2.5,-3,1e3,1.5E-2],"b":{"c":null,"d":true,"e":false},'
            '"esc":"a\\"b\\\\c\\/d\\n\\t\\u0041\\ud83d\\ude00","empty":{},"earr":[],'
            '"\\u043a\\u043b\\u044e\\u0447":"value"}'
        )
        val, _ = ifd.JsonPos(tricky).parse()
        self.assertEqual(val, json.loads(tricky))

    @unittest.skipUnless(HAS_QDRANT, "no qdrant clone")
    def test_equals_stdlib_on_real_openapi(self):
        with open(QDRANT_OPENAPI, encoding="utf-8") as fh:
            text = fh.read()
        val, lines = ifd.JsonPos(text).parse()
        self.assertEqual(val, json.loads(text))
        self.assertGreater(len(lines), 1000)

    def test_line_numbers_point_at_the_key(self):
        text = '{\n "components": {\n  "schemas": {\n   "S": {\n    "properties": {\n     "x": {\n      "type": "integer"\n     }\n    }\n   }\n  }\n }\n}'
        _, lines = ifd.JsonPos(text).parse()
        self.assertEqual(lines[("components", "schemas", "S")], 4)
        self.assertEqual(lines[("components", "schemas", "S", "properties", "x")], 6)


# --------------------------------------------------------------------------
# Parsing proto: what a regex used to break on
# --------------------------------------------------------------------------


@needs_protobuf
class TestProtoParsing(unittest.TestCase):
    def parse(self, text):
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.proto"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return ifd.parse_proto(td, [])

    def test_map_field_survives_comma_inside_angle_brackets(self):
        """A regex missed map<K, V> because of the comma inside the brackets."""
        msgs = self.parse(
            """
            syntax = "proto3";
            package t;
            message Value { string s = 1; }
            message M {
              // Collection data types
              map<string, Value> payload_schema = 1;
              // the next field has to be read after a map
              uint32 after = 2;
              map<string, uint64> counters = 3;
            }
            """
        )
        f = {x.name: x for x in msgs["M"].fields}
        self.assertEqual(sorted(f), ["after", "counters", "payload_schema"])
        self.assertEqual(f["payload_schema"].type_str, "map<string, Value>")
        self.assertEqual(f["counters"].type_str, "map<string, uint64>")
        self.assertTrue(f["payload_schema"].is_map)
        self.assertFalse(f["payload_schema"].repeated)

    def test_map_entry_is_not_reported_as_a_message(self):
        """A synthetic MapEntry must not enter the list of messages."""
        msgs = self.parse(
            'syntax = "proto3"; package t; message M { map<string, uint32> m = 1; }'
        )
        self.assertEqual(sorted(msgs), ["M"])

    def test_oneof_and_nested_fields_are_extracted(self):
        """The analogue of "arguments were not taken out of operator()": the
        construct swallowed fields."""
        msgs = self.parse(
            """
            syntax = "proto3";
            package t;
            message M {
              oneof pick {
                uint64 num = 1;
                string uuid = 2;
              }
              optional bool flag = 3;
              message Inner { uint32 deep = 1; }
              Inner inner = 4;
            }
            """
        )
        f = {x.name: x for x in msgs["M"].fields}
        self.assertEqual(sorted(f), ["flag", "inner", "num", "uuid"])
        self.assertEqual(f["num"].in_oneof, "pick")
        self.assertEqual(f["uuid"].in_oneof, "pick")
        # proto3 optional is implemented as a synthetic oneof and must not be
        # taken for a real one
        self.assertIsNone(f["flag"].in_oneof)
        self.assertTrue(f["flag"].optional)
        self.assertIn("M.Inner", msgs)
        self.assertEqual([x.name for x in msgs["M.Inner"].fields], ["deep"])

    def test_all_comment_forms_are_seen(self):
        """The analogue of "only /*! was seen": blindness to a comment form."""
        msgs = self.parse(
            """
            syntax = "proto3";
            package t;
            message M {
              // Default is 1
              uint32 a = 1;
              /* Default is 2 */
              uint32 b = 2;
              /**
               * Default is 3
               */
              uint32 c = 3;
              uint32 d = 4;  // Default is 4
              // first line
              // Default is 5
              uint32 e = 5;
            }
            """
        )
        f = {x.name: x for x in msgs["M"].fields}
        for name, want in (("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 4.0), ("e", 5.0)):
            got = ifd.parse_default(f[name].comment)
            self.assertIsNotNone(got, f"the comment of field {name} was not read: {f[name].comment!r}")
            self.assertEqual(got[1], want, name)

    def test_field_options_and_reserved_do_not_shift_fields(self):
        msgs = self.parse(
            """
            syntax = "proto3";
            package t;
            message M {
              reserved 2, 3;
              reserved "old_name";
              uint32 a = 1 [deprecated = true];
              // a line with a semicolon; and a brace } inside the comment
              string b = 4;
            }
            """
        )
        f = {x.name: x for x in msgs["M"].fields}
        self.assertEqual(sorted(f), ["a", "b"])
        self.assertTrue(f["a"].deprecated)
        self.assertFalse(f["b"].deprecated)

    def test_line_numbers_are_real(self):
        text = 'syntax = "proto3";\npackage t;\nmessage M {\n  uint32 a = 1;\n  string b = 2;\n}\n'
        msgs = self.parse(text)
        f = {x.name: x for x in msgs["M"].fields}
        self.assertEqual(msgs["M"].line, 3)
        self.assertEqual(f["a"].line, 4)
        self.assertEqual(f["b"].line, 5)


# --------------------------------------------------------------------------
# Parsing OpenAPI
# --------------------------------------------------------------------------


@needs_protobuf
class TestOpenApiParsing(unittest.TestCase):
    def load(self, doc):
        td = tempfile.mkdtemp()
        p = os.path.join(td, "openapi.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        return ifd.parse_openapi(p)

    def test_anyof_nullable_wrapper_is_unwrapped(self):
        schemas, _, _, _ = self.load(
            {
                "components": {
                    "schemas": {
                        "Target": {"type": "object", "properties": {}},
                        "S": obj(
                            {
                                "x": {
                                    "description": "d",
                                    "anyOf": [
                                        {"$ref": "#/components/schemas/Target"},
                                        {"nullable": True},
                                    ],
                                }
                            }
                        ),
                    }
                }
            }
        )
        p = schemas["S"].props["x"]
        self.assertEqual(p.type_str, "Target")
        self.assertTrue(p.nullable)
        self.assertEqual(p.description, "d")

    def test_default_null_is_not_a_stated_default(self):
        """schemars writes "default": null everywhere. Taking that for a value
        invents a mismatch out of nothing."""
        schemas, _, _, _ = self.load(
            {
                "components": {
                    "schemas": {
                        "S": obj(
                            {
                                "x": {"type": "integer", "default": None},
                                "y": {"type": "integer", "default": 7},
                            }
                        )
                    }
                }
            }
        )
        self.assertFalse(schemas["S"].props["x"].has_default)
        self.assertTrue(schemas["S"].props["y"].has_default)

    def test_allof_composition_gathers_properties(self):
        schemas, _, _, _ = self.load(
            {
                "components": {
                    "schemas": {
                        "Base": obj({"a": {"type": "integer"}}, required=["a"]),
                        "S": {"allOf": [{"$ref": "#/components/schemas/Base"}], "properties": {"b": {"type": "string"}}},
                    }
                }
            }
        )
        self.assertEqual(sorted(schemas["S"].props), ["a", "b"])
        self.assertTrue(schemas["S"].props["a"].required)

    def test_transport_parameters_are_collected(self):
        _, transport, _, _ = self.load(
            {
                "paths": {
                    "/collections/{collection_name}": {
                        "put": {
                            "parameters": [
                                {"name": "collection_name", "in": "path"},
                                {"name": "timeout", "in": "query"},
                            ]
                        }
                    }
                },
                "components": {"schemas": {}},
            }
        )
        self.assertEqual(transport, {"collection_name", "timeout"})


# --------------------------------------------------------------------------
# Default values taken from prose
# --------------------------------------------------------------------------


@needs_protobuf
class TestDefaultProse(unittest.TestCase):
    def test_forms_that_must_be_read(self):
        cases = {
            "Default is 1": 1.0,
            "Number of shards. Default is 1 for standalone, otherwise equal to nodes": 1.0,
            "Default: true": True,
            "default = 1": 1.0,
            "0.0 for never, 1.0 for always. Default is 0.4.": 0.4,
            "Search timeout (default: 5)": 5.0,
            "Defaults to 10": 10.0,
            "By default 3": 3.0,
            "Sharding method Default is Auto - points are distributed": "auto",
            "Default is `Cosine`": "cosine",
        }
        for text, want in cases.items():
            got = ifd.parse_default(text)
            self.assertIsNotNone(got, f"not read: {text!r}")
            self.assertEqual(got[1], want, text)

    def test_phrases_that_must_not_be_read_as_a_value(self):
        for text in (
            "Wait timeout for operation commit in seconds, if not specified - default value will be supplied",
            "If none - values from service configuration file are used.",
            "Defaults are taken from the config file",
            "Custom params for HNSW index.",
            "This is the default behaviour",
            "",
        ):
            self.assertIsNone(ifd.parse_default(text), f"a value was invented out of: {text!r}")

    def test_bounds_from_prose(self):
        self.assertEqual(ifd.parse_bound("Minimum is 1", ifd._MIN_PATTERNS), 1.0)
        self.assertEqual(ifd.parse_bound("must be at least 2", ifd._MIN_PATTERNS), 2.0)
        self.assertEqual(ifd.parse_bound("Maximum is 65536", ifd._MAX_PATTERNS), 65536.0)
        self.assertIsNone(ifd.parse_bound("no numbers here", ifd._MIN_PATTERNS))


# --------------------------------------------------------------------------
# Comparison: the tool has to stay quiet where there is no mismatch
# --------------------------------------------------------------------------

SIMPLE_PROTO = """
syntax = "proto3";
package t;
message VectorsConfig { uint32 size = 1; }
message SparseVectorConfig { uint32 size = 1; }
message CreateCollection {
  // Name of the collection
  string collection_name = 1;
  // Configuration for vectors
  optional VectorsConfig vectors_config = 10;
  // Configuration for sparse vectors
  optional SparseVectorConfig sparse_vectors_config = 16;
  // Number of shards, default is 1 for standalone. Minimum is 1
  optional uint32 shard_number = 7;
  // Wait timeout for operation commit in seconds, if not specified - default
  // value will be supplied
  optional uint64 timeout = 9;
}
"""


def simple_openapi(**overrides):
    props = {
        "vectors": {"$ref": "#/components/schemas/VectorsConfig"},
        "sparse_vectors": {"$ref": "#/components/schemas/SparseVectorConfig"},
        "shard_number": {
            "description": "Number of shards in collection. - Default is 1 for standalone - Minimum is 1",
            "default": None,
            "type": "integer",
            "format": "uint32",
            "minimum": 1,
            "nullable": True,
        },
    }
    props.update(overrides)
    return {
        "paths": {
            "/collections/{collection_name}": {
                "put": {
                    "parameters": [
                        {"name": "collection_name", "in": "path"},
                        {"name": "timeout", "in": "query"},
                    ]
                }
            }
        },
        "components": {
            "schemas": {
                "VectorsConfig": obj({"size": {"type": "integer"}}),
                "SparseVectorConfig": obj({"size": {"type": "integer"}}),
                "CreateCollection": obj(props),
            }
        },
    }


@needs_protobuf
class TestSilenceWhenClean(unittest.TestCase):
    def test_naming_conventions_are_not_findings(self):
        """vectors_config against vectors is the norm of two formats. Naive
        comparison on qdrant gave 85 "mismatches" of this kind."""
        findings, cov = scenario(SIMPLE_PROTO, simple_openapi())
        self.assertEqual(findings, [], "\n".join(f"{f.kind} {f.subject}" for f in findings))
        self.assertEqual(cov.matched, 3)

    def test_transport_fields_are_suppressed_not_reported(self):
        findings, cov = scenario(SIMPLE_PROTO, simple_openapi())
        self.assertEqual(findings, [])
        # REST carries collection_name and timeout in the path and query string
        self.assertEqual(len(cov.suppressed_transport), 2)
        self.assertTrue(any("collection_name" in s for s in cov.suppressed_transport))

    def test_matching_bounds_and_defaults_stay_silent(self):
        findings, _ = scenario(SIMPLE_PROTO, simple_openapi())
        self.assertEqual([f for f in findings if f.kind in ("default-mismatch", "bound-mismatch")], [])

    def test_deprecated_field_is_suppressed(self):
        proto = """
        syntax = "proto3";
        package t;
        message M {
          uint32 a = 1;
          // Deprecated
          uint32 old = 2 [deprecated = true];
        }
        """
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer"}})}}}
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [])
        self.assertEqual(len(cov.suppressed_deprecated), 1)

    def test_zero_overlap_is_one_finding_not_many(self):
        """One name and no shared field means different depth or namesakes.
        Printing five "losses" here is lying in our own favour."""
        proto = 'syntax = "proto3"; package t;\nmessage Q { uint32 a = 1; uint32 b = 2; uint32 c = 3; }\n'
        doc = {"components": {"schemas": {"Q": obj({"x": {"type": "integer"}, "y": {"type": "integer"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual([f.kind for f in findings], ["structure-mismatch"])
        self.assertFalse(findings[0].hard)

    def test_json_type_discriminator_is_not_a_finding(self):
        """JSON tells variants apart by a type field, protobuf by the message type."""
        proto = 'syntax = "proto3"; package t;\nmessage TextIndexParams { uint32 min_token_len = 1; }\n'
        doc = {
            "components": {
                "schemas": {
                    "TextIndexType": {"type": "string", "enum": ["text"]},
                    "TextIndexParams": obj(
                        {
                            "min_token_len": {"type": "integer"},
                            "type": {"$ref": "#/components/schemas/TextIndexType"},
                        },
                        required=["type"],
                    ),
                }
            }
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [])
        self.assertEqual(len(cov.suppressed_discriminator), 1)

    def test_flattened_nesting_is_not_a_finding(self):
        """gRPC keeps points_selector as its own message while REST spreads it
        into points and filter. A different shape rather than a missing field."""
        proto = """
        syntax = "proto3";
        package t;
        message PointsSelector { string points = 1; string filter = 2; }
        message DeletePayload {
          string key = 1;
          PointsSelector points_selector = 2;
        }
        """
        doc = {
            "components": {
                "schemas": {
                    "DeletePayload": obj(
                        {
                            "key": {"type": "string"},
                            "points": {"type": "string"},
                            "filter": {"type": "string"},
                        }
                    )
                }
            }
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [], [f"{f.kind} {f.subject}" for f in findings])
        self.assertTrue(cov.suppressed_nested)

    def test_response_envelope_is_not_a_finding(self):
        proto = 'syntax = "proto3"; package t;\nmessage FacetResponse { string hits = 1; double time = 2; string usage = 3; }\n'
        doc = {
            "paths": {
                "/facet": {
                    "post": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "time": {"type": "number"},
                                                "usage": {"type": "string"},
                                                "status": {"type": "string"},
                                                "result": {"$ref": "#/components/schemas/FacetResponse"},
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {"schemas": {"FacetResponse": obj({"hits": {"type": "string"}})}},
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [], [f"{f.kind} {f.subject}" for f in findings])
        self.assertEqual(len(cov.suppressed_envelope), 2)

    def test_union_member_is_not_a_finding(self):
        """gRPC names datetime_range as its own field while REST folds Range and
        DatetimeRange into a union called range. Both offer the capability."""
        proto = """
        syntax = "proto3";
        package t;
        message Range { double gt = 1; }
        message DatetimeRange { string gt = 1; }
        message FieldCondition {
          string key = 1;
          Range range = 2;
          DatetimeRange datetime_range = 3;
        }
        """
        doc = {
            "components": {
                "schemas": {
                    "Range": obj({"gt": {"type": "number"}}),
                    "DatetimeRange": obj({"gt": {"type": "string"}}),
                    "RangeInterface": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/Range"},
                            {"$ref": "#/components/schemas/DatetimeRange"},
                        ]
                    },
                    "FieldCondition": obj(
                        {
                            "key": {"type": "string"},
                            "range": {"$ref": "#/components/schemas/RangeInterface"},
                        }
                    ),
                }
            }
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [], [f"{f.kind} {f.subject}" for f in findings])
        self.assertEqual(len(cov.suppressed_union), 1)

    def test_schema_without_properties_is_not_compared(self):
        """A proto oneof against oneOf/enum in OpenAPI: no properties, nothing to
        compare. Without this guard every field of such a message becomes a false
        finding."""
        proto = """
        syntax = "proto3";
        package t;
        message PointId {
          oneof point_id_options {
            uint64 num = 1;
            string uuid = 2;
          }
        }
        """
        doc = {
            "components": {
                "schemas": {
                    "PointId": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                }
            }
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [])
        self.assertEqual(len(cov.skipped_no_props), 1)


# --------------------------------------------------------------------------
# Comparison: the tool has to find the real thing
# --------------------------------------------------------------------------


@needs_protobuf
class TestFindsRealDrift(unittest.TestCase):
    def test_default_mismatch(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Default is 1\n optional uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer", "default": 2}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "default-mismatch"), ["M.a"])
        self.assertIn("1", findings[0].message)
        self.assertIn("2", findings[0].message)

    def test_default_mismatch_in_prose_on_both_sides(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Default is 0.4.\n optional double a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "number", "description": "Default is 0.8."}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "default-mismatch"), ["M.a"])

    def test_missing_in_openapi(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n uint32 a = 1;\n string only_grpc = 2;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "missing-in-openapi"), ["M.only_grpc"])
        self.assertTrue(findings[0].proto_ref.endswith(":4"), findings[0].proto_ref)

    def test_missing_in_proto(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer"}, "only_rest": {"type": "string"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "missing-in-proto"), ["M.only_rest"])
        self.assertNotEqual(findings[0].openapi_ref.rsplit(":", 1)[1], "0")

    def test_required_mismatch(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n optional uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer"}}, required=["a"])}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "required-mismatch"), ["M.a"])

    def test_bound_mismatch(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Minimum is 1\n optional uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer", "minimum": 2}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "bound-mismatch"), ["M.a"])

    def test_unit_mismatch_is_soft(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Timeout in seconds\n optional uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer", "description": "Timeout in milliseconds"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "unit-mismatch"), ["M.a"])
        self.assertFalse(findings[0].hard)

    def test_same_units_are_silent(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Timeout in seconds\n optional uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer", "description": "Timeout, seconds"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(findings, [])

    def test_name_written_two_ways_is_folded_into_one_finding(self):
        """The `AFL_GCC_ONLY_FSRV` against `FRSV` species: one name spelled two
        ways. That is one finding rather than two losses."""
        proto = 'syntax = "proto3"; package t;\nmessage M {\n uint32 anchor = 1;\n uint32 replica_states = 2;\n}\n'
        doc = {
            "components": {
                "schemas": {
                    "M": obj({"anchor": {"type": "integer"}, "replicate_states": {"type": "integer"}})
                }
            }
        }
        findings, _ = scenario(proto, doc)
        self.assertEqual([f.kind for f in findings], ["name-mismatch"])
        self.assertIn("replica_states", findings[0].message)
        self.assertIn("replicate_states", findings[0].message)
        self.assertFalse(findings[0].hard)

    def test_unrelated_names_are_not_folded(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n uint32 anchor = 1;\n uint32 to_shard_id = 2;\n}\n'
        doc = {
            "components": {
                "schemas": {"M": obj({"anchor": {"type": "integer"}, "comment": {"type": "string"}})}
            }
        }
        findings, _ = scenario(proto, doc)
        self.assertEqual(kinds(findings), ["missing-in-openapi", "missing-in-proto"])

    def test_cardinality_mismatch(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n repeated uint32 a = 1;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(subjects(findings, "cardinality-mismatch"), ["M.a"])

    def test_cardinality_not_judged_when_grpc_side_is_a_message(self):
        """gRPC keeps the list inside a wrapper message (VectorsSelector { names })
        while REST prints an array directly. A different shape rather than a
        different cardinality."""
        proto = """
        syntax = "proto3";
        package t;
        message VectorsSelector { repeated string names = 1; }
        message M { string key = 1; VectorsSelector vectors = 2; }
        """
        doc = {
            "components": {
                "schemas": {
                    "M": obj({"key": {"type": "string"}, "vectors": {"type": "array", "items": {"type": "string"}}})
                }
            }
        }
        findings, _ = scenario(proto, doc)
        self.assertEqual(findings, [], [f"{f.kind} {f.subject}" for f in findings])

    def test_cardinality_agreement_is_silent(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n repeated uint32 a = 1;\n map<string, uint32> b = 2;\n}\n'
        doc = {
            "components": {
                "schemas": {
                    "M": obj(
                        {
                            "a": {"type": "array", "items": {"type": "integer"}},
                            "b": {"type": "object", "additionalProperties": {"type": "integer"}},
                        }
                    )
                }
            }
        }
        findings, _ = scenario(proto, doc)
        self.assertEqual(findings, [])


# --------------------------------------------------------------------------
# Refusing to guess
# --------------------------------------------------------------------------


@needs_protobuf
class TestRefusesToGuess(unittest.TestCase):
    def test_ambiguous_short_name_is_not_matched(self):
        """The analogue of "searched by the short name instead of the full one":
        two messages sharing a short name must not be quietly folded into one
        schema."""
        proto = """
        syntax = "proto3";
        package t;
        message Params { uint32 a = 1; }
        message Outer { message Params { string b = 1; } }
        """
        doc = {"components": {"schemas": {"Params": obj({"a": {"type": "integer"}})}}}
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [])
        self.assertEqual(len(cov.ambiguous), 1)
        self.assertIn("Outer.Params", cov.ambiguous[0])

    def test_convention_differences_still_match(self):
        """A convention difference is no finding: config/conf and a number."""
        proto = 'syntax = "proto3"; package t;\nmessage M {\n uint32 vectors_config = 1;\n uint32 shards = 2;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"vectors_conf": {"type": "integer"}, "shard": {"type": "integer"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertEqual(findings, [])

    def test_ambiguous_field_normalisation_is_not_reported_as_missing(self):
        """Two names on one side reduce to the same key and which maps to which is
        unknown. Reporting two "losses" here is lying in our own favour."""
        proto = (
            'syntax = "proto3"; package t;\nmessage M {\n uint32 anchor = 1;\n'
            " uint32 vectors = 2;\n uint32 vectors_config = 3;\n}\n"
        )
        doc = {
            "components": {
                "schemas": {
                    "M": obj({"anchor": {"type": "integer"}, "vector_conf": {"type": "integer"}})
                }
            }
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [])
        self.assertEqual(len(cov.ambiguous_fields), 1)
        self.assertIn("vectors_config", cov.ambiguous_fields[0])

    def test_every_finding_carries_coordinates(self):
        proto = 'syntax = "proto3"; package t;\nmessage M {\n // Default is 1\n optional uint32 a = 1;\n uint32 gone = 2;\n}\n'
        doc = {"components": {"schemas": {"M": obj({"a": {"type": "integer", "default": 5}, "extra": {"type": "string"}})}}}
        findings, _ = scenario(proto, doc)
        self.assertTrue(findings)
        for f in findings:
            for ref in (f.proto_ref, f.openapi_ref):
                self.assertRegex(ref, r"^.+:\d+$", f"no coordinate: {f.kind} {f.subject}")
                self.assertNotEqual(ref.rsplit(":", 1)[1], "0", f"line number zero: {f.kind} {f.subject}")


# --------------------------------------------------------------------------
# Known truth about qdrant
# --------------------------------------------------------------------------


@unittest.skipUnless(HAS_QDRANT, "no qdrant clone in ~/Projects/oss/qdrant")
@needs_protobuf
class TestQdrantKnownTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings, cls.cov = ifd.run(QDRANT_PROTO, QDRANT_OPENAPI)
        cls.msgs = ifd.parse_proto(QDRANT_PROTO, [])

    def test_payload_schema_is_read_from_the_map_field(self):
        """collections.proto:802. A regex never saw this field and produced a
        false mismatch as its very first item."""
        f = {x.name: x for x in self.msgs["CollectionInfo"].fields}
        self.assertIn("payload_schema", f)
        self.assertEqual(f["payload_schema"].line, 802)
        self.assertTrue(f["payload_schema"].file.endswith("collections.proto"))
        self.assertEqual(f["payload_schema"].type_str, "map<string, PayloadSchemaInfo>")

    def test_no_default_divergence_anywhere(self):
        """Known truth: every default in qdrant agrees. A tool that finds a
        default mismatch here is broken."""
        bad = [f"{f.subject} ({f.proto_ref} / {f.openapi_ref}): {f.message}"
               for f in self.findings if f.kind == "default-mismatch"]
        self.assertEqual(bad, [], "\n".join(bad))

    def test_named_defaults_are_seen_and_agree(self):
        """Four defaults named one by one in the known truth."""
        checks = {
            ("CreateCollection", "shard_number"): 1.0,
            ("CreateCollection", "replication_factor"): 1.0,
            ("AcornSearchParams", "max_selectivity"): 0.4,
        }
        for (msg, fld), want in checks.items():
            f = {x.name: x for x in self.msgs[msg].fields}[fld]
            got = ifd.parse_default(f.comment)
            self.assertIsNotNone(got, f"{msg}.{fld}: the default in the comment was not read")
            self.assertEqual(got[1], want, f"{msg}.{fld}")
        # write_consistency_factor: the default is named in gRPC only and absent
        # from REST. A one-sided value cannot be compared and is no finding.
        f = {x.name: x for x in self.msgs["CreateCollection"].fields}["write_consistency_factor"]
        self.assertEqual(ifd.parse_default(f.comment)[1], 1.0)
        self.assertNotIn("CreateCollection.write_consistency_factor", subjects(self.findings))

    def test_naming_conventions_are_not_reported(self):
        """sparse_vectors_config against sparse_vectors and vectors_config against
        vectors are the norm of the formats. Almost all 85 "mismatches" of the
        naive parse were exactly this."""
        for name in ("vectors_config", "sparse_vectors_config", "vectors", "sparse_vectors"):
            hits = [f for f in self.findings
                    if f.kind.startswith("missing") and f.subject.endswith("." + name)]
            self.assertEqual(hits, [], f"{name}: {[h.subject for h in hits]}")

    def test_transport_fields_do_not_leak_into_findings(self):
        for name in ("collection_name", "timeout", "wait", "ordering"):
            hits = [f for f in self.findings if f.subject.endswith("." + name) and f.kind == "missing-in-openapi"]
            self.assertEqual(hits, [], f"{name}: {[h.subject for h in hits]}")

    def test_coverage_is_not_accidentally_empty(self):
        """An empty report also happens when the tool compared nothing."""
        self.assertGreater(self.cov.matched, 100, "suspiciously few pairs compared")
        self.assertGreater(self.cov.proto_messages, 250)
        self.assertGreater(self.cov.openapi_schemas, 300)

    def test_internal_services_are_excluded_and_said_so(self):
        """Node-to-node RPC is not described by the public REST surface, and
        namesakes from there build false pairs. The exclusion has to be stated
        out loud."""
        self.assertTrue(self.cov.excluded_files)
        self.assertIn("internal", self.cov.excluded_files[0])

    def test_response_envelope_does_not_leak(self):
        """gRPC keeps time and usage inside the response message, REST around result."""
        for f in self.findings:
            self.assertNotIn(
                f.subject.rsplit(".", 1)[-1],
                ("time", "usage", "status"),
                f"the response envelope leaked into the findings: {f.kind} {f.subject}",
            )

    def test_known_findings_survive(self):
        """Nine mismatches verified by hand against the qdrant clone. If the tool
        stops seeing them it lost the signal rather than got cleaner. The list may
        diverge from upstream after the clone is updated."""
        want = {
            "ShardTransferInfo.comment",
            "ShardTransferInfo.method",
            "SparseIndexConfig.index_type",
            "SparseIndexConfig.on_disk",
            "AbortShardTransfer.to_shard_id",
            "MoveShard.to_shard_id",
            "ReplicateShard.to_shard_id",
            "RestartTransfer.to_shard_id",
            "PointStruct.vectors",
        }
        got = {f.subject for f in self.findings if f.hard}
        self.assertEqual(want - got, set(), f"lost: {sorted(want - got)}")

    def test_noise_stays_low(self):
        """More than a dozen hard findings on qdrant means a broken tool rather
        than a harvest: naive comparison gave 85, the worked-out one gives nine."""
        hard = [f for f in self.findings if f.hard]
        self.assertLessEqual(
            len(hard), 15, "\n".join(f"{f.kind} {f.subject}" for f in hard)
        )

    def test_datetime_range_is_not_reported(self):
        """REST keeps DatetimeRange as a member of the RangeInterface union inside
        the range field. A different shape rather than a missing field."""
        self.assertNotIn("FieldCondition.datetime_range", subjects(self.findings))

    def test_every_finding_carries_real_coordinates(self):
        for f in self.findings:
            for ref in (f.proto_ref, f.openapi_ref):
                self.assertRegex(ref, r"^.+:\d+$", f"{f.kind} {f.subject}")
                self.assertNotEqual(ref.rsplit(":", 1)[1], "0", f"{f.kind} {f.subject}")


# --------------------------------------------------------------------------
# Swagger 2.0: the whole grpc-gateway family is described with it
# --------------------------------------------------------------------------


@needs_protobuf
class TestSwagger2(unittest.TestCase):
    def test_definitions_and_package_prefixed_names(self):
        """grpc-gateway puts schemas into definitions and writes the name with the
        package in front: etcdserverpbRangeRequest. Without handling that no pair
        is built at all."""
        proto = """
        syntax = "proto3";
        package etcdserverpb;
        message RangeRequest {
          bytes key = 1;
          int64 limit = 2;
          bool count_only = 3;
        }
        """
        doc = {
            "swagger": "2.0",
            "definitions": {
                "etcdserverpbRangeRequest": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "format": "byte"},
                        "limit": {"type": "string", "format": "int64"},
                        "count_only": {"type": "boolean"},
                    },
                }
            },
        }
        findings, cov = scenario(proto, doc)
        self.assertEqual(findings, [], [f"{f.kind} {f.subject}" for f in findings])
        self.assertEqual(cov.matched, 1)

    def test_swagger2_ref_and_x_nullable(self):
        proto = 'syntax = "proto3"; package pb;\nmessage Inner { int64 v = 1; }\nmessage M { Inner inner = 1; }\n'
        doc = {
            "swagger": "2.0",
            "definitions": {
                "pbInner": obj({"v": {"type": "string"}}),
                "pbM": obj({"inner": {"$ref": "#/definitions/pbInner", "x-nullable": True}}),
            },
        }
        schemas, _, _, _ = ifd.parse_openapi(_write_json(doc))
        self.assertEqual(schemas["pbM"].props["inner"].ref, "pbInner")
        self.assertTrue(schemas["pbM"].props["inner"].nullable)
        findings, _ = scenario(proto, doc)
        self.assertEqual(findings, [])


def _write_json(doc) -> str:
    td = tempfile.mkdtemp()
    p = os.path.join(td, "openapi.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return p


# --------------------------------------------------------------------------
# Negative control: a spec GENERATED from proto
# --------------------------------------------------------------------------


def _googleapis_include():
    try:
        import google.api  # noqa: F401
    except ImportError:
        return None
    import google

    for root in google.__path__:
        cand = os.path.join(root, "api", "annotations.proto")
        if os.path.isfile(cand):
            return os.path.dirname(root)  # the directory holding google/
    return None


ETCD = os.path.expanduser("~/Projects/oss/etcd")
ETCD_PROTO = os.path.join(ETCD, "api/etcdserverpb")
ETCD_SWAGGER = os.path.join(ETCD, "Documentation/dev-guide/apispec/swagger/rpc.swagger.json")
GAPI = _googleapis_include()
HAS_ETCD = os.path.isdir(ETCD_PROTO) and os.path.isfile(ETCD_SWAGGER) and GAPI


@unittest.skipUnless(HAS_ETCD, "no etcd clone or googleapis-common-protos")
@needs_protobuf
class TestGeneratedSpecIsSilent(unittest.TestCase):
    """In etcd the swagger file is generated from proto through grpc-gateway, so
    no mismatch can exist by construction and a correct tool has to report zero.
    This is a test against inventing findings."""

    def test_zero_findings_but_real_work_done(self):
        stubs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protoc-stubs")
        findings, cov = ifd.run(
            ETCD_PROTO,
            ETCD_SWAGGER,
            includes=[os.path.dirname(ETCD), GAPI, stubs],
            skip_proto_file="raft_internal",
        )
        self.assertGreater(cov.matched, 50, "nothing was compared, so zero says nothing")
        self.assertEqual(
            [f"{f.kind} {f.subject}" for f in findings],
            [],
            "a generated spec cannot hold findings",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
