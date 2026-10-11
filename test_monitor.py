"""python -m unittest test_monitor -v"""

import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import monitor as m

CHROME_CURL = r"""curl 'https://tycg.bnu.edu.cn/api/slots?d=2026-10-14' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'accept-encoding: gzip, deflate, br, zstd' \
  -H 'authorization: Bearer abc.def' \
  -b 'JSESSIONID=XYZ; route=1' \
  --data-raw $'{"date":"2026-10-14","name":"羽毛球"}' \
  --compressed"""

PARSER = {
    "ok_when": {"field": "code", "in": [200]},
    "list_path": "data.courts.*.slots.*",
    "free_when": {"field": "status", "in": [0]},
    "fields": {"time": "time", "court": "../name"},
}


def resp(code=200, msg="ok", slots=((("1号场"), "18:00-19:00", 0), ("1号场", "19:00-20:00", 1))):
    courts = {}
    for c, t, s in slots:
        courts.setdefault(c, []).append({"time": t, "status": s})
    return json.dumps({"code": code, "msg": msg, "data": {"courts": [
        {"name": c, "slots": v} for c, v in courts.items()]}}, ensure_ascii=False)


class CurlTest(unittest.TestCase):
    def test_chrome_bash(self):
        r = m.parse_curl(CHROME_CURL)
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["url"], "https://tycg.bnu.edu.cn/api/slots?d=2026-10-14")
        self.assertEqual(r["headers"]["Cookie"], "JSESSIONID=XYZ; route=1")
        self.assertEqual(r["headers"]["authorization"], "Bearer abc.def")
        self.assertEqual(r["headers"]["Accept-Encoding"], "gzip, deflate")
        self.assertNotIn("accept-encoding", r["headers"])
        self.assertEqual(json.loads(r["data"])["name"], "羽毛球")

    def test_date_substitution(self):
        r = m.substitute(m.parse_curl(CHROME_CURL), "2026-10-14", "2026-10-15")
        self.assertIn("2026-10-15", r["url"])
        self.assertIn("2026-10-15", r["data"])

    def test_rejects_cmd_format(self):
        with self.assertRaises(ValueError):
            m.parse_curl('fetch("https://x")')


class ParseTest(unittest.TestCase):
    def test_free_slots_with_parent_field(self):
        slots = m.extract_free_slots(resp(), PARSER, "2026-10-14")
        self.assertEqual(slots, [{"time": "18:00-19:00", "court": "1号场", "date": "2026-10-14"}])

    def test_login_expired_message(self):
        with self.assertRaises(m.SessionExpired):
            m.extract_free_slots(resp(code=401, msg="登录已过期"), PARSER, "")

    def test_other_error(self):
        with self.assertRaises(RuntimeError):
            m.extract_free_slots(resp(code=500, msg="系统繁忙"), PARSER, "")

    def test_regex_mode(self):
        p = {"mode": "regex", "pattern": r'<td class="free" data-t="(?P<time>[^"]+)">(?P<court>[^<]+)'}
        html = '<td class="free" data-t="18:00">3号场</td><td class="busy" data-t="19:00">3号场</td>'
        self.assertEqual(m.extract_free_slots(html, p, "d"), [{"time": "18:00", "court": "3号场", "date": "d"}])

    def test_already_started(self):
        t = datetime(2026, 10, 14, 18, 30, tzinfo=m.BEIJING)
        self.assertTrue(m.already_started({"date": "2026-10-14", "time": "18:00-19:00"}, t))
        self.assertFalse(m.already_started({"date": "2026-10-14", "time": "19:00-20:00"}, t))
        self.assertFalse(m.already_started({"date": "2026-10-15", "time": "08:00-09:00"}, t))


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(m.DEFAULTS, parser=PARSER, smtp={"dry_run": True})
        self.sent = []
        p1 = mock.patch.object(m, "send_email", lambda cfg, s, b: self.sent.append(s))
        p1.start()
        self.addCleanup(p1.stop)

    def run_with(self, slots_by_round):
        st = {}
        for slots in slots_by_round:
            with mock.patch.object(m, "query_all", return_value=({m.slot_key(s): s for s in slots}, [])):
                m.run_once(self.cfg, st)
        return st

    def test_only_new_slots_notify(self):
        a = {"date": "2099-01-01", "time": "18:00", "court": "1"}
        b = {"date": "2099-01-01", "time": "19:00", "court": "1"}
        self.run_with([[a], [a], [a, b], [b], [a, b]])
        # 首轮 a；第三轮新增 b；第五轮 a 被退出来重新出现
        self.assertEqual(len(self.sent), 3)
        self.assertIn("当前空场", self.sent[0])
        self.assertIn("19:00", self.sent[1])
        self.assertIn("18:00", self.sent[2])


class RulesTest(unittest.TestCase):
    """10-22 点、只看羽1-羽8、连续场特别提醒、邮件格式。"""

    def setUp(self):
        self.cfg = m.load_config(Path(__file__).parent / "config.json")
        self.today = m.now().date()

    def d(self, n):
        return (self.today + m.timedelta(days=n)).strftime("%Y-%m-%d")

    def s(self, n, t, court):
        return {"date": self.d(n), "time": t, "court": court}

    def test_filters(self):
        ok = lambda t, c: m.wanted(self.s(1, t, c), self.cfg)
        self.assertTrue(ok("10:00-11:00", "羽1"))
        self.assertTrue(ok("21:00-22:00", "羽8"))
        self.assertFalse(ok("09:00-10:00", "羽1"))
        self.assertFalse(ok("08:00-09:00", "羽4"))
        self.assertFalse(ok("12:00-13:00", "小综合1"))
        self.assertFalse(ok("12:00-13:00", "二层东"))
        self.assertFalse(ok("12:00-13:00", "羽10"))

    def test_format(self):
        self.assertEqual(m.short_time("08:00-09:00"), "8-9点")
        self.assertEqual(m.short_time("10:00-11:00"), "10-11点")
        self.assertEqual(m.short_time("18:30-19:30"), "18:30-19:30点")
        self.assertEqual(m.date_label(self.d(0)), f"{self.d(0)}（今天）")
        self.assertEqual(m.date_label(self.d(1)), f"{self.d(1)}（明天）")
        self.assertEqual(m.date_label(self.d(3)), f"{self.d(3)}（大后天）")
        self.assertEqual(m.slot_text(self.s(2, "08:00-09:00", "羽4")), f"{self.d(2)}（后天） 8-9点 羽4")

    def free(self, *slots):
        return {m.slot_key(x): x for x in slots}

    def test_regular_email(self):
        f = self.free(self.s(1, "11:00-12:00", "羽1"), self.s(1, "13:00-14:00", "羽1"))
        subject, body = m.compose_email(f, list(f), False, 2)
        self.assertTrue(subject.startswith("🏸 新空场："))
        self.assertNotIn("特别", subject)
        for word in ("邱季端", "去预约", "检测时间", ":00"):
            self.assertNotIn(word, body)
        self.assertIn("11-12点 羽1", body)

    def test_streak_email(self):
        old = self.s(1, "11:00-12:00", "羽1")
        new = self.s(1, "12:00-13:00", "羽1")
        other = self.s(1, "12:00-13:00", "羽2")
        f = self.free(old, new, other)
        subject, body = m.compose_email(f, [m.slot_key(new), m.slot_key(other)], False, 2)
        self.assertIn("特别提醒", subject)
        self.assertIn("11-13点 羽1（连续2小时）", subject)
        self.assertIn("其他新空场（1 个）", body)
        self.assertIn("12-13点 羽2", body)

    def test_streak_only_when_new_slot_involved(self):
        a, b = self.s(1, "11:00-12:00", "羽1"), self.s(1, "12:00-13:00", "羽1")
        c = self.s(2, "15:00-16:00", "羽3")
        f = self.free(a, b, c)
        subject, _ = m.compose_email(f, [m.slot_key(c)], False, 2)
        self.assertNotIn("特别提醒", subject)

    def test_streak_needs_same_court_and_adjacent(self):
        f = self.free(self.s(1, "11:00-12:00", "羽1"), self.s(1, "12:00-13:00", "羽2"),
                      self.s(1, "14:00-15:00", "羽1"))
        self.assertEqual(m.find_runs(f, 2), [])

    def test_sample_alerts(self):
        sent = []
        with mock.patch.object(m, "send_email", lambda c, subj, body: sent.append((subj, body))):
            m.sample_alerts(self.cfg)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0][0].startswith("[测试] 🏸 新空场："))
        self.assertTrue(sent[1][0].startswith("[测试] 🔥 特别提醒·连续场："))
        self.assertIn("19-21点 羽2（连续2小时）", sent[1][0])

    def test_avoid_burst_handover(self):
        def ts(h, mi):
            return datetime(2026, 10, 12, h, mi, tzinfo=m.BEIJING).timestamp()
        five_half = 330 * 60
        # 02:00 开班 → 07:30 才交班，正撞 7:30 放场 → 提前到 07:10
        got = m.avoid_burst_handover(self.cfg, ts(2, 0), five_half)
        self.assertEqual(datetime.fromtimestamp(ts(2, 0) + got, m.BEIJING).strftime("%H:%M"), "07:10")
        # 01:00 开班 → 06:30 交班，离冲刺远 → 不动
        self.assertEqual(m.avoid_burst_handover(self.cfg, ts(1, 0), five_half), five_half)
        # 07:12 开班 → 12:42 交班 → 不动
        self.assertEqual(m.avoid_burst_handover(self.cfg, ts(7, 12), five_half), five_half)

    def test_test_email_body(self):
        body = m.test_email_body(self.cfg)
        self.assertIn("特别提醒", body)
        self.assertIn("连续2小时", body)

    def test_failed_email_retries_next_round(self):
        a = {"date": "2099-01-01", "time": "18:00", "court": "1"}
        st = {"free": {}}
        with mock.patch.object(m, "query_all", return_value=({m.slot_key(a): a}, [])), \
             mock.patch.object(m, "notify", return_value=False) as n:
            m.run_once(self.cfg, st)
        self.assertEqual(n.call_count, 1)
        self.assertEqual(st["free"], {})


