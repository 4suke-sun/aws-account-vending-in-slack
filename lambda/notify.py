"""Step Functions の最後に呼ばれ、結果を Slack に通知する Lambda

情シスch の申請メッセージのスレッドと、申請者の DM に送る。
申請者が通知だけ見てログインまでたどり着けるよう、申請内容の控えとログイン方法を必ず載せる。
"""

from slack_sdk import WebClient

from common import load_config, summary_fields

cfg = load_config()
client = WebClient(token=cfg["botToken"])


# ---------- rich_text の部品 ----------


def _t(text: str, **style) -> dict:
    return {"type": "text", "text": text, **({"style": style} if style else {})}


def _code(text: str) -> dict:
    return _t(text, code=True)


def _bold(text: str) -> dict:
    return _t(text, bold=True)


def _emoji(name: str) -> dict:
    return {"type": "emoji", "name": name}


def _link(url: str, text: str) -> dict:
    return {"type": "link", "url": url, "text": text}


def _section(*elements) -> dict:
    return {"type": "rich_text_section", "elements": list(elements)}


def _list(style: str, items: list[list[dict]]) -> dict:
    return {"type": "rich_text_list", "style": style, "elements": [_section(*i) for i in items]}


# ---------- ブロック ----------

DIVIDER = {"type": "divider"}


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _login_steps(req: dict, account_id: str | None, user_created: bool, heading: str, user_name: str) -> dict:
    """ログイン手順を番号付きリスト (rich_text) で返す"""
    if not req.get("permissionSetArn"):
        title = f"{heading} (ルートユーザー)"
        steps = [
            [_link("https://signin.aws.amazon.com/", "AWS のサインイン画面"), _t(" で「ルートユーザー」を選び、"), _t(req["email"]), _t(" を入力")],
            [_t("初回はパスワードが未設定なので、「パスワードをお忘れですか?」から設定")],
            [_t("ルートユーザーは普段使いせず、MFA を設定してください。日常の作業用のユーザーは情シスに相談してください")],
        ]
    else:
        title = f"{heading} (IAM Identity Center)"
        account = [_bold(req["accountName"])] + ([_t(" ("), _code(account_id), _t(")")] if account_id else [])
        # サインイン画面で入力するのはユーザー名 (既存ユーザーはメールと違うことがある)
        first = [_t("下のボタンから AWS アクセスポータルを開き、ユーザー名 "), _code(user_name or req["loginEmail"]), _t(" でサインイン")]
        if user_created:
            first.append(_t("\n初めての場合は、届く確認メールのコードを入力してパスワードを設定", italic=True))
        steps = [
            first,
            [_t("アカウントの一覧から "), *account, _t(" を選ぶ")],
            [_code(req["permissionSetName"]), _t(" の「Management console」を押すとコンソールに入れます")],
        ]
    return {"type": "rich_text", "elements": [_section(_emoji("rocket"), _t(" "), _bold(title)), _list("ordered", steps)]}


def _portal(req: dict) -> list:
    if not req.get("portalUrl") or not req.get("permissionSetArn"):
        return []
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    # URL を開くボタンも押すと block_actions がアプリに届く。応答しないと ⚠️ が出るので受付側で ack する
                    "action_id": "open_portal",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": ":door: AWS アクセスポータルを開く", "emoji": True},
                    "url": req["portalUrl"],
                }
            ],
        },
        _context(f":link: {req['portalUrl']}"),
    ]


# ---------- メッセージ ----------


def build_message(event: dict) -> tuple[str, list]:
    req = event["request"]
    result = event["result"]
    name = req["accountName"]
    account_id = event.get("accountId")

    if result == "SUCCEEDED":
        user_created = event.get("userCreated") in (True, "true")
        title = f":tada: {name} の AWS アカウントができました"
        blocks = [
            _header(":tada: AWS アカウントができました"),
            _context(f"*{name}* の払い出しが完了しました"),
            {"type": "section", "fields": summary_fields(req, account_id, user_name=event.get("loginUserName"))},
            DIVIDER,
            _login_steps(req, account_id, user_created, "ログイン方法", event.get("loginUserName", "")),
            *_portal(req),
        ]
        if user_created:
            blocks.append(_context(":sparkles: IAM Identity Center のユーザーを新規作成しました"))
        return title, blocks

    if result == "DRY_RUN":
        title = f":test_tube: {name} のドライランが完了しました (アカウントは作成していません)"
        checks = [[_emoji("white_check_mark"), _t(" Organizations を呼べることを確認 (ルート: "), _code(event["rootId"]), _t(")")]]
        user = {
            "EXISTS": "既存の Identity Center ユーザーに割り当てる予定",
            "WILL_CREATE": "Identity Center のユーザーを新規作成して割り当てる予定",
        }.get(event.get("dryRunUser"))
        if user:
            checks.append([_emoji("white_check_mark"), _t(" "), _t(req["loginEmail"]), _t(f": {user}")])
        blocks = [
            _header(":test_tube: ドライランが完了しました"),
            _context(f"*{name}* の流れを確認しました。アカウントは作成していません"),
            # ✅ 自体が行頭の印になるので、箇条書き (bullet) にはせず1行ずつ並べる
            {"type": "rich_text", "elements": [_section(*[e for i, c in enumerate(checks) for e in ([_t("\n")] if i else []) + c])]},
            {"type": "section", "fields": summary_fields(req, user_name=event.get("loginUserName"))},
            DIVIDER,
            _login_steps(req, None, event.get("dryRunUser") == "WILL_CREATE", "実際に作成された場合のログイン方法", event.get("loginUserName", "")),
            *_portal(req),
        ]
        return title, blocks

    err = event.get("error") or {}
    title = f":warning: {name} の作成に失敗しました"
    blocks = [
        _header(":warning: アカウントの作成に失敗しました"),
        {"type": "section", "fields": summary_fields(req, account_id or None)},
        DIVIDER,
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*エラー*\n```{err.get('Error')}: {err.get('Cause')}```"}},
    ]
    if account_id:
        blocks.append(_context(f":information_source: アカウント `{account_id}` は作成済みです。ログインの設定だけ完了していません"))
    return title, blocks


# 申請者に理由を伝えてよい (申請者側で対処できる) 失敗
REQUESTER_FACING_ERRORS = {
    "UserNameConflict": "ログイン用のユーザー名 (メールアドレス) が Identity Center の別のユーザーで既に使われていました。"
    "申請フォームで、Identity Center に登録済みのメールアドレスを指定して申請し直すか、情シスに相談してください。",
}


def failure_dm(event: dict, title: str) -> str:
    err = (event.get("error") or {}).get("Error")
    reason = REQUESTER_FACING_ERRORS.get(err)
    lines = [title]
    if reason:
        lines.append(f"理由: {reason}")
    if event.get("accountId"):
        lines.append(f"AWS アカウント `{event['accountId']}` は作成済みで、ログインの設定だけ完了していません。")
    lines.append("情シスが確認して連絡します。")
    return "\n".join(lines)


def handler(event, context):
    req = event["request"]
    title, blocks = build_message(event)

    # 情シスch のスレッドにも同じ内容を控えとして残す
    client.chat_postMessage(channel=req["channelId"], thread_ts=req["messageTs"], text=title, blocks=blocks)

    if event["result"] == "FAILED":
        # 申請者には、申請者側で対処できる理由だけ伝える (内部のエラー詳細は情シスch のスレッドにだけ出す)
        client.chat_postMessage(channel=req["requester"], text=failure_dm(event, title))
    else:
        client.chat_postMessage(channel=req["requester"], text=title, blocks=blocks)
    return {"ok": True}
