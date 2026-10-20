"""Tests for sentinel.api.client with public LLM providers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from sentinel.api.client import (
    APIError,
    AnthropicClient,
    CopilotCliClient,
    ModelClient,
    ModelResponse,
    UsageSummary,
    _parse_json_response,
    _resolve_retry_after_seconds,
    create_model_client,
)
from sentinel.config import Config


@pytest.fixture()
def config() -> Config:
    cfg = Config(
        env={
            "SENTINEL_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
            "SENTINEL_MODEL": "gpt-5.1",
            "SENTINEL_REPO_PATH": "/tmp/test_repo",
        },
        load_dotenv_file=False,
    )
    cfg.api_max_retries = 2
    cfg.api_retry_delay = 0.01
    return cfg


class TestModelResponse:
    def test_fields_populated(self) -> None:
        r = ModelResponse("hello", "gpt-5.1", 10, 5, 15)
        assert r.content == "hello"
        assert r.total_tokens == 15
        assert r.raw == {}


class TestUsageSummary:
    def test_accumulates_correctly(self) -> None:
        usage = UsageSummary()
        usage.add(ModelResponse("a", "m", 10, 5, 15))
        usage.add(ModelResponse("b", "m", 20, 10, 30))
        assert usage.total_calls == 2
        assert usage.total_prompt_tokens == 30
        assert usage.total_completion_tokens == 15
        assert usage.total_tokens == 45


class TestParseJsonResponse:
    def test_plain_json(self) -> None:
        assert _parse_json_response('{"key": "value"}') == {"key": "value"}

    def test_fenced_json(self) -> None:
        assert _parse_json_response('```json\n{"key": "value"}\n```') == {"key": "value"}

    def test_raises_on_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            _parse_json_response("not json")

    def test_strips_trailing_commas(self) -> None:
        assert _parse_json_response('{"a": [1, 2,],}') == {"a": [1, 2]}

    def test_extracts_object_from_surrounding_prose(self) -> None:
        assert _parse_json_response('Here you go: {"a": 1} hope that helps') == {"a": 1}


class TestReasoningModelParameters:
    """OpenAI-compatible reasoning models (gpt-5*, o-series) reject a custom
    temperature and the legacy ``max_tokens`` field."""

    @staticmethod
    def _cfg(model: str) -> Config:
        cfg = Config(
            env={
                "SENTINEL_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
                "SENTINEL_MODEL": model,
                "SENTINEL_REPO_PATH": "/tmp/test_repo",
            },
            load_dotenv_file=False,
        )
        cfg.api_max_retries = 1
        cfg.api_retry_delay = 0.01
        return cfg

    @staticmethod
    def _sent_payload(model: str) -> dict:
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"model": model, "choices": [{"message": {"content": "ok"}}]}
            )
        )
        with ModelClient(TestReasoningModelParameters._cfg(model)) as client:
            client.call_model("sys", "msg", temperature=0.2, max_tokens=123)
        return json.loads(route.calls.last.request.content)

    @respx.mock
    @pytest.mark.parametrize("model", ["gpt-5.1", "o3-mini", "GPT-5.4"])
    def test_reasoning_models_use_default_temperature_and_max_completion_tokens(
        self, model: str
    ) -> None:
        payload = self._sent_payload(model)
        assert payload["temperature"] == 1.0
        assert payload["max_completion_tokens"] == 123
        assert "max_tokens" not in payload

    @respx.mock
    def test_other_models_keep_caller_temperature_and_max_tokens(self) -> None:
        payload = self._sent_payload("gpt-4o-mini")
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 123
        assert "max_completion_tokens" not in payload


class TestCallModelJsonRetry:
    """Invalid JSON gets exactly one compact retry before failing."""

    @staticmethod
    def _reply(content: str) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": content}}]})

    @respx.mock
    def test_retries_once_with_compact_instruction(self, config: Config) -> None:
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=[self._reply("not json"), self._reply('{"ok": true}')]
        )
        with ModelClient(config) as client:
            assert client.call_model_json("sys", "msg") == {"ok": True}
        assert route.call_count == 2
        retry_body = json.loads(route.calls.last.request.content)
        assert "Return only strict JSON" in retry_body["messages"][1]["content"]

    @respx.mock
    def test_raises_after_second_invalid_reply(self, config: Config) -> None:
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=[self._reply("nope"), self._reply("still nope")]
        )
        with ModelClient(config) as client:
            with pytest.raises(ValueError, match="after retry"):
                client.call_model_json("sys", "msg")

    def test_copilot_cli_client_retries_invalid_json(self, tmp_path, monkeypatch) -> None:
        cfg = Config(
            env={"SENTINEL_PROVIDER": "copilot-cli", "SENTINEL_REPO_PATH": str(tmp_path)},
            load_dotenv_file=False,
        )
        replies = iter(["garbage", '{"ok": 1}'])

        class _Done:
            returncode = 0
            stderr = ""

            def __init__(self) -> None:
                self.stdout = next(replies)

        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", lambda *a, **k: _Done())
        with CopilotCliClient(cfg) as client:
            assert client.call_model_json("sys", "msg") == {"ok": 1}


class TestRetryAfterParsing:
    def test_uses_retry_after_ms_header_as_seconds(self) -> None:
        headers = httpx.Headers({"retry-after-ms": "65000"})
        assert _resolve_retry_after_seconds(headers, fallback_seconds=2.0) == pytest.approx(65.0)

    def test_caps_extreme_retry_after_values(self) -> None:
        headers = httpx.Headers({"Retry-After": "999999"})
        assert _resolve_retry_after_seconds(headers, fallback_seconds=2.0) == 120.0

    def test_parses_http_date(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        headers = httpx.Headers({"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")})
        delay = _resolve_retry_after_seconds(headers, fallback_seconds=2.0)
        assert 0.0 <= delay <= 120.0


class TestProviderTelemetry:
    """Every provider must populate llm_telemetry.json so the budget gate
    and hotspots panel work for all users."""

    @respx.mock
    def test_model_client_records_telemetry_for_github_models(self, tmp_path) -> None:
        cfg = Config(
            env={
                "SENTINEL_PROVIDER": "github-models",
                "SENTINEL_ALLOW_GITHUB_MODELS": "true",
                "GITHUB_TOKEN": "ghp_test",
                "SENTINEL_MODEL": "gpt-4o-mini",
                "SENTINEL_REPO_PATH": str(tmp_path),
            },
            load_dotenv_file=False,
        )
        cfg.api_max_retries = 2
        cfg.api_retry_delay = 0.01

        respx.post("https://models.inference.ai.azure.com/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "gpt-4o-mini",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )
        )

        with ModelClient(cfg) as client:
            client.call_model(
                "sys",
                "msg",
                call_metadata={"agent": "coder", "skill_name": "security"},
            )

        telemetry_path = cfg.state_dir / "llm_telemetry.json"
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        entry = telemetry[-1]
        assert entry["status"] == "success"
        assert entry["endpoint"] == "/chat/completions"
        assert entry["prompt_tokens"] == 5
        assert entry["completion_tokens"] == 3
        assert entry["total_tokens"] == 8
        assert entry["agent"] == "coder"
        assert entry["skill_name"] == "security"

    def test_copilot_cli_client_records_telemetry(self, tmp_path, monkeypatch) -> None:
        cfg = Config(
            env={
                "SENTINEL_PROVIDER": "copilot-cli",
                "SENTINEL_MODEL": "gpt-5.1",
                "SENTINEL_REPO_PATH": str(tmp_path),
            },
            load_dotenv_file=False,
        )

        class _FakeCompleted:
            returncode = 0
            stdout = "final answer"
            stderr = ""

        def fake_run(*args, **kwargs):
            return _FakeCompleted()

        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", fake_run)

        with CopilotCliClient(cfg) as client:
            client.call_model(
                "sys",
                "msg",
                call_metadata={"agent": "qa"},
            )

        telemetry_path = cfg.state_dir / "llm_telemetry.json"
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        entry = telemetry[-1]
        assert entry["status"] == "success"
        assert entry["endpoint"] == "copilot-cli"
        assert entry["token_source"] == "estimated"
        assert entry["total_tokens"] > 0  # estimated from prompt+response length
        assert entry["agent"] == "qa"


class TestAnthropicCredentials:
    """Only ANTHROPIC_API_KEY authenticates; a local Claude Code subscription
    token must never be used."""

    def _cfg(self, **env: str) -> Config:
        return Config(
            env={"SENTINEL_PROVIDER": "anthropic", "SENTINEL_REPO_PATH": "/tmp/test_repo", **env},
            load_dotenv_file=False,
        )

    def test_ignores_claude_code_subscription_credentials(self, tmp_path, monkeypatch) -> None:
        creds = tmp_path / ".claude" / ".credentials.json"
        creds.parent.mkdir()
        creds.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "expiresAt": 9999999999999}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
            AnthropicClient(self._cfg())

    def test_uses_api_key(self) -> None:
        client = AnthropicClient(self._cfg(ANTHROPIC_API_KEY="sk-ant-test"))
        assert client._client.api_key == "sk-ant-test"
        client.close()


class TestFactory:
    def test_create_model_client_returns_model_client_for_openai(self, config: Config) -> None:
        client = create_model_client(config)
        assert isinstance(client, ModelClient)
        client.close()

    def test_create_model_client_rejects_unknown_provider(self) -> None:
        cfg = Config(
            env={"SENTINEL_PROVIDER": "not-a-provider", "SENTINEL_REPO_PATH": "/tmp/test_repo"},
            load_dotenv_file=False,
        )
        with pytest.raises(EnvironmentError, match="No model provider resolved"):
            create_model_client(cfg)

    def test_unresolved_provider_message_lists_public_providers(self) -> None:
        cfg = Config(env={"SENTINEL_REPO_PATH": "/tmp/test_repo"}, load_dotenv_file=False)
        with pytest.raises(EnvironmentError) as exc_info:
            create_model_client(cfg)
        message = str(exc_info.value)
        for name in ("copilot-cli", "openai", "anthropic"):
            assert name in message

    def test_create_model_client_selects_each_public_provider(self) -> None:
        cases = {
            "anthropic": ({"ANTHROPIC_API_KEY": "sk-ant-test"}, AnthropicClient),
            "openai": ({"OPENAI_API_KEY": "sk-test"}, ModelClient),
            "copilot-cli": ({}, CopilotCliClient),
            "github-models": (
                {"GITHUB_TOKEN": "ghp_test", "SENTINEL_ALLOW_GITHUB_MODELS": "true"},
                ModelClient,
            ),
        }
        for provider, (extra_env, expected) in cases.items():
            cfg = Config(
                env={"SENTINEL_PROVIDER": provider, "SENTINEL_REPO_PATH": "/tmp/test_repo", **extra_env},
                load_dotenv_file=False,
            )
            client = create_model_client(cfg)
            assert isinstance(client, expected), provider
            client.close()

    def test_create_model_client_local_only_returns_noop(self) -> None:
        cfg = Config(
            env={
                "SENTINEL_REPO_PATH": "/tmp/test_repo",
                "SENTINEL_LOCAL_ONLY_MODE": "true",
            },
            load_dotenv_file=False,
        )
        client = create_model_client(cfg)
        with pytest.raises(APIError, match="No-op client"):
            client.call_model("sys", "msg")
