"""Slack からのリクエスト (スラッシュコマンド / ショートカット / モーダル / ボタン) を受ける Lambda

Slack には3秒以内に応答を返す必要があるので、応答 (ack) と後続処理を lazy listener で分ける。
lazy listener は同じ関数を非同期で呼び直して動く。
"""

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk.errors import SlackApiError

from common import load_config, now_iso, request_blocks

cfg = load_config()
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sfn = boto3.client("stepfunctions")
# Organizations の API はレートが低めなので、スロットリングは SDK 側で待って再試行する
org = boto3.client("organizations", config=Config(retries={"mode": "adaptive", "max_attempts": 5}))
sso = boto3.client("sso-admin", config=Config(retries={"mode": "adaptive", "max_attempts": 5}))

app = App(
    token=cfg["botToken"],
    signing_secret=cfg["signingSecret"],
    process_before_response=True,  # Lambda では応答を返した時点で処理が止まるため必須
    token_verification_enabled=False,  # 起動時の auth.test を省いてコールドスタートを短くする
)

# CreateAccount の AccountName は ASCII の表示可能文字のみ、50文字まで
ACCOUNT_NAME_RE = re.compile(r"[\x20-\x7E]{1,50}")


# ---------- OU の選択肢 ----------

_ou_cache: dict = {"at": 0.0, "options": []}
OU_CACHE_SECONDS = 300
MAX_OPTIONS = 100  # static_select の選択肢の上限
MAX_LABEL = 75  # 選択肢のテキストの上限


def _child_ous(parent_id: str) -> list[dict]:
    ous = []
    for page in org.get_paginator("list_organizational_units_for_parent").paginate(ParentId=parent_id):
        ous += page["OrganizationalUnits"]
    return ous


def ou_options() -> list[dict]:
    """組織の OU を入れ子も含めて取り、「親 / 子 / 孫」のパスを名前にする。先頭は「ルート直下 (OU なし)」

    固定の設定にすると、OU を消したり増やしたりしても反映されない。
    モーダルは3秒以内に開く必要があるので、同じ階層はまとめて並列に取り、実行環境ごとに5分キャッシュする
    """
    if time.time() - _ou_cache["at"] < OU_CACHE_SECONDS:
        return _ou_cache["options"]
    root_id = org.list_roots()["Roots"][0]["Id"]
    found = []
    level = [(root_id, "")]
    with ThreadPoolExecutor(max_workers=5) as pool:
        while level:
            results = pool.map(lambda p: [(ou, p[1]) for ou in _child_ous(p[0])], level)
            level = []
            for children in results:
                for ou, parent_path in children:
                    path = f"{parent_path} / {ou['Name']}" if parent_path else ou["Name"]
                    found.append({"label": path, "id": ou["Id"]})
                    level.append((ou["Id"], path))
    found.sort(key=lambda o: o["label"])
    options = [{"label": "ルート直下 (OU なし)", "id": root_id}] + found
    options = options[:MAX_OPTIONS]
    for o in options:
        if len(o["label"]) > MAX_LABEL:
            o["label"] = "…" + o["label"][-(MAX_LABEL - 1):]
    _ou_cache.update(at=time.time(), options=options)
    return options


# ---------- IAM Identity Center ----------

NO_PERMISSION_SET = "none"
_ps_cache: dict = {"at": 0.0, "options": []}
_instance: dict = {}


def identity_center() -> dict:
    """組織の Identity Center インスタンス。変わらないので実行環境ごとに1回だけ取る"""
    if not _instance:
        ins = sso.list_instances()["Instances"][0]
        _instance.update(
            instanceArn=ins["InstanceArn"],
            identityStoreId=ins["IdentityStoreId"],
            # カスタムのサブドメインを設定していない場合のアクセスポータルの URL
            portalUrl=f"https://{ins['IdentityStoreId']}.awsapps.com/start",
        )
    return _instance


