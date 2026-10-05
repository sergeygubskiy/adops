"""Структура проверки состояния: чистая логика и порядок ожидания — без браузера, время подменено."""
from adops import health
from adops.demo import DemoProbe


class Clock:
    """Управляемое время: sleep двигает часы, реального ожидания нет."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def check(probe, **kw):
    c = Clock()
    return health.check_account(probe, clock=c, sleep=c.sleep, **kw), c


def test_parse_amount_variants():
    assert health.parse_amount("-$7.75") == "-7,75"
    assert health.parse_amount("−$7.75") == "-7,75"          # юникод-минус
    assert health.parse_amount("₹40,141.58") == "40141,58"
    assert health.parse_amount("") == "" and health.parse_amount("n/a") == ""


def test_refund_negative_is_zero():
    assert health.refund_from_funds("-7,75") == "0"
    assert health.refund_from_funds("12,50") == "12,50"
    assert health.refund_from_funds("0,00") == "0,00"


def test_classify_alerts_priority():
    assert health.classify_alerts(["Tip"]) == health.ACTIVE
    assert health.classify_alerts(["Your account is SUSPENDED"]) == health.SUSPENDED
    assert health.classify_alerts(["Verify your account"]) == health.VERIFICATION
    assert health.classify_alerts(["Verify your account", "account suspended"]) == health.SUSPENDED
    assert health.classify_alerts([]) == health.ACTIVE


def test_status_line_formats():
    assert health.status_line(health.SUSPENDED, {"Eligible": 3}) == "suspended"
    assert health.status_line(health.VERIFICATION, {}) == "verification"
    assert health.status_line(health.NOT_READY, {}) == "not ready (re-check)"
    assert health.status_line(health.ACTIVE, {"Eligible": 3}) == "active - Eligible"
    assert health.status_line(health.ACTIVE, {"Eligible": 2, "Paused": 1}) == "active - 2 Eligible, 1 Paused"
    assert health.status_line(health.ACTIVE, {}) == "active - ?"
    assert health.status_line(health.ACTIVE, {"Eligible": 2}, kinds_known=False) == "active - ?"


def test_looks_suspended_second_gate_rule():
    assert health.looks_suspended({"Not eligible": 3, "Paused": 7}) is True
    assert health.looks_suspended({"Eligible": 1, "Not eligible": 3}) is False
    assert health.looks_suspended({"Paused": 2}) is False
    assert health.looks_suspended({}) is False


def test_carousel_is_flipped_to_the_end():
    p = DemoProbe("suspended")           # проблемное уведомление — на 2-м месте карусели
    texts, bar = health.collect_alerts(p)
    assert "Your account is suspended" in texts and bar is True


def test_carousel_guard_against_endless_flip():
    class Endless:
        def alert_page(self):
            return ["x"], 1, 99
        def next_alert(self):
            return True
    texts, _ = health.collect_alerts(Endless(), max_pages=5)
    assert texts == ["x"]


def test_suspended_found_on_second_carousel_page():
    res, _ = check(DemoProbe("suspended"))
    assert res.state == health.SUSPENDED and res.line() == "suspended"
    assert res.amount == "12,50"


def test_active_account_with_ad_counts():
    res, _ = check(DemoProbe("ok"))
    assert res.state == health.ACTIVE and res.line() == "active - 2 Eligible, 1 Paused"


def test_verification():
    assert check(DemoProbe("verify"))[0].state == health.VERIFICATION


def test_not_ready_when_balance_never_appears():
    """Главное правило: нет доказательства отрисовки — НЕ «всё хорошо», а «не догрузилось»."""
    res, clock = check(DemoProbe("ok", ready_after=10**9), funds_wait=30.0)
    assert res.state == health.NOT_READY
    assert clock.t >= 30.0


def test_slow_page_waits_instead_of_guessing():
    res, clock = check(DemoProbe("suspended", ready_after=5), funds_wait=150.0)
    assert res.state == health.SUSPENDED and clock.t >= 5.0


def test_healthy_account_exits_early_when_bar_rendered():
    res, clock = check(DemoProbe("ok"), bar_wait=75.0)
    assert res.state == health.ACTIVE and clock.t < 75.0


def test_no_bar_at_all_waits_only_bar_wait():
    class NoBar(DemoProbe):
        def alert_page(self):
            return [], 0, 0
    res, clock = check(NoBar("ok"), bar_wait=10.0)
    assert res.state == health.ACTIVE and 10.0 <= clock.t < 12.0


def test_second_gate_rereads_alerts():
    class LateAlert(DemoProbe):
        """Уведомление о проблеме появляется только при втором чтении панели."""
        reads = 0

        def alert_page(self):
            LateAlert.reads += 1
            return (["Tip"] if LateAlert.reads < 2 else ["Your account is suspended"]), 1, 1
        def ad_statuses(self):
            return ["Not eligible", "Paused"]

    LateAlert.reads = 0
    res, _ = check(LateAlert("ok"))
    assert res.state == health.SUSPENDED and "затвор" in res.note


def test_should_stop_interrupts_waiting():
    c = Clock()
    res = health.check_account(DemoProbe("ok", ready_after=10**9), clock=c, sleep=c.sleep,
                               should_stop=lambda: True)
    assert res.state == health.NOT_READY and res.note == "остановлено"
