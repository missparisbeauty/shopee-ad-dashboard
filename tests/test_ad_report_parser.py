"""ad_report_parser 單元測試
涵蓋：UTF-8 BOM / BIG5 / 蝦皮 lifetime 摘要列 / 多日警告 /
     多欄對同一 std field 不被 0 蓋 / 缺資料跳過 / 編碼偵測失敗
"""
import pytest
from ad_report_parser import (
    parse_csv, parse_date, parse_number, parse_int,
    map_columns, _extract_metadata, _find_header_row,
)


class TestNumberParser:
    def test_basic(self):
        assert parse_number("100") == 100.0
        assert parse_number("1,234") == 1234.0
        assert parse_number("NT$1,500") == 1500.0
        assert parse_number("3.14") == 3.14

    def test_percent(self):
        assert parse_number("2.5%") == 0.025
        assert parse_number("100%") == 1.0

    def test_empty_or_invalid(self):
        assert parse_number("") is None
        assert parse_number("-") is None
        assert parse_number("N/A") is None
        assert parse_number(None) is None
        assert parse_number("abc") is None

    def test_int(self):
        assert parse_int("1,234") == 1234
        assert parse_int("0") == 0
        assert parse_int("") is None


class TestDateParser:
    def test_iso(self):
        assert parse_date("2026-04-25") == "2026-04-25"

    def test_slash(self):
        assert parse_date("2026/04/25") == "2026-04-25"
        assert parse_date("2026/4/5") == "2026-04-05"

    def test_with_time(self):
        assert parse_date("2026-04-25 10:30:00") == "2026-04-25"

    def test_invalid(self):
        assert parse_date("") is None
        assert parse_date("abc") is None
        assert parse_date(None) is None


class TestColumnMapping:
    def test_chinese_aliases(self):
        m = map_columns(["日期", "商品ID", "曝光數", "點擊數", "花費", "銷售額"])
        assert m["日期"] == "date"
        assert m["商品ID"] == "product_id"
        assert m["曝光數"] == "impressions"
        assert m["點擊數"] == "clicks"
        assert m["花費"] == "spend"
        assert m["銷售額"] == "revenue"

    def test_english_aliases(self):
        m = map_columns(["Date", "Item ID", "Impressions", "Cost", "Sales"])
        assert m["Date"] == "date"
        assert m["Item ID"] == "product_id"
        assert m["Cost"] == "spend"
        assert m["Sales"] == "revenue"

    def test_shopee_cpc_specific(self):
        m = map_columns(["瀏覽數", "銷售金額", "投入產出比", "成本收入比率", "結束日期"])
        assert m["瀏覽數"] == "impressions"
        assert m["銷售金額"] == "revenue"
        assert m["投入產出比"] == "roas"
        assert m["成本收入比率"] == "acos"
        assert m["結束日期"] == "date"

    def test_unknown_columns(self):
        m = map_columns(["foo", "bar", "baz"])
        assert m["foo"] == ""