def permission_set_options() -> list[dict]:
    """権限セットの一覧。先頭は「付与しない」。OU と同じく5分キャッシュ"""
    if time.time() - _ps_cache["at"] < OU_CACHE_SECONDS:
        return _ps_cache["options"]
    arn = identity_center()["instanceArn"]
    ps_arns = []
    for page in sso.get_paginator("list_permission_sets").paginate(InstanceArn=arn):
        ps_arns += page["PermissionSets"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        names = pool.map(lambda a: sso.describe_permission_set(InstanceArn=arn, PermissionSetArn=a)["PermissionSet"]["Name"], ps_arns)
        found = sorted(({"label": n, "id": a} for n, a in zip(names, ps_arns)), key=lambda o: o["label"])
    options = [{"label": "付与しない (ルートユーザーで入る)", "id": NO_PERMISSION_SET}] + found[: MAX_OPTIONS - 1]
    _ps_cache.update(at=time.time(), options=options)
    return options


# ---------- 申請フォームを開く ----------


def build_modal() -> dict:
    # OU と権限セットは別々の API なので並列に取る (3秒以内にモーダルを開くため)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ou_future = pool.submit(ou_options)
        ps_future = pool.submit(permission_set_options)
        options, ps_options = ou_future.result(), ps_future.result()
    return {
        "type": "modal",
        "callback_id": "aws_account_request",
        "title": {"type": "plain_text", "text": "AWSアカウント申請"},
        "submit": {"type": "plain_text", "text": "申請する"},
        "close": {"type": "plain_text", "text": "やめる"},
        "blocks": [
            {
                "type": "input",
                "block_id": "account_name",
                "label": {"type": "plain_text", "text": "アカウント名"},
                "hint": {"type": "plain_text", "text": "半角英数字と記号のみ、50文字まで (日本語は使えません)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "max_length": 50,
                    "placeholder": {"type": "plain_text", "text": "例: sandbox-taro-01"},
                },
            },
            {
                "type": "input",
                "block_id": "email",
                "label": {"type": "plain_text", "text": "ルートユーザーのメールアドレス"},
                "hint": {"type": "plain_text", "text": "他の AWS アカウントで使っていないアドレス。+ 付きアドレスも使えます"},
                "element": {"type": "email_text_input", "action_id": "value"},
            },
            {
                "type": "input",
                "block_id": "ou",
                "label": {"type": "plain_text", "text": "配置する OU"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "initial_option": _ou_option(options[0]),
                    "options": [_ou_option(o) for o in options],
                },
            },
            {
                "type": "input",
                "block_id": "permission_set",
                "label": {"type": "plain_text", "text": "ログイン権限 (Identity Center の権限セット)"},
                "hint": {"type": "plain_text", "text": "作成したアカウントに、申請者がこの権限でログインできるようにします"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    # 初期値は強すぎない PowerUserAccess (なければ「付与しない」)
                    "initial_option": _ou_option(next((o for o in ps_options if o["label"] == "PowerUserAccess"), ps_options[0])),
                    "options": [_ou_option(o) for o in ps_options],
                },
            },
            {
                "type": "input",
                "block_id": "login_source",
                "label": {"type": "plain_text", "text": "ログインに使うユーザー"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "value",
                    "initial_option": _radio("slack", "自分の Slack のメールアドレス", "Identity Center で同じメールのユーザーを使う (なければ作る)"),
                    "options": [
                        _radio("slack", "自分の Slack のメールアドレス", "Identity Center で同じメールのユーザーを使う (なければ作る)"),
                        _radio("form", "下に入力したメールアドレス", "Slack とは別のアドレスで Identity Center を使っている場合"),
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "login_email",
                "optional": True,
                "label": {"type": "plain_text", "text": "ログインに使うメールアドレス"},
                "hint": {"type": "plain_text", "text": "上で「下に入力したメールアドレス」を選んだときだけ入力"},
                "element": {"type": "email_text_input", "action_id": "value"},
            },
            {
                "type": "input",
                "block_id": "purpose",
                "label": {"type": "plain_text", "text": "用途"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True, "max_length": 200},
            },
            {
                "type": "input",
                "block_id": "options",
                "optional": True,
                "label": {"type": "plain_text", "text": "オプション"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "value",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "ドライラン"},
                            "description": {"type": "plain_text", "text": "アカウントは作らず、流れだけ確認する"},
                            "value": "dry_run",
                        }
                    ],
                },
            },
        ],
    }


def _ou_option(o: dict) -> dict:
    return {"text": {"type": "plain_text", "text": o["label"]}, "value": o["id"]}


def _radio(value: str, text: str, description: str) -> dict:
    return {"text": {"type": "plain_text", "text": text}, "description": {"type": "plain_text", "text": description}, "value": value}


LOADING_PREFIX = "loading-"


def _fresh(cache: dict) -> bool:
    return time.time() - cache["at"] < OU_CACHE_SECONDS


def _caches_warm() -> bool:
    return _fresh(_ou_cache) and _fresh(_ps_cache)


def loading_modal(external_id: str) -> dict:
    """OU と権限セットを取っている間に先に開く「準備中」のモーダル (送信ボタンなし)"""
    return {
        "type": "modal",
        "external_id": external_id,
        "title": {"type": "plain_text", "text": "AWSアカウント申請"},
        "close": {"type": "plain_text", "text": "やめる"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: フォームを準備しています…"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "組織の OU と権限セットの一覧を読み込んでいます"}]},
        ],
    }


