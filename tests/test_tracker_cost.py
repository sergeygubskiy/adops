"""Чистая логика cost.py: разбор сумм, нормализация меток, шапка/дубли, тело запроса. Без сети."""
from adops.tracker import cost


def test_parse_cost():
    assert cost.parse_cost("271,83") == 271.83
    assert cost.parse_cost("271.83") == 271.83
    assert cost.parse_cost(" 100 ") == 100.0
    assert cost.parse_cost("0") == 0.0
    assert cost.parse_cost("0,0007") == 0.0007
    assert cost.parse_cost("1 234,56") == 1234.56      # неразрывный пробел-тысячные
    assert cost.parse_cost("1 234,56") == 1234.56           # обычный пробел-тысячные
    assert cost.parse_cost("$50,5") == 50.5
    for bad in ("", None, "abc", "-5", "1.234.56", "nan", "inf"):
        assert cost.parse_cost(bad) is None, bad


def test_norm_tag():
    assert cost.norm_tag("TAG-1/AbC") == "tag-1/abc"
    assert cost.norm_tag(" tag-1/abc/ ") == "tag-1/abc"
    assert cost.norm_tag("T-1/ABC") == cost.norm_tag("t-1/abc")
    assert cost.norm_tag(None) == "" and cost.norm_tag("  ") == ""


def test_fmt_money():
    assert cost.fmt_money(271.83) == "271.83"
    assert cost.fmt_money(100.0) == "100"
    assert cost.fmt_money(0.0007) == "0.0007"
    assert cost.fmt_money(0) == "0"
    assert cost.fmt_money(None) == "—"


def test_looks_like_header():
    assert cost.looks_like_header(["Метка", "Сумма"]) is True
    assert cost.looks_like_header(["tag", "cost"]) is True
    assert cost.looks_like_header(["t-100/abc", "271,83"]) is False
    assert cost.looks_like_header(["", ""]) is False


def test_parse_rows_grid():
    grid = [
        ["Метка", "Cost"],                 # шапка → пропуск
        ["t-100/AbCd", "271,83"],
        ["", ""],                          # пусто → пропуск
        ["t-111/aaa", "10"],
        ["t-222/bbb", ""],                 # частичная → ошибка
        ["", "5"],                         # частичная → ошибка
        ["t-333/ccc", "плохо"],            # плохая сумма → ошибка
        ["T-100/abcd", "999"],             # дубль (тот же ключ) → пропуск
    ]
    rows, errs = cost.parse_rows(grid)
    assert [r["tag"] for r in rows] == ["t-100/AbCd", "t-111/aaa"]
    assert rows[0]["cost"] == 271.83 and rows[1]["cost"] == 10.0
    assert rows[0]["key"] == cost.norm_tag("t-100/AbCd")
    assert len(errs) == 4
    assert any("дубль" in e for e in errs) and any("плохая сумма" in e for e in errs)


def test_parse_rows_keeps_order_and_exact_case():
    rows, _ = cost.parse_rows([["T-AaA/bBb", "1,5"], ["T-Zzz/999", "2"]])
    assert [r["tag"] for r in rows] == ["T-AaA/bBb", "T-Zzz/999"]


def test_build_cost_body_contract():
    body = cost.build_cost_body("t-1/abc", 271.83)
    assert body == {"period": "all_time", "timezone": "UTC", "tagSlot": 2, "tagValue": "t-1/abc",
                    "model": "TOTAL", "cost": 271.83, "currency": "USD"}
    assert isinstance(body["tagSlot"], int)
