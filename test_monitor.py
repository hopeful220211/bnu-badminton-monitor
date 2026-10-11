"""python -m unittest test_monitor -v"""

import json
import unittest
from datetime import datetime
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
            with mock.patch.object(m, "query_all", return_value={m.slot_key(s): s for s in slots}):
                m.run_once(self.cfg, st)
        return st

    def test_only_new_slots_notify(self):
        a = {"date": "2099-01-01", "time": "18:00", "court": "1"}
        b = {"date": "2099-01-01", "time": "19:00", "court": "1"}
        self.run_with([[a], [a], [a, b], [b], [a, b]])
        # 首轮 a；第三轮新增 b；第五轮 a 被退出来重新出现
        self.assertEqual(len(self.sent), 3)
        self.assertIn("当前空余场地", self.sent[0])
        self.assertIn("19:00", self.sent[1])
        self.assertIn("18:00", self.sent[2])

    def test_failed_email_retries_next_round(self):
        a = {"date": "2099-01-01", "time": "18:00", "court": "1"}
        st = {"free": {}}
        with mock.patch.object(m, "query_all", return_value={m.slot_key(a): a}), \
             mock.patch.object(m, "notify", return_value=False) as n:
            m.run_once(self.cfg, st)
        self.assertEqual(n.call_count, 1)
        self.assertEqual(st["free"], {})


class SmtpTest(unittest.TestCase):
    def test_qq_ssl(self):
        cfg = {"smtp": {"host": "smtp.qq.com", "port": "465", "user": "a@qq.com",
                        "password": "code", "to": ["b@x.com"]}}
        with mock.patch.object(m.smtplib, "SMTP_SSL") as ssl_cls:
            m.send_email(cfg, "标题", "正文")
        server = ssl_cls.return_value
        server.login.assert_called_once_with("a@qq.com", "code")
        self.assertEqual(server.sendmail.call_args[0][1], ["b@x.com"])


if __name__ == "__main__":
    unittest.main()