def open_modal(ack, body, client):
    """3秒以内に必ずモーダルを開く

    trigger_id の期限 (3秒) があるのは views.open だけで、開いたあとの views.update には期限がない。
    キャッシュが冷えているとき (コールドスタート直後など) は、API を呼ばずに「準備中」のモーダルを先に開き、
    中身は lazy listener (fill_modal) で差し替える。lazy には views.open の戻り値 (view_id) を渡せないので、
    trigger_id から作った external_id で同じモーダルを指定する
    """
    ack()
    started = time.time()
    warm = _caches_warm()
    try:
        if warm:
            client.views_open(trigger_id=body["trigger_id"], view=build_modal())
        else:
            client.views_open(trigger_id=body["trigger_id"], view=loading_modal(LOADING_PREFIX + body["trigger_id"]))
    except SlackApiError as e:
        if e.response.get("error") != "expired_trigger_id":
            raise
        client.chat_postMessage(
            channel=body["user_id"] if "user_id" in body else body["user"]["id"],
            text="フォームを開くのに時間がかかりすぎました。もう一度 `/aws-account` を実行してください",
        )
    print(f"open_modal took {time.time() - started:.2f}s (cache {'warm' if warm else 'cold'})")


def fill_modal(body, client):
    """「準備中」で開いたモーダルを本物のフォームに差し替える (lazy listener)

    キャッシュが温まっていて最初から本物を開いた場合は、準備中のモーダルが存在しないので何もしない
    """
    started = time.time()
    view = build_modal()
    external_id = LOADING_PREFIX + body["trigger_id"]
    # lazy は ack 側の views.open より先に走ることがあるので、準備中のモーダルができるまで少し待つ
    for _ in range(6):
        try:
            client.views_update(external_id=external_id, view=view)
            print(f"fill_modal replaced loading view in {time.time() - started:.2f}s")
            return
        except SlackApiError as e:
            if e.response.get("error") != "not_found":
                raise
            time.sleep(0.5)
    print("fill_modal: no loading view (opened directly with warm cache)")


app.command("/aws-account")(ack=open_modal, lazy=[fill_modal])
app.shortcut("request_aws_account")(ack=open_modal, lazy=[fill_modal])


# ---------- 申請の送信 ----------


def _read_form(view: dict) -> dict:
    v = view["state"]["values"]
    selected = v["options"]["value"].get("selected_options") or []
    return {
        "accountName": (v["account_name"]["value"]["value"] or "").strip(),
        "email": (v["email"]["value"]["value"] or "").strip(),
        "ouId": v["ou"]["value"]["selected_option"]["value"],
        "ouName": v["ou"]["value"]["selected_option"]["text"]["text"],
        "permissionSetArn": v["permission_set"]["value"]["selected_option"]["value"],
        "permissionSetName": v["permission_set"]["value"]["selected_option"]["text"]["text"],
        "loginSource": v["login_source"]["value"]["selected_option"]["value"],
        "formLoginEmail": (v["login_email"]["value"].get("value") or "").strip(),
        "purpose": (v["purpose"]["value"]["value"] or "").strip(),
        "dryRun": any(o["value"] == "dry_run" for o in selected),
    }


def validate_request(ack, view):
    form = _read_form(view)
    errors = {}
    if not ACCOUNT_NAME_RE.fullmatch(form["accountName"]):
        errors["account_name"] = "半角英数字と記号のみ、50文字までで入力してください"
    if not (6 <= len(form["email"]) <= 64 and form["email"].isascii()):
        errors["email"] = "6〜64文字の半角のメールアドレスを入力してください"
    # 送信の ack も3秒以内。ここでは API を呼ばず、キャッシュが温まっているときだけ照合する
    # (冷えているときは post_request で改めて確かめる)
    if _fresh(_ou_cache) and form["ouId"] not in {o["id"] for o in _ou_cache["options"]}:
        errors["ou"] = "この OU は見つかりません。フォームを開き直してください"
    if _fresh(_ps_cache) and form["permissionSetArn"] not in {o["id"] for o in _ps_cache["options"]}:
        errors["permission_set"] = "この権限セットは見つかりません。フォームを開き直してください"
    if form["permissionSetArn"] != NO_PERMISSION_SET and form["loginSource"] == "form" and not form["formLoginEmail"]:
        errors["login_email"] = "「下に入力したメールアドレス」を選んだときは入力してください"
    if errors:
        ack(response_action="errors", errors=errors)
        return
    ack()


def _login_user(form: dict, requester: str, client) -> dict:
    """Identity Center で使うユーザーの情報。Slack のプロフィールから名前 (とメール) を取る

    メールの取得には Slack アプリに users:read と users:read.email のスコープが要る
    """
    if form["permissionSetArn"] == NO_PERMISSION_SET:
        return {"permissionSetArn": "", "loginEmail": ""}
    profile = client.users_info(user=requester)["user"]["profile"]
    email = profile.get("email", "") if form["loginSource"] == "slack" else form["formLoginEmail"]
    display = profile.get("real_name") or profile.get("display_name") or email.split("@")[0]
    ic = identity_center()
    return {
        "loginEmail": email,
        "displayName": display,
        "givenName": profile.get("first_name") or display,
        "familyName": profile.get("last_name") or display,
        "ssoInstanceArn": ic["instanceArn"],
        "identityStoreId": ic["identityStoreId"],
        "portalUrl": ic["portalUrl"],
    }


