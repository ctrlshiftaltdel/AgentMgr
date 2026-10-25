"""Pure contract tests for the Hermes-in-sandbox managed execution layer.

Everything here is DB-free, network-free and privilege-free.  The tests pin the
contracts that later tasks (root helper, workload, broker, adapter) must honour:

* deterministic unit/handle derivation and a sandbox spec that carries only
  hashes/sizes — never the raw prompt, profile or egress token;
* HMAC-signed task/runtime attestations with binding, expiry, sandbox-fact and
  evidence checks;
* a fail-closed artifact reader (``O_NOFOLLOW``, regular file, size cap,
  streaming SHA-256);
* a capability matrix that only becomes ``verified`` for a valid runtime
  attestation whose evidence reference matches the trusted configuration;
* the fixed managed prompt wrapper with a hard byte bound.

The independent canonical-JSON signer below is intentionally *not* imported
from the module under test: it pins the signed document format.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    CapabilityStatus,
    RuntimeCapability,
    RuntimeType,
    VersionSource,
)
from app.runtime.hermes_egress import MAX_EGRESS_TTL_SECONDS, egress_token_hash
from app.runtime.hermes_managed import (
    DEFAULT_PROMPT_MAX_BYTES,
    MANAGED_INSTRUCTION,
    RUNTIME_ATTESTATION_SCHEMA,
    TASK_ATTESTATION_SCHEMA,
    ArtifactError,
    AttestationError,
    HermesManagedConfig,
    PromptError,
    RuntimeAttestation,
    SandboxSpecError,
    SandboxStatus,
    SandboxStop,
    TaskAttestation,
    build_managed_prompt,
    build_sandbox_spec,
    managed_capabilities,
    read_artifact,
    runtime_handle_for,
    unit_name_for,
    verify_runtime_attestation,
    verify_task_attestation,
)

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
KEY = b"a" * 32
OTHER_KEY = b"b" * 32
RUNTIME_ROOT_HASH = "sha256:" + "1" * 64
EVIDENCE_REF = "evidence:hermes:managed:test-0001"
HELPER_VERSION = "1.0.0"

SANDBOX_FACTS = {
    "private_network": True,
    "protect_home": True,
    "protect_system": "strict",
    "no_new_privileges": True,
    "capability_bounding_set": "empty",
    "toolsets": ["terminal"],
    "external_ip": False,
}

MANAGED_CAPABILITIES = (
    RuntimeCapability.TASK_SUBMIT,
    RuntimeCapability.TASK_LOOKUP,
    RuntimeCapability.TASK_STATUS,
    RuntimeCapability.TASK_CANCEL,
    RuntimeCapability.TASK_RESULT,
    RuntimeCapability.RUNTIME_HEALTH,
    RuntimeCapability.RUNTIME_ISOLATION,
    RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS,
    RuntimeCapability.RUNTIME_TOOL_PERMISSIONS,
    RuntimeCapability.RUNTIME_MANAGED_EXECUTION,
)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(document: dict[str, object]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sign(document: dict[str, object], *, key: bytes = KEY) -> str:
    return hmac.new(key, _canonical(document), hashlib.sha256).hexdigest()


def make_config(tmp_path: Path) -> HermesManagedConfig:
    return HermesManagedConfig(
        helper_path=tmp_path / "agentmgr-hermes-exec",
        runtime_root=tmp_path / "runtime",
        task_root=tmp_path / "tasks",
        broker_socket=tmp_path / "egress.sock",
        attestation_key_path=tmp_path / "attestation.key",
        egress_token_key_path=tmp_path / "egress.key",
        runtime_attestation_path=tmp_path / "runtime-attestation.json",
        evidence_ref=EVIDENCE_REF,
        runtime_name="hermes",
        runtime_version="0.22.1",
        model_name="model-x",
        egress_provider_host="provider.example",
        worker_id="worker-1",
    )


def make_spec(config: HermesManagedConfig, **overrides: object):
    values: dict[str, object] = {
        "task_id": "task_abc",
        "grant_id": "grnt_abc",
        "idempotency_key": "idem_abc",
        "prompt": "PROMPT-CANARY",
        "profile_template": "PROFILE-CANARY",
        "egress_token": "egt.canary",
    }
    values.update(overrides)
    return build_sandbox_spec(config, **values)  # type: ignore[arg-type]


def task_payload(spec, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": TASK_ATTESTATION_SCHEMA,
        "unit": spec.unit,
        "task_id": spec.task_id,
        "grant_id": spec.grant_id,
        "runtime_handle": spec.runtime_handle,
        "uid": 12345,
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "profile_template_sha256": spec.profile_template_sha256,
        "prompt_sha256": spec.prompt_sha256,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "issued_at": _iso_utc(NOW),
        "expires_at": _iso_utc(NOW + timedelta(seconds=300)),
        "nonce": "n" * 32,
    }
    document.update(overrides)
    return document


def make_task_attestation(spec, *, key: bytes = KEY, **overrides: object) -> TaskAttestation:
    document = task_payload(spec, **overrides)
    return TaskAttestation.from_document({**document, "hmac": _sign(document, key=key)})


def runtime_payload(config: HermesManagedConfig, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "helper_version": HELPER_VERSION,
        "runtime_name": config.runtime_name,
        "runtime_version": config.runtime_version,
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "broker_socket_ok": True,
        "evidence_ref": config.evidence_ref,
        "issued_at": _iso_utc(NOW),
        "expires_at": _iso_utc(NOW + timedelta(hours=1)),
        "nonce": "m" * 32,
    }
    document.update(overrides)
    return document


def make_runtime_attestation(
    config: HermesManagedConfig, *, key: bytes = KEY, **overrides: object
) -> RuntimeAttestation:
    document = runtime_payload(config, **overrides)
    return RuntimeAttestation.from_document({**document, "hmac": _sign(document, key=key)})


# ---------------------------------------------------------------------------
# unit / handle derivation
# ---------------------------------------------------------------------------


def test_unit_and_handle_are_derived_and_pattern_safe():
    unit = unit_name_for("task_abc", "idem_abc")

    assert re.fullmatch(r"agentmgr-hermes-[0-9a-f]{16}", unit)
    assert unit_name_for("task_abc", "idem_abc") == unit
    assert unit_name_for("task_abc", "idem_other") != unit
    assert unit_name_for("task_other", "idem_abc") != unit

    handle = runtime_handle_for(unit)
    assert re.fullmatch(r"rtask_hermes_[A-Za-z0-9_-]{4,63}", handle)
    assert runtime_handle_for(unit) == handle
    assert runtime_handle_for(unit_name_for("task_abc", "idem_other")) != handle
    # The handle is domain separated, not just a digest of the unit name.
    assert handle != "rtask_hermes_" + hashlib.sha256(unit.encode()).hexdigest()[:32]


def test_unit_name_contains_no_caller_input():
    unit = unit_name_for("../../etc/passwd", "idem/key with spaces")
    assert unit.startswith("agentmgr-hermes-")
    assert re.fullmatch(r"agentmgr-hermes-[0-9a-f]{16}", unit)
    assert "passwd" not in unit


@pytest.mark.parametrize(
    ("task_id", "idempotency_key"),
    [("", "idem"), ("task", ""), ("   ", "idem"), ("task", "  "), (None, "idem"), ("task", None)],
)
def test_unit_name_rejects_empty_bindings(task_id, idempotency_key):
    with pytest.raises(SandboxSpecError):
        unit_name_for(task_id, idempotency_key)


def test_runtime_handle_rejects_empty_or_unsafe_units():
    for unit in ("", "   ", None, "agentmgr hermes", "agentmgr-hermes-abc\n"):
        with pytest.raises(SandboxSpecError):
            runtime_handle_for(unit)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_config_carries_all_managed_paths_and_limits(tmp_path):
    config = make_config(tmp_path)

    assert config.task_root == tmp_path / "tasks"
    assert config.runtime_attestation_path == tmp_path / "runtime-attestation.json"
    assert config.egress_token_key_path == tmp_path / "egress.key"
    assert config.egress_provider_host == "provider.example"
    assert config.egress_path_prefix == "/v1"
    assert config.local_proxy_base_url_template == "http://127.0.0.1:{port}/v1"
    assert config.worker_id == "worker-1"
    assert config.lease_seconds > 0
    assert config.poll_interval > 0
    assert config.health_backoff_seconds > 0
    assert 1 <= config.egress_token_ttl_seconds <= MAX_EGRESS_TTL_SECONDS
    assert config.prompt_max_bytes == DEFAULT_PROMPT_MAX_BYTES

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.worker_id = "worker-2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# sandbox spec
# ---------------------------------------------------------------------------


def test_sandbox_spec_is_deterministic_and_bounded(tmp_path):
    config = make_config(tmp_path)
    spec = make_spec(config)

    assert spec.unit == unit_name_for("task_abc", "idem_abc")
    assert spec.runtime_handle == runtime_handle_for(spec.unit)
    assert spec.task_dir == config.task_root / spec.unit
    assert spec.prompt_sha256 == hashlib.sha256(b"PROMPT-CANARY").hexdigest()
    assert spec.prompt_size == len("PROMPT-CANARY")
    assert spec.profile_template_sha256 == hashlib.sha256(b"PROFILE-CANARY").hexdigest()
    assert spec.egress_token_sha256 == egress_token_hash("egt.canary")
    assert spec.ttl_seconds == config.egress_token_ttl_seconds
    assert spec.runtime_max_sec == config.runtime_max_sec
    assert spec.memory_max_mb == config.memory_max_mb
    assert spec.cpu_quota_percent == config.cpu_quota_percent
    assert spec.tasks_max == config.tasks_max
    assert "PROMPT-CANARY" not in repr(spec)
    assert "PROFILE-CANARY" not in repr(spec)
    assert "egt.canary" not in repr(spec)


def test_sandbox_spec_is_stable_across_calls(tmp_path):
    config = make_config(tmp_path)

    assert make_spec(config) == make_spec(config)
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_spec(config).unit = "other"  # type: ignore[misc]


def test_sandbox_spec_changes_with_idempotency_key(tmp_path):
    config = make_config(tmp_path)
    first = make_spec(config)
    second = make_spec(config, idempotency_key="idem_other")

    assert first.unit != second.unit
    assert first.task_dir != second.task_dir
    assert first.runtime_handle != second.runtime_handle


def test_sandbox_spec_hashes_utf8_bytes(tmp_path):
    config = make_config(tmp_path)
    spec = make_spec(config, prompt="é")

    assert spec.prompt_size == 2
    assert spec.prompt_sha256 == hashlib.sha256("é".encode("utf-8")).hexdigest()


def test_sandbox_spec_uses_configured_ttl(tmp_path):
    config = dataclasses.replace(make_config(tmp_path), egress_token_ttl_seconds=60)
    assert make_spec(config).ttl_seconds == 60


def test_sandbox_spec_rejects_oversized_prompt(tmp_path):
    config = dataclasses.replace(make_config(tmp_path), prompt_max_bytes=5)

    with pytest.raises(SandboxSpecError):
        make_spec(config, prompt="too long")


def test_sandbox_spec_allows_prompt_at_limit(tmp_path):
    config = dataclasses.replace(make_config(tmp_path), prompt_max_bytes=5)
    spec = make_spec(config, prompt="12345")

    assert spec.prompt_size == 5


def test_sandbox_spec_rejects_bad_ttl(tmp_path):
    for ttl in (0, -1, MAX_EGRESS_TTL_SECONDS + 1):
        config = dataclasses.replace(make_config(tmp_path), egress_token_ttl_seconds=ttl)
        with pytest.raises(SandboxSpecError):
            make_spec(config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": ""},
        {"grant_id": ""},
        {"idempotency_key": ""},
        {"prompt": ""},
        {"profile_template": ""},
        {"egress_token": ""},
        {"prompt": None},
        {"profile_template": None},
        {"egress_token": None},
        {"egress_token": "e" * 5000},
    ],
)
def test_sandbox_spec_rejects_invalid_inputs(tmp_path, overrides):
    config = make_config(tmp_path)

    with pytest.raises(SandboxSpecError):
        make_spec(config, **overrides)


# ---------------------------------------------------------------------------
# managed prompt
# ---------------------------------------------------------------------------


def test_managed_prompt_prepends_fixed_instruction():
    prompt = "Summarise the input and write the result."
    wrapped = build_managed_prompt(prompt)

    assert wrapped.startswith(MANAGED_INSTRUCTION)
    assert wrapped.endswith(prompt)
    assert "out/result.txt" in MANAGED_INSTRUCTION
    assert "current working directory" in MANAGED_INSTRUCTION
    assert "network" in MANAGED_INSTRUCTION
    assert "terminal" in MANAGED_INSTRUCTION
    assert build_managed_prompt(prompt) == wrapped


def test_managed_prompt_enforces_byte_limit():
    base = len(build_managed_prompt("x").encode("utf-8"))

    assert build_managed_prompt("x", max_bytes=base)
    with pytest.raises(PromptError):
        build_managed_prompt("x", max_bytes=base - 1)
    with pytest.raises(PromptError):
        build_managed_prompt("x" * 10, max_bytes=base)


def test_managed_prompt_counts_utf8_bytes():
    base = len(build_managed_prompt("é").encode("utf-8"))

    assert build_managed_prompt("é", max_bytes=base)
    with pytest.raises(PromptError):
        build_managed_prompt("é", max_bytes=base - 1)


@pytest.mark.parametrize("prompt", ["", "   ", None, 1])
def test_managed_prompt_rejects_empty_or_non_text(prompt):
    with pytest.raises(PromptError):
        build_managed_prompt(prompt)


@pytest.mark.parametrize("max_bytes", [0, -1])
def test_managed_prompt_rejects_invalid_limit(max_bytes):
    with pytest.raises(PromptError):
        build_managed_prompt("prompt", max_bytes=max_bytes)


def test_managed_prompt_defaults_to_the_configured_ceiling():
    assert build_managed_prompt("x", max_bytes=None)
    assert build_managed_prompt("x" * (DEFAULT_PROMPT_MAX_BYTES // 2))
    with pytest.raises(PromptError):
        build_managed_prompt("x" * DEFAULT_PROMPT_MAX_BYTES)


# ---------------------------------------------------------------------------
# artifact reader
# ---------------------------------------------------------------------------


def _write_result(tmp_path: Path, data: bytes) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    target = out / "result.txt"
    target.write_bytes(data)
    return target


def test_artifact_reader_accepts_regular_file(tmp_path):
    data = b"OK\n"
    target = _write_result(tmp_path, data)

    result = read_artifact(target, max_bytes=1024)

    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)
    assert result.relative_path == "out/result.txt"


def test_artifact_reader_streams_large_files(tmp_path):
    data = (b"0123456789abcdef" * 20000) + b"tail"
    target = _write_result(tmp_path, data)

    result = read_artifact(target, max_bytes=len(data))

    assert result.size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()


def test_artifact_reader_rejects_oversize(tmp_path):
    target = _write_result(tmp_path, b"0123456789")

    assert read_artifact(target, max_bytes=10).size == 10
    with pytest.raises(ArtifactError):
        read_artifact(target, max_bytes=9)


def test_artifact_reader_rejects_symlink(tmp_path):
    target = _write_result(tmp_path, b"PAYLOAD")
    link = tmp_path / "out" / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ArtifactError) as excinfo:
        read_artifact(link, max_bytes=1024)

    assert str(tmp_path) not in str(excinfo.value)


def test_artifact_reader_rejects_dangling_symlink(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    link = out / "result.txt"
    link.symlink_to(out / "missing.txt")

    with pytest.raises(ArtifactError):
        read_artifact(link, max_bytes=1024)


def test_artifact_reader_rejects_empty_file(tmp_path):
    target = _write_result(tmp_path, b"")

    with pytest.raises(ArtifactError) as excinfo:
        read_artifact(target, max_bytes=1024)

    assert str(tmp_path) not in str(excinfo.value)


def test_artifact_reader_rejects_non_regular_file(tmp_path):
    directory = tmp_path / "out"
    directory.mkdir()

    with pytest.raises(ArtifactError):
        read_artifact(directory, max_bytes=1024)


def test_artifact_reader_rejects_missing_file(tmp_path):
    with pytest.raises(ArtifactError):
        read_artifact(tmp_path / "out" / "result.txt", max_bytes=1024)


@pytest.mark.parametrize("max_bytes", [0, -1, None, "1024"])
def test_artifact_reader_rejects_invalid_limit(tmp_path, max_bytes):
    target = _write_result(tmp_path, b"data")

    with pytest.raises(ArtifactError):
        read_artifact(target, max_bytes=max_bytes)


# ---------------------------------------------------------------------------
# task attestation
# ---------------------------------------------------------------------------


def test_task_attestation_document_roundtrip(tmp_path):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec)
    document = attestation.to_document()

    assert set(document) == {
        "schema",
        "unit",
        "task_id",
        "grant_id",
        "runtime_handle",
        "uid",
        "runtime_root_hash",
        "profile_template_sha256",
        "prompt_sha256",
        "sandbox_facts",
        "issued_at",
        "expires_at",
        "nonce",
        "hmac",
    }
    assert TaskAttestation.from_document(document) == attestation
    serialized = json.dumps(document)
    assert "PROMPT-CANARY" not in serialized
    assert "PROFILE-CANARY" not in serialized
    assert "egt.canary" not in serialized


def test_task_attestation_verifies(tmp_path):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec)

    assert verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW) is None
    assert verify_task_attestation(
        attestation, key=KEY, spec=spec, now=NOW + timedelta(seconds=299)
    ) is None


def test_task_attestation_rejects_wrong_key_and_tamper(tmp_path):
    spec = make_spec(make_config(tmp_path))

    with pytest.raises(AttestationError):
        verify_task_attestation(
            make_task_attestation(spec, key=OTHER_KEY), key=KEY, spec=spec, now=NOW
        )

    valid = make_task_attestation(spec)
    document = valid.to_document()
    document["uid"] = 54321
    tampered = TaskAttestation.from_document(document)
    with pytest.raises(AttestationError):
        verify_task_attestation(tampered, key=KEY, spec=spec, now=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"unit": "agentmgr-hermes-0000000000000000"},
        {"task_id": "task_other"},
        {"grant_id": "grnt_other"},
        {"runtime_handle": "rtask_hermes_other"},
        {"prompt_sha256": "0" * 64},
        {"profile_template_sha256": "0" * 64},
    ],
    ids=["unit", "task", "grant", "handle", "prompt", "profile"],
)
def test_task_attestation_rejects_binding_mismatch(tmp_path, overrides):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec, **overrides)

    with pytest.raises(AttestationError):
        verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW)


def test_task_attestation_rejects_wrong_task_directory(tmp_path):
    config = make_config(tmp_path)
    spec = make_spec(config)
    wrong_dir = dataclasses.replace(spec, task_dir=config.task_root / "not-the-unit")

    with pytest.raises(AttestationError):
        verify_task_attestation(
            make_task_attestation(spec), key=KEY, spec=wrong_dir, now=NOW
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "agentmgr.hermes.sandbox.v2"},
        {"schema": ""},
        {"issued_at": _iso_utc(NOW + timedelta(seconds=10))},
        {"expires_at": _iso_utc(NOW)},
        {"expires_at": _iso_utc(NOW - timedelta(seconds=1))},
        {"uid": 0},
        {"uid": -1},
        {"uid": "12345"},
        {"uid": True},
        {"runtime_root_hash": ""},
        {"runtime_root_hash": "deadbeef"},
        {"runtime_root_hash": "sha256:xyz"},
        {"nonce": ""},
        {"prompt_sha256": ""},
        {"profile_template_sha256": ""},
    ],
)
def test_task_attestation_rejects_bad_fields(tmp_path, overrides):
    spec = make_spec(make_config(tmp_path))

    with pytest.raises(AttestationError):
        attestation = make_task_attestation(spec, **overrides)
        verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW)


@pytest.mark.parametrize(
    "sandbox_facts",
    [
        {**SANDBOX_FACTS, "private_network": False},
        {**SANDBOX_FACTS, "protect_home": False},
        {**SANDBOX_FACTS, "protect_system": "full"},
        {**SANDBOX_FACTS, "no_new_privileges": False},
        {**SANDBOX_FACTS, "capability_bounding_set": "full"},
        {**SANDBOX_FACTS, "toolsets": ["terminal", "web"]},
        {**SANDBOX_FACTS, "external_ip": True},
        {**SANDBOX_FACTS, "private_network": 1},
        {key: value for key, value in SANDBOX_FACTS.items() if key != "toolsets"},
    ],
)
def test_task_attestation_rejects_unsafe_sandbox_facts(tmp_path, sandbox_facts):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec, sandbox_facts=sandbox_facts)

    with pytest.raises(AttestationError):
        verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW)


def test_task_attestation_accepts_json_roundtripped_document(tmp_path):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec)
    document = json.loads(json.dumps(attestation.to_document()))
    roundtripped = TaskAttestation.from_document(document)

    assert roundtripped == attestation
    assert verify_task_attestation(roundtripped, key=KEY, spec=spec, now=NOW) is None


@pytest.mark.parametrize("hmac_value", ["", "0" * 63, "0" * 65, "z" * 64, "0" * 64])
def test_task_attestation_rejects_bad_hmac(tmp_path, hmac_value):
    spec = make_spec(make_config(tmp_path))
    document = task_payload(spec)
    attestation = TaskAttestation.from_document({**document, "hmac": hmac_value})

    with pytest.raises(AttestationError):
        verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW)


def test_task_attestation_hmac_is_canonical_json():
    spec = SandboxSpecForCanonicalJson()
    document = {
        "schema": TASK_ATTESTATION_SCHEMA,
        "unit": spec.unit,
        "task_id": spec.task_id,
        "grant_id": spec.grant_id,
        "runtime_handle": spec.runtime_handle,
        "uid": 12345,
        "runtime_root_hash": RUNTIME_ROOT_HASH,
        "profile_template_sha256": spec.profile_template_sha256,
        "prompt_sha256": spec.prompt_sha256,
        "sandbox_facts": dict(SANDBOX_FACTS),
        "issued_at": _iso_utc(NOW),
        "expires_at": _iso_utc(NOW + timedelta(seconds=300)),
        "nonce": "n" * 32,
    }
    signature = _sign(document)
    # Pinned canonical-JSON vector: sorted keys, compact separators, ASCII.
    expected_input = (
        '{"expires_at":"2026-10-25T12:05:00Z","grant_id":"grnt_abc",'
        '"issued_at":"2026-10-25T12:00:00Z","nonce":"' + "n" * 32 + '",'
        '"profile_template_sha256":"' + spec.profile_template_sha256 + '",'
        '"prompt_sha256":"' + spec.prompt_sha256 + '",'
        '"runtime_handle":"' + spec.runtime_handle + '",'
        '"runtime_root_hash":"' + RUNTIME_ROOT_HASH + '",'
        '"sandbox_facts":{"capability_bounding_set":"empty","external_ip":false,'
        '"no_new_privileges":true,"private_network":true,"protect_home":true,'
        '"protect_system":"strict","toolsets":["terminal"]},'
        '"schema":"' + TASK_ATTESTATION_SCHEMA + '",'
        '"task_id":"task_abc","uid":12345,"unit":"' + spec.unit + '"}'
    )
    assert _canonical(document).decode("utf-8") == expected_input

    attestation = TaskAttestation.from_document({**document, "hmac": signature})
    assert verify_task_attestation(attestation, key=KEY, spec=spec, now=NOW) is None


class SandboxSpecForCanonicalJson:
    """Minimal spec-shaped object with pinned values for the vector test."""

    unit = "agentmgr-hermes-0123456789abcdef"
    runtime_handle = "rtask_hermes_0123456789abcdef0123456789abcdef"
    task_id = "task_abc"
    grant_id = "grnt_abc"
    prompt_sha256 = "2" * 64
    profile_template_sha256 = "3" * 64
    task_dir = Path("/task-root") / unit


def test_task_attestation_document_parser_rejects_unknown_keys(tmp_path):
    spec = make_spec(make_config(tmp_path))
    document = task_payload(spec)
    document["hmac"] = _sign(document)

    with pytest.raises(AttestationError):
        TaskAttestation.from_document({**document, "extra": 1})
    without_uid = {key: value for key, value in document.items() if key != "uid"}
    with pytest.raises(AttestationError):
        TaskAttestation.from_document(without_uid)
    with pytest.raises(AttestationError):
        TaskAttestation.from_document(["not", "a", "document"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"issued_at": "not-a-timestamp"},
        {"issued_at": "2026-10-25T12:00:00"},
        {"expires_at": "2026-10-25T12:00:00"},
        {"unit": ""},
        {"task_id": ""},
        {"grant_id": ""},
        {"runtime_handle": ""},
        {"nonce": ""},
        {"hmac": 12345},
        {"sandbox_facts": ["not", "a", "mapping"]},
    ],
)
def test_task_attestation_document_parser_rejects_bad_documents(tmp_path, overrides):
    spec = make_spec(make_config(tmp_path))
    document = task_payload(spec)
    document["hmac"] = _sign(document)
    document.update(overrides)

    with pytest.raises(AttestationError):
        TaskAttestation.from_document(document)


def test_task_attestation_isolated_from_caller_mutation(tmp_path):
    spec = make_spec(make_config(tmp_path))
    attestation = make_task_attestation(spec)
    document = attestation.to_document()
    document["sandbox_facts"]["external_ip"] = True  # type: ignore[index]

    assert attestation.to_document()["sandbox_facts"]["external_ip"] is False


# ---------------------------------------------------------------------------
# runtime attestation
# ---------------------------------------------------------------------------


def test_runtime_attestation_document_roundtrip(tmp_path):
    config = make_config(tmp_path)
    attestation = make_runtime_attestation(config)
    document = attestation.to_document()

    assert set(document) == {
        "schema",
        "helper_version",
        "runtime_name",
        "runtime_version",
        "runtime_root_hash",
        "sandbox_facts",
        "broker_socket_ok",
        "evidence_ref",
        "issued_at",
        "expires_at",
        "nonce",
        "hmac",
    }
    assert RuntimeAttestation.from_document(document) == attestation
    assert json.dumps(document)


def test_runtime_attestation_verifies(tmp_path):
    config = make_config(tmp_path)

    assert (
        verify_runtime_attestation(
            make_runtime_attestation(config), key=KEY, config=config, now=NOW
        )
        is None
    )


def test_runtime_attestation_rejects_wrong_key_and_tamper(tmp_path):
    config = make_config(tmp_path)

    with pytest.raises(AttestationError):
        verify_runtime_attestation(
            make_runtime_attestation(config, key=OTHER_KEY),
            key=KEY,
            config=config,
            now=NOW,
        )

    document = make_runtime_attestation(config).to_document()
    document["helper_version"] = "9.9.9"
    with pytest.raises(AttestationError):
        verify_runtime_attestation(
            RuntimeAttestation.from_document(document), key=KEY, config=config, now=NOW
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "agentmgr.hermes.runtime.v2"},
        {"runtime_name": "other"},
        {"runtime_version": "9.9.9"},
        {"helper_version": ""},
        {"runtime_root_hash": ""},
        {"runtime_root_hash": "deadbeef"},
        {"evidence_ref": ""},
        {"evidence_ref": "evidence:other"},
        {"broker_socket_ok": False},
        {"broker_socket_ok": "yes"},
        {"issued_at": _iso_utc(NOW + timedelta(seconds=10))},
        {"expires_at": _iso_utc(NOW)},
        {"nonce": ""},
    ],
)
def test_runtime_attestation_rejects_bad_fields(tmp_path, overrides):
    config = make_config(tmp_path)

    with pytest.raises(AttestationError):
        attestation = make_runtime_attestation(config, **overrides)
        verify_runtime_attestation(attestation, key=KEY, config=config, now=NOW)


def test_runtime_attestation_rejects_unsafe_sandbox_facts(tmp_path):
    config = make_config(tmp_path)

    for facts in (
        {**SANDBOX_FACTS, "external_ip": True},
        {**SANDBOX_FACTS, "private_network": False},
        {**SANDBOX_FACTS, "toolsets": ["terminal", "browser"]},
    ):
        attestation = make_runtime_attestation(config, sandbox_facts=facts)
        with pytest.raises(AttestationError):
            verify_runtime_attestation(attestation, key=KEY, config=config, now=NOW)


@pytest.mark.parametrize("hmac_value", ["", "0" * 64, "0" * 63, "g" * 64])
def test_runtime_attestation_rejects_bad_hmac(tmp_path, hmac_value):
    config = make_config(tmp_path)
    document = runtime_payload(config)
    attestation = RuntimeAttestation.from_document({**document, "hmac": hmac_value})

    with pytest.raises(AttestationError):
        verify_runtime_attestation(attestation, key=KEY, config=config, now=NOW)


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_are_unverified_without_runtime_attestation(tmp_path):
    config = make_config(tmp_path)
    caps = managed_capabilities(config, None, now=NOW)

    for name in (
        RuntimeCapability.TASK_SUBMIT,
        RuntimeCapability.TASK_STATUS,
        RuntimeCapability.RUNTIME_ISOLATION,
        RuntimeCapability.RUNTIME_MANAGED_EXECUTION,
    ):
        capability = caps.capability(name)
        assert capability is not None
        assert capability.status is CapabilityStatus.UNVERIFIED
        assert capability.evidence_ref is None
        assert capability.limitations

    assert caps.capability(RuntimeCapability.TASK_EVENTS_POLL).status is CapabilityStatus.UNVERIFIED
    assert (
        caps.capability(RuntimeCapability.TASK_INPUT_PROVIDE).status
        is CapabilityStatus.UNVERIFIED
    )


def test_capabilities_matrix_covers_every_contract_capability(tmp_path):
    config = make_config(tmp_path)
    caps = managed_capabilities(config, None, now=NOW)

    assert {capability.name for capability in caps.capabilities} == {
        capability.value for capability in RuntimeCapability
    }
    assert caps.adapter_contract_version == ADAPTER_CONTRACT_VERSION
    assert caps.boundary == BOUNDARY
    assert caps.runtime_type is RuntimeType.HERMES
    assert caps.runtime_name == config.runtime_name
    assert caps.runtime_version == config.runtime_version
    assert caps.version_source is VersionSource.OPERATOR_VERIFIED
    assert caps.observed_at == NOW


def test_capabilities_with_attestation_and_evidence_are_verified(tmp_path):
    config = make_config(tmp_path)
    caps = managed_capabilities(config, make_runtime_attestation(config), now=NOW)

    for name in MANAGED_CAPABILITIES:
        capability = caps.capability(name)
        assert capability is not None, name
        assert capability.status is CapabilityStatus.VERIFIED, name
        assert capability.evidence_ref == config.evidence_ref
        assert capability.version == ADAPTER_CONTRACT_VERSION

    assert caps.capability(RuntimeCapability.RUNTIME_ISOLATION).limitations
    assert caps.capability(RuntimeCapability.RUNTIME_TASK_SCOPED_CREDENTIALS).limitations
    assert caps.capability(RuntimeCapability.RUNTIME_TOOL_PERMISSIONS).limitations
    assert caps.capability(RuntimeCapability.TASK_EVENTS_POLL).status is CapabilityStatus.UNVERIFIED
    assert caps.capability(RuntimeCapability.TASK_INPUT_PROVIDE).status is CapabilityStatus.UNVERIFIED
    assert caps.capability(RuntimeCapability.TASK_APPROVAL_RESOLVE).status is CapabilityStatus.UNVERIFIED
    assert caps.capability(RuntimeCapability.TASK_UNKNOWN_RESOLVE).status is CapabilityStatus.UNVERIFIED


def test_capabilities_fail_closed_without_matching_evidence(tmp_path):
    config = make_config(tmp_path)
    other_config = dataclasses.replace(config, evidence_ref="evidence:other")
    caps = managed_capabilities(config, make_runtime_attestation(other_config), now=NOW)

    for name in MANAGED_CAPABILITIES:
        assert caps.capability(name).status is CapabilityStatus.UNVERIFIED


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_at": _iso_utc(NOW)},
        {"hmac": ""},
        {"hmac": "not-a-signature"},
        {"runtime_version": "9.9.9"},
        {"runtime_name": "other"},
        {"broker_socket_ok": False},
        {"sandbox_facts": {**SANDBOX_FACTS, "external_ip": True}},
    ],
    ids=[
        "expired",
        "blank-hmac",
        "malformed-hmac",
        "wrong-version",
        "wrong-name",
        "broker-down",
        "unsafe-facts",
    ],
)
def test_capabilities_fail_closed_for_untrusted_runtime_attestation(tmp_path, overrides):
    config = make_config(tmp_path)
    document = runtime_payload(config)
    document.update(overrides)
    if "hmac" not in overrides:
        document["hmac"] = _sign(document)
    attestation = RuntimeAttestation.from_document(document)

    caps = managed_capabilities(config, attestation, now=NOW)

    for name in MANAGED_CAPABILITIES:
        assert caps.capability(name).status is CapabilityStatus.UNVERIFIED


# ---------------------------------------------------------------------------
# supervisor-facing status/stop contracts (used by Task 5)
# ---------------------------------------------------------------------------


def test_sandbox_status_and_stop_are_frozen_contracts():
    observed = NOW
    status = SandboxStatus(
        unit="agentmgr-hermes-0123456789abcdef",
        task_id="task_abc",
        runtime_handle="rtask_hermes_0123456789abcdef0123456789abcdef",
        active_state="active",
        sub_state="running",
        main_pid=4321,
        result=None,
        exit_status=None,
        started=True,
        heartbeat_at=observed,
        exit_code=None,
        timed_out=False,
        cancelled=False,
        process_gone=False,
        observed_at=observed,
    )
    stop = SandboxStop(
        unit=status.unit,
        active_state="inactive",
        result="success",
        exit_status=0,
        main_pid=0,
        process_gone=True,
        stop_confirmed=True,
        observed_at=observed,
    )

    assert status.active_state == "active"
    assert status.started is True
    assert stop.stop_confirmed is True
    assert stop.process_gone is True

    with pytest.raises(dataclasses.FrozenInstanceError):
        status.active_state = "inactive"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stop.stop_confirmed = False  # type: ignore[misc]
