from __future__ import annotations

import json

from lingtai_kernel.runtime_identity import runtime_identity, runtime_identity_event_fields


def test_runtime_identity_event_fields_are_json_serializable():
    fields = runtime_identity_event_fields()

    assert set(fields) == {"kernel_version", "kernel_runtime_stamp", "kernel_runtime"}
    assert fields["kernel_version"]
    assert fields["kernel_runtime_stamp"]
    assert fields["kernel_runtime"]["version"] == fields["kernel_version"]
    assert fields["kernel_runtime"]["stamp"] == fields["kernel_runtime_stamp"]
    json.dumps(fields)


def test_runtime_identity_is_cached():
    assert runtime_identity() is runtime_identity()
