"""receiver / notify で共通の設定読み込みと Slack メッセージ組み立て"""

import datetime
import json
import os

import boto3

REQUIRED_KEYS = ("botToken", "signingSecret", "adminChannelId", "approverUserIds")


def load_config() -> dict:
    """SSM パラメータストア (SecureString) から Slack の設定を読む。

    値が足りないまま起動したときは例外にする。初期化で失敗した実行環境は再利用されないので、
    パラメータを入れ直したあとは次の呼び出しで読み直される。
    """
    raw = boto3.client("ssm").get_parameter(Name=os.environ["SLACK_PARAM_NAME"], WithDecryption=True)["Parameter"]["Value"]
    cfg = json.loads(raw)
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Slack の設定が未登録です: {missing}")
    return cfg


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec="seconds")


def summary_fields(item: dict, account_id: str | None = None, *, with_account_id: bool = True, user_name: str | None = None) -> list:
    """申請内容の項目 (section の fields)。承認依頼と完了通知で共通

    コードチップはコピーしたり見比べたりする値 (アカウントID・権限セット) だけに使う。
    メールアドレスをバッククォートで囲むと Slack の自動リンクと混ざって崩れるので囲まない
    """
    fields = [{"type": "mrkdwn", "text": f":label: *アカウント名*\n*{item['accountName']}*"}]
    if with_account_id:
        fields.append({"type": "mrkdwn", "text": f":1234: *アカウントID*\n" + (f"`{account_id}`" if account_id else "_(作成後に決まります)_")})
    fields += [
        {"type": "mrkdwn", "text": f":email: *ルートユーザーのメール*\n{item['email']}"},
        {"type": "mrkdwn", "text": f":file_folder: *配置する OU*\n{item.get('ouName') or item['ouId']}"},
    ]
    if item.get("permissionSetArn"):
        fields += [
            # サインインで入力するユーザー名が分かっていれば、メールと並べて出す
            {"type": "mrkdwn", "text": f":bust_in_silhouette: *ログインするユーザー*\n" + (
                    # ユーザー名とメールが違うときだけ並べる (新規作成したユーザーは同じ)
                    f"`{user_name}` ({item['loginEmail']})" if user_name and user_name != item["loginEmail"] else f"`{user_name or item['loginEmail']}`"
                )},
            {"type": "mrkdwn", "text": f":key: *権限セット*\n`{item['permissionSetName']}`"},
        ]
    else:
        fields.append({"type": "mrkdwn", "text": ":key: *権限セット*\n_付与しない (ルートユーザーで入る)_"})
    return fields


def request_blocks(item: dict, *, with_actions: bool = False, footer: str | None = None) -> list:
    """情シスch に出す申請メッセージの blocks"""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": ":inbox_tray: AWS アカウントの払い出し申請", "emoji": True}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<@{item['requester']}> さんからの申請  |  :clock3: {item['requestedAt']}"}],
        },
        {"type": "section", "fields": summary_fields(item, with_account_id=False)},
        {"type": "section", "text": {"type": "mrkdwn", "text": f":memo: *用途*\n>{item.get('purpose') or '-'}"}},
    ]
    if item.get("dryRun"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ":test_tube: *ドライラン* (アカウントは作成しません)"}]})
    blocks.append({"type": "divider"})
    if with_actions:
        blocks.append(
            {
                "type": "actions",
                "block_id": "decision",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approve_request",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "承認"},
                        "value": item["requestId"],
                        "confirm": {
                            "title": {"type": "plain_text", "text": "承認しますか?"},
                            "text": {"type": "mrkdwn", "text": f"*{item['accountName']}* を作成します。"},
                            "confirm": {"type": "plain_text", "text": "承認する"},
                            "deny": {"type": "plain_text", "text": "やめる"},
                        },
                    },
                    {
                        "type": "button",
                        "action_id": "reject_request",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "却下"},
                        "value": item["requestId"],
                    },
                ],
            }
        )
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks
