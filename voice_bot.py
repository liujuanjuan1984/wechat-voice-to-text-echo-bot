#!/usr/bin/env python3
"""Minimal no-LLM WeChat voice transcription bot."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import random
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import certifi

VERSION = "0.1.0"
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.10"
CLIENT_VERSION = (2 << 16) | (4 << 8) | 10
LONG_POLL_MS = 35_000
MAX_BACKOFF = 30
SESSION_EXPIRED = -14
RATE_LIMIT = -2

ITEM_TEXT = 1
ITEM_VOICE = 3
USER_MESSAGE = 1
BOT_MESSAGE = 2
FINISHED = 2
MAX_PROCESSED = 4096

LOG = logging.getLogger("wechat-voice-to-text-echo-bot")


class ILinkError(RuntimeError):
    pass


class ILinkTimeout(ILinkError):
    pass


class SessionExpired(ILinkError):
    pass


class ILinkResponseError(ILinkError):
    pass


@dataclass(frozen=True)
class Credentials:
    token: str
    account_id: str
    base_url: str
    bound_user_id: str

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Credentials":
        values = (
            str(payload.get("token") or "").strip(),
            str(payload.get("account_id") or "").strip(),
            str(payload.get("base_url") or ILINK_BASE_URL).strip(),
            str(payload.get("bound_user_id") or payload.get("user_id") or "").strip(),
        )
        if not all(values):
            raise ILinkError("credentials are incomplete; run login again")
        return cls(*values)

    def dump(self) -> dict[str, str]:
        return {
            "version": VERSION,
            "token": self.token,
            "account_id": self.account_id,
            "base_url": self.base_url.rstrip("/"),
            "bound_user_id": self.bound_user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def state_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if configured := os.getenv("VOICE_BOT_STATE_DIR", "").strip():
        return Path(configured).expanduser().resolve()
    xdg = os.getenv("XDG_STATE_HOME", "").strip()
    root = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return (root / "wechat-voice-to-text-echo-bot").resolve()


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def write_json(path: Path, data: dict[str, Any]) -> None:
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ILinkError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ILinkError(f"{path} must contain a JSON object")
    return data


def credentials_path(root: Path) -> Path:
    return root / "credentials.json"


def save_credentials(root: Path, credentials: Credentials) -> None:
    write_json(credentials_path(root), credentials.dump())


def load_credentials(root: Path) -> Credentials:
    path = credentials_path(root)
    if not path.exists():
        raise ILinkError("not logged in; run: python voice_bot.py login")
    return Credentials.load(read_json(path))


def mask(value: Any, length: int = 8) -> str:
    text = str(value or "")
    return text if len(text) <= length else f"{text[:length]}…"


def base_info() -> dict[str, str]:
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": f"wechat-voice-to-text-echo-bot/{VERSION}",
    }


class ILinkAPI:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def _headers(self, json_body: bool) -> dict[str, str]:
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(CLIENT_VERSION),
        }
        if json_body:
            uin = random.SystemRandom().randrange(2**32)
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(str(uin).encode()).decode(),
                }
            )
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            data=body,
            headers=self._headers(payload is not None),
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=self.ssl_context
            ) as response:
                raw = response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ILinkResponseError(f"{endpoint} HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ILinkTimeout(f"{endpoint} timed out") from exc
        except urllib.error.URLError as exc:
            if (
                isinstance(exc.reason, (TimeoutError, socket.timeout))
                or "timed out" in str(exc.reason).lower()
            ):
                raise ILinkTimeout(f"{endpoint} timed out") from exc
            raise ILinkError(f"{endpoint} network error: {exc.reason}") from exc
        except OSError as exc:
            raise ILinkError(f"{endpoint} network error: {exc}") from exc

        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ILinkError(f"{endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ILinkError(f"{endpoint} returned a non-object")
        return data

    def qr_code(self, tokens: Iterable[str] = ()) -> dict[str, Any]:
        return self._request(
            "POST",
            "ilink/bot/get_bot_qrcode?bot_type=3",
            payload={"local_token_list": list(tokens)[:10]},
        )

    def qr_status(self, qr: str, verify_code: str = "") -> dict[str, Any]:
        query = {"qrcode": qr}
        if verify_code:
            query["verify_code"] = verify_code
        return self._request(
            "GET",
            f"ilink/bot/get_qrcode_status?{urllib.parse.urlencode(query)}",
            timeout=40,
        )

    def updates(self, cursor: str, timeout_ms: int) -> dict[str, Any] | None:
        try:
            return self._request(
                "POST",
                "ilink/bot/getupdates",
                payload={"get_updates_buf": cursor, "base_info": base_info()},
                timeout=timeout_ms / 1000 + 5,
            )
        except ILinkTimeout:
            return None

    def _send(self, message: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST",
            "ilink/bot/sendmessage",
            payload={"msg": message, "base_info": base_info()},
        )
        try:
            ret = int(result.get("ret") or 0)
            errcode = (
                int(result["errcode"]) if result.get("errcode") is not None else None
            )
        except (TypeError, ValueError):
            ret, errcode = -1, -1
        errmsg = str(result.get("errmsg") or "")
        if is_session_expired(ret, errcode, errmsg):
            raise SessionExpired("iLink session expired; run login again")
        if ret or errcode not in {None, 0}:
            raise ILinkResponseError(f"sendmessage ret={ret} errmsg={errmsg}")
        return result

    def send_text(
        self, message: dict[str, Any], text: str, *, key: str
    ) -> dict[str, Any]:
        outbound = outbound_message(message, key)
        outbound["item_list"] = [{"type": ITEM_TEXT, "text_item": {"text": text}}]
        return self._send(outbound)

    def notify(self, action: str) -> None:
        self._request(
            "POST",
            f"ilink/bot/msg/notify{action}",
            payload={"base_info": base_info()},
        )


def outbound_message(message: dict[str, Any], key: str) -> dict[str, Any]:
    outbound: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": str(message.get("from_user_id") or ""),
        "client_id": f"vtb-{key[:44]}",
        "message_type": BOT_MESSAGE,
        "message_state": FINISHED,
    }
    if token := str(message.get("context_token") or ""):
        outbound["context_token"] = token
    return outbound


def show_qr(url: str) -> None:
    print("\n请使用绑定用户的微信扫描二维码：")
    try:
        import qrcode

        image = qrcode.QRCode(border=1)
        image.add_data(url)
        image.make(fit=True)
        image.print_ascii(invert=True)
    except Exception as exc:
        LOG.warning("terminal QR rendering unavailable: %s", exc)
    print(f"\n二维码链接：{url}\n")


def login(root: Path, timeout: int = 480) -> Credentials:
    private_dir(root)
    existing = None
    if credentials_path(root).exists():
        with contextlib.suppress(ILinkError):
            existing = load_credentials(root)
    tokens = [existing.token] if existing else []
    base_url = ILINK_BASE_URL
    api = ILinkAPI(base_url)
    qr_data = api.qr_code(tokens)
    qr = str(qr_data.get("qrcode") or "")
    if not qr:
        raise ILinkError("QR response did not contain a qrcode")
    show_qr(str(qr_data.get("qrcode_img_content") or qr))

    deadline = time.monotonic() + timeout
    verify_code = ""
    refreshes = 0
    while time.monotonic() < deadline:
        try:
            result = api.qr_status(qr, verify_code)
        except ILinkTimeout:
            continue
        except ILinkError as exc:
            LOG.warning("QR status failed: %s", exc)
            time.sleep(1)
            continue

        status = str(result.get("status") or "wait")
        if status == "wait":
            continue
        if status in {"scaned", "scanned"}:
            print("已扫码，请在手机上确认。")
            verify_code = ""
        elif status == "scaned_but_redirect" and result.get("redirect_host"):
            base_url = f"https://{result['redirect_host']}"
            api = ILinkAPI(base_url)
        elif status == "need_verifycode":
            if not sys.stdin.isatty():
                raise ILinkError("verification code required; run login in a terminal")
            verify_code = input("输入手机微信显示的数字验证码：").strip()
        elif status == "expired":
            refreshes += 1
            if refreshes > 3:
                raise ILinkError("QR code expired repeatedly")
            api = ILinkAPI(ILINK_BASE_URL)
            qr_data = api.qr_code(tokens)
            qr = str(qr_data.get("qrcode") or "")
            if not qr:
                raise ILinkError("refreshed QR response did not contain a qrcode")
            show_qr(str(qr_data.get("qrcode_img_content") or qr))
            verify_code = ""
        elif status == "verify_code_blocked":
            raise ILinkError("verification blocked; run login again later")
        elif status == "binded_redirect":
            if existing:
                return existing
            raise ILinkError("Bot is already bound and no local credential exists")
        elif status == "confirmed":
            credentials = Credentials(
                token=str(result.get("bot_token") or "").strip(),
                account_id=str(result.get("ilink_bot_id") or "").strip(),
                base_url=str(result.get("baseurl") or base_url).strip(),
                bound_user_id=str(result.get("ilink_user_id") or "").strip(),
            )
            if not all(
                (
                    credentials.token,
                    credentials.account_id,
                    credentials.bound_user_id,
                )
            ):
                raise ILinkError("login response is missing credentials")
            save_credentials(root, credentials)
            print(
                f"登录成功。Bot={mask(result.get('ilink_bot_id'))}，"
                f"唯一绑定用户={mask(result.get('ilink_user_id'))}。"
            )
            return credentials
        else:
            LOG.warning("unknown QR status: %s", mask(status))
            time.sleep(1)
    raise ILinkError("QR login timed out")


class ProcessedStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.keys = [
            str(key)
            for key in (read_json(path).get("keys") or [])
            if isinstance(key, str)
        ]
        self.known = set(self.keys)

    def add(self, key: str) -> None:
        if key in self.known:
            return
        self.keys.append(key)
        self.known.add(key)
        if len(self.keys) > MAX_PROCESSED:
            removed = self.keys[:-MAX_PROCESSED]
            self.keys = self.keys[-MAX_PROCESSED:]
            self.known.difference_update(removed)
        write_json(
            self.path,
            {"version": 1, "keys": self.keys, "updated_at": int(time.time())},
        )


def message_key(message: dict[str, Any], item: dict[str, Any], index: int) -> str:
    text = str(
        (item.get("text_item") or {}).get("text")
        or (item.get("voice_item") or {}).get("text")
        or ""
    )
    raw = "\0".join(
        [
            str(message.get("message_id") or ""),
            str(item.get("msg_id") or ""),
            str(message.get("from_user_id") or ""),
            str(message.get("create_time_ms") or ""),
            str(index),
            text,
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def voice_items(
    message: dict[str, Any],
) -> Iterable[tuple[int, dict[str, Any], str]]:
    for index, item in enumerate(message.get("item_list") or []):
        if not isinstance(item, dict) or item.get("type") != ITEM_VOICE:
            continue
        text = str((item.get("voice_item") or {}).get("text") or "").strip()
        if text:
            yield index, item, text


def export_item(message: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    for index, item in enumerate(message.get("item_list") or []):
        if not isinstance(item, dict) or item.get("type") != ITEM_TEXT:
            continue
        if str((item.get("text_item") or {}).get("text") or "").strip() == "导出":
            return index, item
    return None


def allowed(message: dict[str, Any], credentials: Credentials) -> bool:
    try:
        is_user = int(message.get("message_type")) == USER_MESSAGE
    except (TypeError, ValueError):
        return False
    group_fields = ("group_id", "room_id", "chat_room_id")
    return (
        is_user
        and not any(message.get(field) for field in group_fields)
        and str(message.get("from_user_id") or "") == credentials.bound_user_id
        and str(message.get("to_user_id") or "") in {"", credentials.account_id}
    )


class VoiceReplyService:
    def __init__(
        self,
        credentials: Credentials,
        api: ILinkAPI,
        processed: ProcessedStore,
    ) -> None:
        self.credentials = credentials
        self.api = api
        self.processed = processed
        self.export_cache: list[str] = []

    def handle_message(self, message: dict[str, Any]) -> int:
        if not allowed(message, self.credentials):
            return 0
        if command := export_item(message):
            return self._export(message, *command)

        replies = 0
        voice_seen = False
        for index, item, text in voice_items(message):
            voice_seen = True
            key = message_key(message, item, index)
            if key in self.processed.known:
                continue
            response = self.api.send_text(message, text, key=key)
            self.processed.add(key)
            self.export_cache.append(text)
            replies += 1
            LOG.info(
                "replied chars=%d server_id=%s",
                len(text),
                mask(response.get("message_id"), 12),
            )

        if not voice_seen:
            types = [
                item.get("type")
                for item in (message.get("item_list") or [])
                if isinstance(item, dict)
            ]
            if ITEM_VOICE in types:
                LOG.warning("voice has no WeChat transcription; ignored")
        return replies

    def _export(
        self,
        message: dict[str, Any],
        index: int,
        item: dict[str, Any],
    ) -> int:
        key = message_key(message, item, index)
        if key in self.processed.known:
            return 0
        count = len(self.export_cache)
        text = "\n\n".join(self.export_cache) if count else "暂无可导出的文本。"
        self.api.send_text(message, text, key=key)
        self.processed.add(key)
        self.export_cache.clear()
        LOG.info("exported count=%d chars=%d", count, len(text))
        return 1


def response_status(response: dict[str, Any]) -> tuple[int, int | None, str]:
    try:
        ret = int(response.get("ret") or 0)
        errcode = (
            int(response["errcode"]) if response.get("errcode") is not None else None
        )
    except (TypeError, ValueError):
        return -1, -1, ""
    return ret, errcode, str(response.get("errmsg") or "")


def is_session_expired(ret: int, errcode: int | None, errmsg: str) -> bool:
    if SESSION_EXPIRED in {ret, errcode}:
        return True
    return RATE_LIMIT in {ret, errcode} and errmsg.lower() == "unknown error"


def retry_delay(failures: int) -> int:
    return min(MAX_BACKOFF, 2 ** min(max(failures, 1), 5))


@contextlib.contextmanager
def process_lock(root: Path):
    private_dir(root)
    with (root / "bot.lock").open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ILinkError(f"another bot is using {root}") from exc
        yield


def load_cursor(path: Path) -> str:
    return str(read_json(path).get("get_updates_buf") or "")


def save_cursor(path: Path, cursor: str) -> None:
    write_json(path, {"version": 1, "get_updates_buf": cursor})


def serve(root: Path) -> int:
    credentials = load_credentials(root)
    api = ILinkAPI(credentials.base_url, credentials.token)
    with process_lock(root):
        service = VoiceReplyService(
            credentials,
            api,
            ProcessedStore(root / "processed.json"),
        )
        cursor_path = root / "sync.json"
        cursor = load_cursor(cursor_path)
        timeout_ms = LONG_POLL_MS
        failures = 0
        LOG.info(
            "started bot=%s bound_user=%s",
            mask(credentials.account_id),
            mask(credentials.bound_user_id),
        )
        with contextlib.suppress(ILinkError):
            api.notify("start")
        try:
            while True:
                try:
                    response = api.updates(cursor, timeout_ms)
                    if response is None:
                        continue
                    ret, errcode, errmsg = response_status(response)
                    if ret or errcode not in {None, 0}:
                        if is_session_expired(ret, errcode, errmsg):
                            raise SessionExpired("session expired; run login again")
                        failures += 1
                        delay = retry_delay(failures)
                        LOG.warning(
                            "getupdates ret=%s errcode=%s errmsg=%s; retry=%ss",
                            ret,
                            errcode,
                            errmsg[:120],
                            delay,
                        )
                        time.sleep(delay)
                        continue

                    failures = 0
                    suggested = response.get("longpolling_timeout_ms")
                    if isinstance(suggested, int) and 1000 <= suggested <= 120_000:
                        timeout_ms = suggested
                    for message in response.get("msgs") or []:
                        if isinstance(message, dict):
                            service.handle_message(message)
                    if next_cursor := str(response.get("get_updates_buf") or ""):
                        cursor = next_cursor
                        save_cursor(cursor_path, cursor)
                except SessionExpired:
                    raise
                except ILinkError as exc:
                    failures += 1
                    delay = retry_delay(failures)
                    LOG.warning("poll/send failed: %s; retry=%ss", exc, delay)
                    time.sleep(delay)
        finally:
            with contextlib.suppress(ILinkError):
                api.notify("stop")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    commands = root.add_subparsers(dest="command", required=True)
    login_cmd = commands.add_parser("login", help="bind one WeChat user by QR")
    login_cmd.add_argument("--state-dir")
    login_cmd.add_argument("--timeout", type=int, default=480)
    serve_cmd = commands.add_parser("serve", help="run the bot")
    serve_cmd.add_argument("--state-dir")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    root = state_dir(args.state_dir)
    try:
        if args.command == "login":
            login(root, max(1, args.timeout))
            return 0
        return serve(root)
    except SessionExpired as exc:
        LOG.error("%s", exc)
        return 2
    except ILinkError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
