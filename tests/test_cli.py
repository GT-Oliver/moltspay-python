"""CLI parser and command-dispatch regression tests."""

import json

import pytest

from moltspay.cli import build_parser, client_for, cmd_alipay, cmd_limits, cmd_pay, cmd_status, configure_stdio, cmd_wechat
from moltspay.exceptions import InteractiveRailRequiresLifecycle
from moltspay.models import Balance, Limits, PaymentResult


def test_help_lists_all_supported_commands():
    parser = build_parser()
    help_text = parser.format_help()

    for command in (
        "init", "status", "faucet", "pay", "approve", "services", "fund",
        "transfer", "config", "balance", "wechat", "list", "validate", "server",
    ):
        assert command in help_text

    assert "limits" not in help_text


def test_node_parity_options_are_present_and_python_only_options_are_hidden(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["pay", "--help"])
    pay_help = capsys.readouterr().out
    for option in ("--prompt", "--image", "--data", "--buyer", "--pack", "--config-dir", "--json"):
        assert option in pay_help
    assert "--buyer-id" not in pay_help
    assert "--topup-pack" not in pay_help


def test_nested_help_describes_balance_and_wechat_commands(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["balance", "--help"])

    with pytest.raises(SystemExit):
        parser.parse_args(["wechat", "--help"])
    help_text = capsys.readouterr().out
    assert "start" in help_text
    assert "list" in help_text


def test_status_keeps_single_chain_balance_key(monkeypatch, capsys):
    class FakeClient:
        address = "0x123"

        def __init__(self, **kwargs):
            pass

        def get_config(self):
            return {"chain": "base"}

        def balance(self, chain):
            return Balance(address=self.address, usdc=1, eth=0, chain=chain)

    monkeypatch.setattr("moltspay.cli.MoltsPay", FakeClient)
    assert cmd_status(type("Args", (), {"chain": "base", "all": False})()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["balance"]["usdc"] == 1
    assert payload["balances"]["base"]["usdc"] == 1


def test_limits_reads_and_updates_limits(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs):
            self.updated = None

        def set_limits(self, **kwargs):
            self.updated = kwargs

        def limits(self):
            return Limits(max_per_tx=5, max_per_day=50)

    monkeypatch.setattr("moltspay.cli.MoltsPay", FakeClient)
    args = type("Args", (), {"chain": "base", "max_per_tx": 5.0, "max_per_day": 50.0})()
    assert cmd_limits(args) == 0
    assert json.loads(capsys.readouterr().out)["max_per_day"] == 50


def test_configure_stdio_requests_utf8(monkeypatch):
    class Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    stdout = Stream()
    stderr = Stream()
    monkeypatch.setattr("moltspay.cli.sys.stdout", stdout)
    monkeypatch.setattr("moltspay.cli.sys.stderr", stderr)

    configure_stdio()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_wechat_start_prints_qr_to_stderr_and_keeps_json_on_stdout(monkeypatch, capsys):
    class Session:
        code_url = "weixin://wxpay/bizpayurl?pr=test"

        def model_dump(self):
            return {"code_url": self.code_url, "status": "pending"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def start_wechat_payment(self, server, service, params):
            assert (server, service, params) == ("http://provider", "ping", {"x": 1})
            return Session()

    monkeypatch.setattr("moltspay.cli.MoltsPay", FakeClient)
    monkeypatch.setattr("moltspay.cli.print_wechat_qr", lambda url: print(f"QR: {url}", file=__import__("sys").stderr))

    assert cmd_wechat(type("Args", (), {
        "wechat_command": "start", "server": "http://provider", "service": "ping", "params": '{"x": 1}'
    })()) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "pending"
    assert "QR: weixin://wxpay/bizpayurl?pr=test" in captured.err


def test_pay_passes_repeat_policy_and_prints_each_balance_topup_qr(monkeypatch, capsys):
    class FakeClient:
        def pay(self, server, service, **kwargs):
            options = kwargs["rail_options"]
            assert options["topup_pack"] == "10"
            assert options["max_topup_attempts"] == 4
            assert options["topup_poll_interval"] == 0.25
            options["on_topup_required"]("10", "weixin://pay/topup-1")
            options["on_topup_required"]("10", "weixin://pay/topup-2")
            return PaymentResult(
                success=True, amount=0.01, token="BALANCE", service_id=service,
                result={"ok": True},
            )

    shown = []
    monkeypatch.setattr("moltspay.cli.client_for", lambda args: FakeClient())
    monkeypatch.setattr("moltspay.cli.print_wechat_qr", shown.append)
    args = build_parser().parse_args([
        "pay", "https://provider.test", "ping", "--rail", "balance", "--pack", "10",
        "--max-topup-attempts", "4", "--topup-poll-interval", "0.25",
    ])

    assert cmd_pay(args) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["success"] is True
    assert captured.err.count("Provider balance top-up required: CNY 10") == 2
    assert shown == ["weixin://pay/topup-1", "weixin://pay/topup-2"]


def test_pay_rejects_blocking_alipay_lifecycle_in_openclaw(monkeypatch):
    monkeypatch.setenv("AIPAY_FRAMEWORK", "openclaw")
    monkeypatch.setattr(
        "moltspay.cli.client_for",
        lambda args: (_ for _ in ()).throw(AssertionError("must reject before provider request")),
    )
    args = build_parser().parse_args([
        "pay", "https://provider.test", "pong", "--rail", "alipay",
        "--session-id", "d52e3b71-d00e-4a51-bc16-169cba465bc9",
    ])

    with pytest.raises(InteractiveRailRequiresLifecycle) as error:
        cmd_pay(args)

    assert error.value.details == {"command": "moltspay alipay start"}


def test_alipay_start_is_nonblocking_and_emits_media(monkeypatch, capsys):
    observed = {}

    class Session:
        status = "pending"
        media_paths = ["/tmp/alipay-payment.png"]

        def model_dump(self):
            return {
                "status": self.status,
                "media_paths": self.media_paths,
                "payment_session_id": "mpay_alipay_1",
            }

    class FakeClient:
        def start_alipay_payment(self, server, service, params, **kwargs):
            observed.update(server=server, service=service, params=params, kwargs=kwargs)
            return Session()

    monkeypatch.setattr("moltspay.cli.client_for", lambda args: FakeClient())
    args = build_parser().parse_args([
        "alipay", "start", "https://provider.test", "pong", '{"x":1}',
        "--session-id", "d52e3b71-d00e-4a51-bc16-169cba465bc9",
        "--framework", "openclaw", "--intent-summary", "原始请求：购买 pong 服务",
    ])

    assert cmd_alipay(args) == 0
    output = capsys.readouterr().out
    assert '"status": "pending"' in output
    assert "MEDIA: /tmp/alipay-payment.png" in output
    assert observed["kwargs"]["business_session_id"] == "d52e3b71-d00e-4a51-bc16-169cba465bc9"


def test_client_for_passes_alipay_framework(monkeypatch, tmp_path):
    captured = {}

    class FakeMoltsPay:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("moltspay.cli.MoltsPay", FakeMoltsPay)
    args = type("Args", (), {
        "config_dir": str(tmp_path), "chain": "base", "framework": "openclaw",
    })()

    client_for(args)

    assert captured["alipay_framework"] == "openclaw"