BNU_CURL = r"""curl --url 'https://tycg.bnu.edu.cn/www/tycg/app/getYdxcd' \
  -H 'Accept: */*' \
  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
  -b '_ga=GA1.1.1; _ga_X=GS2.1.s1$o5$g0$t1$j60$l0$h0; dd.SessionId=fake; dd.CookieId=fake%2B%3D%3D' \
  -H 'Origin: https://tycg.bnu.edu.cn' \
  -H 'X-Requested-With: XMLHttpRequest' \
  --data-raw 'dataOne=%7B%22cgid%22%3A%226fdb8db0%22%2C%22xmlx%22%3A%225%22%2C%22yyrq%22%3A%222026-10-14%22%2C%22xydm%22%3A%2211%22%7D'"""


class BnuTest(unittest.TestCase):
    """北师大 getYdxcd 接口（结构取自真实返回，内容裁剪过）。"""

    def setUp(self):
        self.cfg = m.load_config(Path(__file__).parent / "config.json")
        self.text = (Path(__file__).parent / "tests_data" / "bnu_getYdxcd.json").read_text(encoding="utf-8")

    def test_curl_and_date(self):
        r = m.substitute(m.parse_curl(BNU_CURL), self.cfg["date_in_request"], "2026-10-15")
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["url"], "https://tycg.bnu.edu.cn/www/tycg/app/getYdxcd")
        self.assertIn("dd.SessionId=fake", r["headers"]["Cookie"])
        self.assertIn("%222026-10-15%22", r["data"])

    def test_parse(self):
        slots = m.extract_free_slots(self.text, self.cfg["parser"], "2026-10-14")
        got = sorted((s["court"], s["time"]) for s in slots)
        self.assertEqual(got, [("小综合1", "21:00-22:00"), ("羽4", "08:00-09:00")])
        self.assertEqual(slots[0]["date"], "2026-10-14")

    def test_failure_response(self):
        bad = json.dumps({"success": 0, "message": "请先登录", "data": None}, ensure_ascii=False)
        with self.assertRaises(m.SessionExpired):
            m.extract_free_slots(bad, self.cfg["parser"], "")
        bad = json.dumps({"success": 0, "message": "不在预约时间内", "data": None}, ensure_ascii=False)
        with self.assertRaises(RuntimeError):
            m.extract_free_slots(bad, self.cfg["parser"], "")

    def test_release_time_hides_fourth_day(self):
        before = datetime(2026, 10, 12, 7, 29, tzinfo=m.BEIJING)
        after = datetime(2026, 10, 12, 7, 30, tzinfo=m.BEIJING)
        self.assertEqual(m.target_dates(self.cfg, before)[-1], "2026-10-14")
        self.assertEqual(m.target_dates(self.cfg, after)[-1], "2026-10-15")

    def test_failed_date_does_not_renotify(self):
        cfg = dict(self.cfg, smtp={"dry_run": True})
        a = {"date": "2099-01-01", "time": "08:00-09:00", "court": "羽4"}
        b = {"date": "2099-01-02", "time": "08:00-09:00", "court": "羽4"}
        sent = []
        st = {}
        rounds = [({m.slot_key(a): a, m.slot_key(b): b}, []),
                  ({m.slot_key(a): a}, ["2099-01-02"]),   # 01-02 这天查询失败
                  ({m.slot_key(a): a, m.slot_key(b): b}, [])]
        with mock.patch.object(m, "send_email", lambda c, s, body: sent.append(s)):
            for r in rounds:
                with mock.patch.object(m, "query_all", return_value=r):
                    m.run_once(cfg, st)
        self.assertEqual(len(sent), 1)  # 只有首轮那一封


class SmtpTest(unittest.TestCase):
    def test_qq_ssl(self):
        cfg = {"smtp": {"host": "smtp.qq.com", "port": "465", "user": "a@qq.com",
                        "password": "code", "to": ["b@x.com"]}}
        with mock.patch.object(m.smtplib, "SMTP_SSL") as ssl_cls:
            server = ssl_cls.return_value
            server.mail.return_value = (250, b"OK")
            server.rcpt.return_value = (250, b"OK")
            server.data.return_value = (250, b"OK: queued as 123")
            m.send_email(cfg, "标题", "正文")
        server.login.assert_called_once_with("a@qq.com", "code")
        server.rcpt.assert_called_once_with("b@x.com")
        raw = server.data.call_args[0][0]
        self.assertIn("Message-ID:", raw)
        self.assertIn("Date:", raw)

    def test_recipient_refused(self):
        cfg = {"smtp": {"host": "smtp.qq.com", "user": "a@qq.com", "password": "c", "to": "bad@x.com "}}
        with mock.patch.object(m.smtplib, "SMTP_SSL") as ssl_cls:
            server = ssl_cls.return_value
            server.mail.return_value = (250, b"OK")
            server.rcpt.return_value = (550, b"Mailbox not found")
            with self.assertRaises(m.smtplib.SMTPRecipientsRefused):
                m.send_email(cfg, "标题", "正文")
        server.rcpt.assert_called_once_with("bad@x.com")


if __name__ == "__main__":
    unittest.main()
