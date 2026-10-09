import hashlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from agent_bridge import Bridge
from agent_bridge.hermes import (
    HERMES_DESTINATION,
    HERMES_REGISTRATION,
    ContractFile,
    HermesConfig,
    HermesError,
    HermesGateway,
    HermesRunner,
    delegation_request,
)
from agent_bridge.protocol import BridgeError, json_dumps

from tests.support import BridgeTestCase

MODEL = "gemma4:12b-it-qat"


class FakeGateway:
    """Audited wire contract only; no Hermes process/provider is present."""

    def __init__(self):
        self.calls = []
        self.health_body = {
            "ok": True,
            "service": "hermes-agent-delegate-gateway",
            "primary": "native",
            "models": {"offline": MODEL},
            "hermesHome": "PRIVATE_HOME",
        }
        self.delegate_body = {
            "ok": True,
            "completed": True,
            "runStatus": "completed",
            "exitCode": 0,
            "backendUsed": "wsl",
            "providerUsed": "custom",
            "modelRoute": MODEL,
            "modelUsed": MODEL,
            "fallbackChain": [MODEL],
            "response": "Synthetic result 😀",
            "stderr": "RAW_PRIVATE_STDERR",
            "sessionId": "PRIVATE_SESSION",
        }
        self.status = 200
        self.delegate_delay = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.calls.append(("GET", self.path, None))
                self.reply(owner.health_body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(("POST", self.path, body))
                threading.Event().wait(owner.delegate_delay)
                self.reply(owner.delegate_body)

            def reply(self, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # A synthetic timed-out client has closed its socket.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def delegations(self):
        return [call for call in self.calls if call[0] == "POST"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HermesRunnerTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeGateway()
        self.addCleanup(self.fake.close)
        pins = []
        for role in ("gateway", "router", "launcher", "wsl_policy", "wsl_fallback"):
            path = self.root / f"synthetic-{role}.txt"
            path.write_text(f"reviewed fake {role}", encoding="utf-8")
            pins.append(
                (
                    role,
                    ContractFile(path, hashlib.sha256(path.read_bytes()).hexdigest()),
                )
            )
        self.config = HermesConfig(self.fake.endpoint, MODEL, tuple(pins), 1.0)
        self.gateway = HermesGateway(self.config)
        self.runner = HermesRunner(self.bridge, self.gateway)

    def handoff(
        self,
        instructions="Validate synthetic explicit input",
        destination=HERMES_DESTINATION,
    ):
        task = self.bridge.task(
            source="owner",
            destination=destination,
            title="Synthetic task",
            description="Synthetic explicit task",
        )
        return self.bridge.handoff(
            task.task_id,
            source="owner",
            destination=destination,
            instructions=instructions,
            context={"ignored_history": "DO_NOT_FORWARD"},
        )

    def test_startup_and_registration_are_idle(self):
        registry = {"codex:worker": "existing codex runner"}
        preview = HERMES_REGISTRATION.preview(registry)
        self.assertEqual(
            preview,
            {
                "destination": HERMES_DESTINATION,
                "registration_valid": True,
                "runtime_required": "hermes local gateway runner",
                "applied": False,
            },
        )
        self.assertEqual(registry, {"codex:worker": "existing codex runner"})
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.bridge.events(), [])

    def test_registration_conflict_fails_without_dispatch(self):
        self.assertFalse(
            HERMES_REGISTRATION.preview({HERMES_DESTINATION: "other"})["registration_valid"]
        )
        self.assertEqual(self.fake.calls, [])

    def test_explicit_health_never_delegates_or_leaks_private_fields(self):
        self.assertEqual(self.gateway.health(), {"healthy": True, "local_route_available": True})
        self.assertEqual(self.fake.delegations, [])

    def test_handoff_report_and_completed_replay(self):
        handoff = self.handoff()
        report = self.runner.run(handoff.message_id)
        self.assertEqual(report.kind, "report")
        self.assertEqual(report.status, "succeeded")
        self.assertEqual(report.in_reply_to, handoff.message_id)
        self.assertEqual(report.payload["result"], "Synthetic result 😀")
        self.assertEqual(self.runner.run(handoff.message_id), report)
        self.bridge.acknowledge(report.message_id, actor="owner")
        self.assertEqual(self.runner.run(handoff.message_id), report)
        self.assertEqual(len(self.fake.delegations), 1)
        self.assertEqual(
            self.fake.delegations[0],
            (
                "POST",
                "/delegate",
                delegation_request(handoff.payload["instructions"]),
            ),
        )
        serialized = json_dumps(report.to_dict())
        for private in (
            "RAW_PRIVATE_STDERR",
            "PRIVATE_SESSION",
            "PRIVATE_HOME",
            "DO_NOT_FORWARD",
        ):
            self.assertNotIn(private, serialized)

    def test_task_without_handoff_and_wrong_destination_cannot_invoke(self):
        task = self.bridge.task(
            source="owner",
            destination=HERMES_DESTINATION,
            title="No dispatch",
            description="No dispatch",
        )
        for message in (task, self.handoff(destination="codex:worker")):
            with self.assertRaisesRegex(BridgeError, "runner_explicit_addressed_handoff_required"):
                self.runner.run(message.message_id)
        self.assertEqual(self.fake.calls, [])

    def test_malformed_handoff_cannot_invoke(self):
        handoff = self.handoff()
        malformed = replace(handoff, payload={**handoff.payload, "instructions": ""})
        with self.assertRaises(BridgeError):
            self.bridge.receive(malformed)
        with self.assertRaises(BridgeError):
            self.runner.run("not-a-message-id")
        self.assertEqual(self.fake.calls, [])

    def test_uncertain_acknowledged_handoff_never_retried_after_reopen(self):
        handoff = self.handoff()
        self.bridge.acknowledge(handoff.message_id, actor=HERMES_DESTINATION)
        runner = HermesRunner(Bridge.open(self.root), self.gateway)
        with self.assertRaisesRegex(BridgeError, "already_claimed_no_retry"):
            runner.run(handoff.message_id)
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_claims_delegate_once(self):
        handoff = self.handoff()

        def run():
            try:
                return HermesRunner(Bridge.open(self.root), self.gateway).run(handoff.message_id)
            except BridgeError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertTrue(any(result is not None for result in results))
        self.assertEqual(len(self.fake.delegations), 1)

    def test_malformed_response_fails_closed_and_replay_does_not_retry(self):
        self.fake.delegate_body = b'{"ok":true,"ok":false}'
        handoff = self.handoff()
        report = self.runner.run(handoff.message_id)
        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.payload["result"], "hermes_response_invalid")
        self.assertEqual(self.runner.run(handoff.message_id), report)
        self.assertEqual(len(self.fake.delegations), 1)

    def test_timeout_is_sanitized_and_does_not_retry(self):
        with patch(
            "http.client.HTTPConnection.request",
            side_effect=TimeoutError("SECRET_RAW_ERROR"),
        ):
            handoff = self.handoff()
            report = self.runner.run(handoff.message_id)
        self.assertEqual(report.status, "blocked")
        self.assertEqual(report.payload["result"], "hermes_timeout")
        self.assertEqual(self.runner.run(handoff.message_id), report)
        self.assertNotIn("SECRET_RAW_ERROR", json_dumps(report.to_dict()))
        self.assertEqual(self.fake.delegations, [])

    def test_unavailable_interface_never_falls_back(self):
        with patch(
            "http.client.HTTPConnection.request",
            side_effect=ConnectionRefusedError("RAW_ERROR"),
        ):
            report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_gateway_unavailable")
        self.assertEqual(self.fake.delegations, [])

    def test_real_transport_timeout_blocks_replay_after_one_fake_dispatch(self):
        self.fake.delegate_delay = 0.1
        runner = HermesRunner(
            self.bridge, HermesGateway(replace(self.config, timeout_seconds=0.05))
        )
        handoff = self.handoff()
        report = runner.run(handoff.message_id)
        self.assertEqual(report.payload["result"], "hermes_timeout")
        self.assertEqual(runner.run(handoff.message_id), report)
        self.assertEqual(len(self.fake.delegations), 1)

    def test_unexpected_invocation_error_is_sanitized(self):
        with patch.object(self.gateway, "delegate", side_effect=ValueError("PRIVATE_RAW_ERROR")):
            report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "runner_invocation_failed")
        self.assertNotIn("PRIVATE_RAW_ERROR", json_dumps(report.to_dict()))

    def test_request_overrides_sessions_and_history_rejected_before_io(self):
        mutations = [{"backend": backend} for backend in ("native", "primary", "", "cloud")] + [
            {"forceOffline": False}
        ]
        mutations += [
            {key: "forbidden"}
            for key in (
                "sessionId",
                "session_id",
                "resume",
                "history",
                "messages",
                "modelRoute",
                "model",
                "forceBackend",
                "force_backend",
                "force_offline",
                "fallbackChain",
            )
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(HermesError):
                self.gateway.delegate({**delegation_request("Synthetic prompt"), **mutation})
        self.assertEqual(self.fake.calls, [])

    def test_unproven_or_changed_contract_fails_before_http(self):
        config = replace(self.config, contract_files=())
        with self.assertRaisesRegex(HermesError, "contract_unproven"):
            HermesGateway(config)
        self.config.contract_files[0][1].path.write_text("changed", encoding="utf-8")
        report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_contract_changed")
        self.assertEqual(self.fake.calls, [])

    def test_changed_local_model_health_cannot_dispatch(self):
        self.fake.health_body["models"]["offline"] = "cloud:model"
        report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_local_policy_unproven")
        self.assertEqual(self.fake.delegations, [])

    def test_contract_rechecked_between_health_and_dispatch(self):
        original = self.gateway._health

        def change_after_health(deadline):
            result = original(deadline)
            self.config.contract_files[0][1].path.write_text("changed", encoding="utf-8")
            return result

        with patch.object(self.gateway, "_health", side_effect=change_after_health):
            report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_contract_changed")
        self.assertEqual(self.fake.delegations, [])

    def test_bad_json_utf8_and_oversized_response_fail_closed(self):
        for body in (b"not json", b"[]", b"\xff", b" " * 65537):
            with self.subTest(kind=body[:8]):
                self.fake.delegate_body = body
                report = self.runner.run(self.handoff().message_id)
                self.assertEqual(report.payload["result"], "hermes_response_invalid")

    def test_invalid_output_unicode_fails_closed(self):
        self.fake.delegate_body["response"] = "\ud800"
        report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_response_invalid")

    def test_old_result_cannot_attach_to_a_newer_handoff(self):
        handoff = self.handoff()

        def external_intervention(body):
            self.bridge.report(
                handoff.task_id,
                source=HERMES_DESTINATION,
                result="External explicit failure",
                status="failed",
            )
            self.bridge.handoff(
                handoff.task_id,
                source="owner",
                destination=HERMES_DESTINATION,
                instructions="Separate explicit handoff",
                context={},
            )
            return "Old invocation result"

        with (
            patch.object(self.gateway, "delegate", side_effect=external_intervention),
            self.assertRaises(BridgeError),
        ):
            self.runner.run(handoff.message_id)
        reports = [m for m in self.bridge.events() if m.kind == "report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].payload["result"], "External explicit failure")

    def test_unverified_completed_results_fail_closed(self):
        cases = {
            "backendUsed": "native",
            "providerUsed": "openai",
            "modelUsed": "other",
            "modelRoute": "other",
            "fallbackChain": [MODEL, "cloud"],
            "completed": False,
            "runStatus": "failed",
            "exitCode": 1,
            "response": "",
        }
        original = self.fake.delegate_body.copy()
        for field, value in cases.items():
            with self.subTest(field=field):
                self.fake.delegate_body = {**original, field: value}
                report = self.runner.run(self.handoff().message_id)
                self.assertEqual(report.status, "blocked")
                self.assertEqual(report.payload["result"], "hermes_result_unverified")

    def test_no_redirect_or_raw_gateway_error_is_exposed(self):
        self.fake.status = 302
        self.fake.health_body = {"error": "SECRET_RAW_ERROR"}
        report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "hermes_gateway_rejected")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.delegations, [])

    def test_secret_input_denied_and_secret_output_redacted(self):
        report = self.runner.run(self.handoff("api_key=PRIVATE_CREDENTIAL").message_id)
        self.assertEqual(report.payload["result"], "hermes_input_not_shareable")
        self.assertEqual(self.fake.calls, [])
        self.fake.delegate_body["response"] = "OK api_key=PRIVATE_CREDENTIAL Bearer PRIVATE_TOKEN"
        report = self.runner.run(self.handoff().message_id)
        self.assertNotIn("PRIVATE_CREDENTIAL", report.payload["result"])
        self.assertNotIn("PRIVATE_TOKEN", report.payload["result"])
        self.fake.delegate_body["response"] = (
            "-----BEGIN PRIVATE KEY-----\nPRIVATE_KEY_BODY\n-----END PRIVATE KEY-----"
        )
        report = self.runner.run(self.handoff().message_id)
        self.assertEqual(report.payload["result"], "[REDACTED]")

    def test_endpoint_and_timeout_restrictions(self):
        for model in ("cloud:model", "gpt-model", "gemma4:cloud", "unknown-local-model"):
            with self.subTest(model=model), self.assertRaises(HermesError):
                HermesGateway(replace(self.config, local_model=model))
        for endpoint in (
            "http://example.invalid:8788",
            "http://localhost:8788",
            "https://127.0.0.1:1",
            "http://user:pass@127.0.0.1:1",
            "http://127.0.0.1:1/delegate",
            "http://127.0.0.1:1?token=private",
            "http://127.0.0.1",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(HermesError):
                HermesGateway(replace(self.config, endpoint=endpoint))
        for timeout in (True, 0, -1, 61, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(HermesError):
                HermesGateway(replace(self.config, timeout_seconds=timeout))


if __name__ == "__main__":
    unittest.main()
