"""Próbák: levélküldés SMTP-n (álszerverrel): szöveg + HTML, ékezetek, hibák."""
import unittest

import _path  # noqa: F401
from ads_engine import config, mailer
from mock_smtp import MockSMTP


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.smtp = MockSMTP().start()

    def tearDown(self):
        self.smtp.stop()

    def settings(self, **over):
        env = {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.port), "SMTP_USER": self.smtp.user, "SMTP_PASSWORD": self.smtp.password,
               "SMTP_FROM": "Pacsi Ads <hello@pacsit.hu>", "REPORT_TO": "ember@pelda.hu, masik@pelda.hu", "SMTP_SECURITY": "plain"}
        env.update(over)
        return config.load(env)

    def test_sends_text_and_html_with_accents(self):
        rcpt = mailer.send(self.settings(), "Heti jelentés – Pacsi", "Szia!\nKöszönöm a figyelmet: őűáé", "<p>Szia <b>Pacsi</b></p>")
        self.assertEqual(rcpt, ["ember@pelda.hu", "masik@pelda.hu"])
        msg = self.smtp.messages[0]
        self.assertEqual(msg["Subject"], "Heti jelentés – Pacsi")
        self.assertIn("őűáé", self.smtp.text_of(msg))
        self.assertIn("<b>Pacsi</b>", self.smtp.html_of(msg))
        self.assertIn("ember@pelda.hu", msg["X-Envelope-To"])
        self.assertTrue(msg["Message-ID"].endswith("@pacsit.hu>"))

    def test_wrong_password_is_a_clear_error(self):
        with self.assertRaises(mailer.MailError) as cm:
            mailer.send(self.settings(SMTP_PASSWORD="rossz"), "x", "y")
        self.assertIn("SMTP_PASSWORD", str(cm.exception))
        self.assertEqual(self.smtp.messages, [])

    def test_missing_config(self):
        with self.assertRaises(mailer.MailError) as cm:
            mailer.send(config.load({}), "x", "y")
        self.assertIn("REPORT_TO", str(cm.exception))

    def test_unreachable_server(self):
        s = self.settings()
        self.smtp.stop()
        with self.assertRaises(mailer.MailError):
            mailer.send(s, "x", "y")

    def test_explicit_recipients_override(self):
        mailer.send(self.settings(), "x", "y", recipients=["csak@pelda.hu"])
        self.assertEqual(self.smtp.messages[0]["X-Envelope-To"], "<csak@pelda.hu>")


if __name__ == "__main__":
    unittest.main()