class TestParseCsv:
    def test_chinese_utf8_bom(self):
        csv = ('日期,商品ID,商品名稱,曝光數,點擊數,點擊率,花費,訂單數,銷售額\n'
               '2026-04-25,P10001,保濕精華液,12500,320,2.56%,"1,280",18,"4,200"\n'
               '2026-04-26,P10001,保濕精華液,15800,410,2.59%,"1,540",24,"5,820"\n')
        r = parse_csv(csv.encode('utf-8-sig'))
        assert r.encoding == "utf-8-sig"
        assert len(r.rows) == 2
        assert r.skipped_rows == 0
        assert r.rows[0]["spend"] == 1280.0
        assert r.rows[0]["revenue"] == 4200.0
        assert r.rows[0]["ctr"] == pytest.approx(0.0256)

    def test_big5_chinese(self):
        csv = ('日期,商品名稱,曝光數,點擊數,花費,訂單數,銷售額\n'
               '2026/04/15,小工具,5000,150,750,10,3500\n')
        r = parse_csv(csv.encode('big5'))
        assert r.encoding == "big5"
        assert len(r.rows) == 1
        assert r.rows[0]["roas"] == pytest.approx(3500/750, rel=1e-3)

    def test_lifetime_summary_row_extracts_period(self):
        """蝦皮真實格式：摘要列在 header 之前，含「期間」欄位"""
        csv = ('所有CPC成效報告 - Shopee台灣\n'
               '使用者名稱,kebagoods\n'
               '賣場名稱,科爸好皮\n'
               '報表匯出時間,2026/05/03 16:50\n'
               '期間,2026/05/03 - 2026/05/03\n'
               '\n'
               '商品 ID,廣告名稱,曝光數,點擊數,點擊率,花費,訂單數,銷售金額\n'
               'TEST001,廣告A,1000,30,3.0%,150,2,800\n'
               'TEST002,廣告B,2000,50,2.5%,250,5,1500\n')
        r = parse_csv(csv.encode('utf-8-sig'))
        assert len(r.rows) == 2
        assert r.metadata["report_period_start"] == "2026-05-03"
        assert r.metadata["report_period_end"] == "2026-05-03"
        assert r.metadata["period_days"] == 1
        assert r.metadata["is_lifetime_report"] is False
        assert r.metadata["shop_name"] == "科爸好皮"
        # 沒有「日期」欄位但有「期間」摘要 → fallback date
        assert r.rows[0]["date"] == "2026-05-03"

    def test_lifetime_multi_day_warning(self):
        """30 天報表應標記 is_lifetime_report=True"""
        csv = ('所有CPC成效報告 - Shopee台灣\n'
               '期間,2026/04/04 - 2026/05/03\n'
               '\n'
               '商品 ID,廣告名稱,曝光數,點擊數,花費,銷售金額\n'
               'X001,廣告X,30000,900,4500,24000\n')
        r = parse_csv(csv.encode('utf-8-sig'))
        assert r.metadata["is_lifetime_report"] is True
        assert r.metadata["period_days"] == 30
        assert r.summary["is_lifetime_report"] is True
        assert r.rows[0]["date"] == "2026-05-03"  # fallback 用 end_date

    def test_multiple_columns_to_revenue_first_nonzero_wins(self):
        """蝦皮真實 bug: 「銷售金額/直接銷售金額/Vouchered Sales」都對 revenue
        後面 0 不該蓋前面非 0"""
        csv = ('商品 ID,廣告名稱,點擊數,花費,銷售金額,直接銷售金額,Vouchered Sales\n'
               'P001,廣告A,100,500,1500,1200,0\n')
        r = parse_csv(csv.encode('utf-8-sig'))
        assert len(r.rows) == 1
        # 銷售金額 1500 應該保留，不被後面 Vouchered Sales 0 覆蓋
        assert r.rows[0]["revenue"] == 1500.0

    def test_skip_rows_without_metric(self):
        csv = ('日期,商品ID,商品名稱,曝光數,點擊數,花費,銷售額\n'
               '2026-04-01,X1,A品,1000,20,200,1000\n'
               '2026-04-01,X2,B品,,,,\n'  # 全空 → skip
               '2026-04-01,X3,C品,500,5,50,150\n')
        r = parse_csv(csv.encode('utf-8'))
        assert len(r.rows) == 2
        assert r.skipped_rows == 1

    def test_unknown_headers_rejected(self):
        with pytest.raises(ValueError, match="無法辨識"):
            parse_csv(b'foo,bar,baz\n1,2,3\n')

    def test_empty_file(self):
        with pytest.raises((ValueError, Exception)):
            parse_csv(b'')

    def test_tsv(self):
        csv = '日期\t商品ID\t商品名稱\t曝光數\t點擊數\t花費\t銷售額\n2026-04-01\tT1\t測試\t1000\t30\t100\t500\n'
        r = parse_csv(csv.encode('utf-8'))
        assert len(r.rows) == 1

    def test_summary_aggregation(self):
        csv = ('日期,商品ID,商品名稱,曝光數,點擊數,花費,訂單數,銷售額\n'
               '2026-04-01,P1,A,1000,30,150,3,500\n'
               '2026-04-01,P2,B,2000,60,300,6,1200\n')
        r = parse_csv(csv.encode('utf-8-sig'))
        assert r.summary["row_count"] == 2
        assert r.summary["total_spend"] == 450.0
        assert r.summary["total_revenue"] == 1700.0
        assert r.summary["roas"] == pytest.approx(1700/450, rel=1e-3)


class TestFindHeaderRow:
    def test_skips_summary_rows(self):
        rows = [
            ['標題'],
            ['店家', '測試'],
            [],
            ['日期', '商品ID', '商品名稱', '曝光數', '點擊數', '花費', '銷售額'],
            ['2026-04-01', 'P1', 'A', '1000', '30', '150', '500'],
        ]
        idx, score = _find_header_row(rows)
        assert idx == 3
        assert score >= 3
