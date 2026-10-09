"""Opt-in local WSL Hermes runner; importing/configuring it never delegates."""

import hashlib
import http.client
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .bridge import Bridge
from .protocol import BridgeError, Message, json_dumps, json_loads
from .runners import ExplicitHandoffRunner, RunnerRegistration

HERMES_DESTINATION = "hermes:orchestrator"
HERMES_REGISTRATION = RunnerRegistration(HERMES_DESTINATION, "hermes local gateway runner")
_CONTRACT_ROLES = frozenset({"gateway", "router", "launcher", "wsl_policy", "wsl_fallback"})
_LOCAL_MODELS = frozenset({"gemma4:12b-it-qat", "qwen2.5:14b"})
_MAX_RESPONSE = 65536
_MAX_PROMPT = 16384
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+"
    r"|\b(?:[A-Za-z0-9_]*(?:api[_-]?key|token|secret|password))\s*[=:]\s*[^\s,;]+)",
    re.IGNORECASE,
)


class HermesError(BridgeError):
    """Only fixed categories cross the runner/report boundary."""


@dataclass(frozen=True)
class ContractFile:
    path: Path
    sha256: str

    def verify(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise HermesError("hermes_contract_unproven")
        try:
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            raise HermesError("hermes_contract_unavailable") from None
        if digest != self.sha256:
            raise HermesError("hermes_contract_changed")


@dataclass(frozen=True)
class HermesConfig:
    endpoint: str
    local_model: str
    contract_files: tuple[tuple[str, ContractFile], ...]
    timeout_seconds: float = 30.0

    def validate(self) -> tuple[str, int]:
        try:
            url = urlsplit(self.endpoint)
            port = url.port
        except (ValueError, TypeError):
            raise HermesError("hermes_endpoint_invalid") from None
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65535
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise HermesError("hermes_endpoint_invalid")
        if self.local_model not in _LOCAL_MODELS:
            raise HermesError("hermes_local_model_unproven")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 60:
            raise HermesError("hermes_timeout_invalid")
        roles = [role for role, _ in self.contract_files]
        if len(roles) != len(_CONTRACT_ROLES) or set(roles) != _CONTRACT_ROLES:
            raise HermesError("hermes_contract_unproven")
        paths = [pin.path for _, pin in self.contract_files]
        if any(not path.is_absolute() for path in paths) or len(set(paths)) != len(paths):
            raise HermesError("hermes_contract_unproven")
        return url.hostname, port

    def verify_contract(self) -> None:
        self.validate()
        for _, pin in self.contract_files:
            pin.verify()


def delegation_request(prompt: str) -> dict:
    return {"prompt": prompt, "backend": "wsl", "forceOffline": True}


def validate_delegation_request(body: dict) -> None:
    # No operator/handoff-supplied gateway options, session, history, model or
    # backend override can enter this exact request contract.
    if (
        type(body) is not dict
        or set(body) != {"prompt", "backend", "forceOffline"}
        or body["backend"] != "wsl"
        or body["forceOffline"] is not True
        or type(body["prompt"]) is not str
        or not body["prompt"].strip()
    ):
        raise HermesError("hermes_request_invalid")
    try:
        size = len(body["prompt"].encode("utf-8"))
    except UnicodeError:
        raise HermesError("hermes_request_invalid") from None
    if size > _MAX_PROMPT or _SECRET.search(body["prompt"]):
        raise HermesError("hermes_input_not_shareable")


class HermesGateway:
    def __init__(self, config: HermesConfig):
        config.validate()
        self.config = config

    def _request(self, method: str, path: str, body: dict | None, deadline: float) -> dict:
        host, port = self.config.validate()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HermesError("hermes_timeout")
        # Numeric loopback only; no proxies, auth, redirects, DNS or fallback.
        connection = http.client.HTTPConnection(host, port, timeout=remaining)
        try:
            connection.request(
                method,
                path,
                body=None if body is None else json_dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            sock = connection.sock
            response = connection.getresponse()
            if response.status != 200:
                raise HermesError("hermes_gateway_rejected")
            data = bytearray()
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HermesError("hermes_timeout")
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = response.read1(min(4096, _MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_RESPONSE:
                    raise HermesError("hermes_response_invalid")
            parsed = json_loads(data.decode("utf-8"))
            if type(parsed) is not dict:
                raise HermesError("hermes_response_invalid")
            return parsed
        except HermesError:
            raise
        except TimeoutError:
            raise HermesError("hermes_timeout") from None
        except (OSError, http.client.HTTPException):
            raise HermesError("hermes_gateway_unavailable") from None
        except (BridgeError, UnicodeError, ValueError, RecursionError):
            raise HermesError("hermes_response_invalid") from None
        finally:
            connection.close()

    def _health(self, deadline: float) -> dict:
        self.config.verify_contract()
        data = self._request("GET", "/health", None, deadline)
        if (
            data.get("ok") is not True
            or data.get("service") != "hermes-agent-delegate-gateway"
            or type(data.get("models")) is not dict
            or data["models"].get("offline") != self.config.local_model
        ):
            raise HermesError("hermes_local_policy_unproven")
        # Do not expose private home paths, cooldown metadata or other models.
        return {"healthy": True, "local_route_available": True}

    def health(self) -> dict:
        """Explicit no-inference check, never called by construction/registration."""
        return self._health(time.monotonic() + self.config.timeout_seconds)

    def delegate(self, body: dict) -> str:
        validate_delegation_request(body)
        deadline = time.monotonic() + self.config.timeout_seconds
        self._health(deadline)
        # Recheck pins immediately before dispatch; health is not a policy proof.
        self.config.verify_contract()
        data = self._request("POST", "/delegate", body, deadline)
        model = self.config.local_model
        if (
            data.get("ok") is not True
            or data.get("completed") is not True
            or data.get("runStatus") != "completed"
            or type(data.get("exitCode")) is not int
            or data["exitCode"] != 0
            or data.get("backendUsed") != "wsl"
            or data.get("providerUsed") not in ("custom", "ollama")
            or data.get("modelRoute") != model
            or data.get("modelUsed") != model
            or data.get("fallbackChain") != [model]
            or type(data.get("response")) is not str
            or not data["response"].strip()
        ):
            raise HermesError("hermes_result_unverified")
        try:
            data["response"].encode("utf-8")
        except UnicodeError:
            raise HermesError("hermes_response_invalid") from None
        # Discard stderr, session IDs, error text and every unknown field.
        # Suppress the whole credential-bearing result (including PEM bodies),
        # rather than masking a marker while leaving its associated secret.
        return "[REDACTED]" if _SECRET.search(data["response"]) else data["response"]


class HermesRunner:
    registration = HERMES_REGISTRATION

    def __init__(self, bridge: Bridge, gateway: HermesGateway):
        self.gateway = gateway
        self.lifecycle = ExplicitHandoffRunner(bridge, HERMES_DESTINATION)

    def run(self, handoff_message_id: str) -> Message:
        """Only this explicit method can delegate; there is no watcher/auto-run."""

        def invoke(handoff: Message) -> tuple[str, str]:
            try:
                result = self.gateway.delegate(delegation_request(handoff.payload["instructions"]))
                return "succeeded", result
            except HermesError as exc:
                return "blocked", str(exc)

        return self.lifecycle.run(handoff_message_id, invoke)