def post_request(body, view, client):
    form = _read_form(view)
    requester = body["user"]["id"]
    # 送信時の ack では照合を省いたことがあるので、ここで改めて確かめる (時間制限なし)
    if form["ouId"] not in {o["id"] for o in ou_options()} or form["permissionSetArn"] not in {
        o["id"] for o in permission_set_options()
    }:
        client.chat_postMessage(
            channel=requester,
            text=":warning: 選んだ OU か権限セットが見つかりませんでした (削除された可能性があります)。フォームを開き直して申請してください",
        )
        return
    item = {
        **form,
        **_login_user(form, requester, client),
        "requestId": str(uuid.uuid4()),
        "requester": requester,
        "requestedAt": now_iso(),
        "status": "REQUESTED",
    }
    item.pop("formLoginEmail", None)
    if item["permissionSetArn"] and not item["loginEmail"]:
        client.chat_postMessage(
            channel=requester,
            text=":warning: Slack のプロフィールからメールアドレスを取れませんでした。フォームでメールアドレスを入力して申請し直してください",
        )
        return
    # 申請の時点で監査ログに残す (ここではアカウント作成の処理は動かさない)
    table.put_item(Item=item)
    client.chat_postMessage(
        channel=cfg["adminChannelId"],
        text=f"AWS アカウントの払い出し申請: {item['accountName']} (<@{item['requester']}>)",
        blocks=request_blocks(item, with_actions=True),
    )
    client.chat_postMessage(
        channel=item["requester"],
        text=f":inbox_tray: *{item['accountName']}* の申請を受け付けました。情シスの承認をお待ちください。",
    )


app.view("aws_account_request")(ack=validate_request, lazy=[post_request])


# ---------- 承認 / 却下 ----------


def ack_only(ack):
    ack()


def _decide(body, client, decision: str):
    user = body["user"]["id"]
    request_id = body["actions"][0]["value"]
    channel = body["container"]["channel_id"]
    ts = body["container"]["message_ts"]

    # ボタンはチャンネルのメンバーなら誰でも押せるので、押した人をここで確認する
    if user not in cfg["approverUserIds"]:
        client.chat_postEphemeral(channel=channel, user=user, text="承認・却下できるのは情シスの担当者だけです")
        return

    # 申請中 → 承認/却下 の遷移を条件付き更新で1回だけにする (二重押し・再送対策)
    try:
        item = table.update_item(
            Key={"requestId": request_id},
            UpdateExpression="SET #s = :new, approver = :u, decidedAt = :t, channelId = :c, messageTs = :ts",
            ConditionExpression="#s = :requested",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new": decision,
                ":requested": "REQUESTED",
                ":u": user,
                ":t": now_iso(),
                ":c": channel,
                ":ts": ts,
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        client.chat_postEphemeral(channel=channel, user=user, text="この申請はすでに処理済みです")
        return

    if decision == "APPROVED":
        footer = f":white_check_mark: <@{user}> が承認しました ({item['decidedAt']})。作成を開始します"
    else:
        footer = f":x: <@{user}> が却下しました ({item['decidedAt']})"
    client.chat_update(channel=channel, ts=ts, text=footer, blocks=request_blocks(item, footer=footer))

    if decision == "REJECTED":
        client.chat_postMessage(channel=item["requester"], text=f":x: *{item['accountName']}* の申請は却下されました。")
        return

    try:
        # 実行名を申請IDにしておくと、同じ申請で2回目の実行は起動できない
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=request_id,
            input=json.dumps(item, ensure_ascii=False),
        )
    except Exception as e:
        table.update_item(
            Key={"requestId": request_id},
            UpdateExpression="SET #s = :s, failureReason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "FAILED", ":r": f"StartExecution: {e}"},
        )
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: ワークフローを起動できませんでした: {e}")
        raise
    client.chat_postMessage(
        channel=item["requester"],
        text=f":white_check_mark: *{item['accountName']}* の申請が承認されました。作成が終わったらお知らせします。",
    )


def approve(body, client):
    _decide(body, client, "APPROVED")


def reject(body, client):
    _decide(body, client, "REJECTED")


app.action("approve_request")(ack=ack_only, lazy=[approve])
# 通知の「AWS アクセスポータルを開く」ボタン。URL はブラウザで開かれるので、ここは受け取ったと返すだけ
app.action("open_portal")(ack_only)
app.action("reject_request")(ack=ack_only, lazy=[reject])


def handler(event, context):
    return SlackRequestHandler(app=app).handle(event, context)
