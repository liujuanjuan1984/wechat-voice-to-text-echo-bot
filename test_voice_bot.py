import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import voice_bot


class FakeAPI:
    def __init__(self):
        self.text_calls = []

    def send_text(self, message, text, *, key):
        self.text_calls.append(
            {
                "message": message,
                "text": text,
                "key": key,
            }
        )
        return {"ret": 0, "message_id": "server-text-1"}


def credentials():
    return voice_bot.Credentials(
        token="secret-token",
        account_id="bot-account",
        base_url="https://example.invalid",
        bound_user_id="bound-user",
    )


def voice_message(**overrides):
    message = {
        "message_id": "message-100",
        "from_user_id": "bound-user",
        "to_user_id": "bot-account",
        "message_type": 1,
        "context_token": "context-1",
        "item_list": [
            {
                "type": 3,
                "msg_id": "voice-item-200",
                "voice_item": {"text": "这是微信提供的转写", "playtime": 2300},
            }
        ],
    }
    message.update(overrides)
    return message


def export_message(**overrides):
    message = {
        "message_id": "export-100",
        "from_user_id": "bound-user",
        "to_user_id": "bot-account",
        "message_type": 1,
        "context_token": "context-2",
        "item_list": [
            {
                "type": 1,
                "msg_id": "export-item-200",
                "text_item": {"text": "导出"},
            }
        ],
    }
    message.update(overrides)
    return message


class VoiceReplyServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_dir = Path(self.tempdir.name)
        self.api = FakeAPI()
        self.service = voice_bot.VoiceReplyService(
            credentials(),
            self.api,
            voice_bot.ProcessedStore(self.state_dir / "processed.json"),
        )

    def test_replies_with_exact_transcription_to_bound_user(self):
        replies = self.service.handle_message(voice_message())

        self.assertEqual(replies, 1)
        self.assertEqual(self.api.text_calls[0]["text"], "这是微信提供的转写")

    def test_ignores_other_user(self):
        replies = self.service.handle_message(
            voice_message(from_user_id="someone-else")
        )

        self.assertEqual(replies, 0)
        self.assertEqual(self.api.text_calls, [])

    def test_ignores_group_and_bot_messages(self):
        self.service.handle_message(voice_message(group_id="group-1"))
        self.service.handle_message(voice_message(message_type=2))

        self.assertEqual(self.api.text_calls, [])

    def test_ignores_voice_without_wechat_transcription(self):
        message = voice_message()
        message["item_list"][0]["voice_item"].pop("text")

        replies = self.service.handle_message(message)

        self.assertEqual(replies, 0)
        self.assertEqual(self.api.text_calls, [])

    def test_does_not_reply_twice_after_restart(self):
        self.service.handle_message(voice_message())
        service_after_restart = voice_bot.VoiceReplyService(
            credentials(),
            self.api,
            voice_bot.ProcessedStore(self.state_dir / "processed.json"),
        )
        service_after_restart.handle_message(voice_message())

        self.assertEqual(len(self.api.text_calls), 1)

    def test_export_joins_cache_with_blank_lines_and_clears_it(self):
        first = voice_message()
        second = voice_message(
            message_id="message-101",
            item_list=[
                {
                    "type": 3,
                    "msg_id": "voice-item-201",
                    "voice_item": {"text": "第二条语音"},
                }
            ],
        )
        self.service.handle_message(first)
        self.service.handle_message(second)

        replies = self.service.handle_message(export_message())

        self.assertEqual(replies, 1)
        self.assertEqual(
            self.api.text_calls[2]["text"],
            "这是微信提供的转写\n\n第二条语音",
        )
        self.assertEqual(self.service.export_cache, [])

        self.service.handle_message(
            voice_message(
                message_id="message-102",
                item_list=[
                    {
                        "type": 3,
                        "msg_id": "voice-item-202",
                        "voice_item": {"text": "导出后的新消息"},
                    }
                ],
            )
        )
        self.service.handle_message(
            export_message(
                message_id="export-101",
                item_list=[
                    {
                        "type": 1,
                        "msg_id": "export-item-201",
                        "text_item": {"text": "导出"},
                    }
                ],
            )
        )
        self.assertEqual(self.api.text_calls[3]["text"], "导出后的新消息")

    def test_empty_export_returns_notice(self):
        self.service.handle_message(export_message())

        self.assertEqual(self.api.text_calls[0]["text"], "暂无可导出的文本。")

    def test_export_is_ignored_from_unbound_user(self):
        self.service.handle_message(export_message(from_user_id="someone-else"))

        self.assertEqual(self.api.text_calls, [])

    def test_duplicate_export_is_not_sent_twice(self):
        self.service.handle_message(voice_message())
        self.service.handle_message(export_message())
        restarted = voice_bot.VoiceReplyService(
            credentials(),
            self.api,
            voice_bot.ProcessedStore(self.state_dir / "processed.json"),
        )
        restarted.handle_message(export_message())

        self.assertEqual(len(self.api.text_calls), 2)


class PayloadTests(unittest.TestCase):
    def test_send_payload_is_plain_text(self):
        message = voice_message()
        api = voice_bot.ILinkAPI("https://example.invalid")
        with patch.object(api, "_send", return_value={"ret": 0}) as sent:
            api.send_text(message, "这是微信提供的转写", key="abc123")
        outbound = sent.call_args.args[0]
        reply_item = outbound["item_list"][0]

        self.assertEqual(outbound["to_user_id"], "bound-user")
        self.assertEqual(outbound["context_token"], "context-1")
        self.assertEqual(outbound["message_type"], voice_bot.BOT_MESSAGE)
        self.assertEqual(reply_item["text_item"]["text"], "这是微信提供的转写")
        self.assertNotIn("ref_msg", reply_item)


class CredentialTests(unittest.TestCase):
    def test_credentials_are_saved_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir) / "state"
            voice_bot.save_credentials(state_dir, credentials())

            path = voice_bot.credentials_path(state_dir)
            loaded = voice_bot.load_credentials(state_dir)
            self.assertEqual(loaded, credentials())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)


class LoginTests(unittest.TestCase):
    def test_login_binds_scanned_user_and_saves_credentials(self):
        class FakeILinkAPI:
            def __init__(self, base_url, token=""):
                self.base_url = base_url
                self.token = token

            def qr_code(self, tokens=()):
                return {
                    "qrcode": "qr-value",
                    "qrcode_img_content": "https://example.invalid/qr",
                }

            def qr_status(self, qr, verify_code=""):
                return {
                    "status": "confirmed",
                    "bot_token": "new-token",
                    "ilink_bot_id": "new-account@im.bot",
                    "baseurl": "https://ilink.example",
                    "ilink_user_id": "scanned-user",
                }

        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(voice_bot, "ILinkAPI", FakeILinkAPI),
            patch.object(voice_bot, "show_qr"),
            patch("builtins.print"),
        ):
            result = voice_bot.login(Path(tempdir), timeout=5)

            self.assertEqual(result.bound_user_id, "scanned-user")
            self.assertEqual(result.account_id, "new-account@im.bot")
            self.assertEqual(
                voice_bot.load_credentials(Path(tempdir)),
                result,
            )


class ProtocolTests(unittest.TestCase):
    def test_session_expiry_codes(self):
        self.assertTrue(voice_bot.is_session_expired(-14, None, ""))
        self.assertTrue(voice_bot.is_session_expired(-2, None, "unknown error"))
        self.assertFalse(voice_bot.is_session_expired(-2, None, "freq limit"))

    def test_retry_delay_is_capped(self):
        self.assertEqual(voice_bot.retry_delay(1), 2)
        self.assertEqual(voice_bot.retry_delay(1000), voice_bot.MAX_BACKOFF)


if __name__ == "__main__":
    unittest.main()
