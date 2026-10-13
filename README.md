# aws-account-vending-slack

> [!WARNING]
> **実験的な実装です。** 個人の検証用に作ったもので、動作の保証やサポートはありません。仕様は予告なく変わることがあります。
>
> - Organizations の管理アカウントに、アカウント作成と Identity Center の操作ができる強い権限を持つ仕組みをデプロイします。まずは検証用の組織で試してください
> - 承認すると、実際に AWS アカウントが作られます。閉鎖しても完全に閉鎖されるまで (最大90日) は組織のアカウント数の上限に数えられるので、試すときはドライランを使ってください
> - 本番の組織で使う場合は、コードと権限を確認したうえで、自己責任でお使いください

Slack から AWS アカウントの払い出しを申請し、情シスのチャンネルで承認ボタンを押すと、AWS Organizations でアカウントを作成して IAM Identity Center の権限まで割り当てる仕組みです。情シス担当が AWS コンソールにログインせず、普段使っている Slack だけで完結させることを狙っています。

- Slack は無料プランで動きます (Slack API のみ。ワークフロービルダーは使いません)
- Control Tower は使いません。Organizations の API だけで作ります
- 登壇資料: [1人アドミンな私は AWSアカウント申請をSlackで完結したい！](https://speakerdeck.com/ysuzuki/1-hito-ado-minna-watashi-ha-aws-akaunto-shinsei-o-slack-de-kanketsu-shitai)

## 流れ

```
[申請] /aws-account・グローバルショートカット → モーダル
       → 受付 Lambda → DynamoDB に申請を記録 → 情シスch に承認依頼を投稿
[却下] 受付 Lambda → メッセージを却下済みに更新 + DynamoDB に記録 + 申請者に DM
[承認] 受付 Lambda (署名検証・承認者チェック) → メッセージを承認済みに更新 → Step Functions を開始
       Step Functions: CreateAccount → 作成完了までポーリング
                       → MoveAccount (選んだ OU へ。ルート直下なら飛ばす)
                       → Identity Center: 申請者のユーザーを検索 (なければ作成) → 権限セットを割り当て
                       → DynamoDB に結果を記録
                       → 通知 Lambda: 承認メッセージのスレッドに完了返信 + 申請者にログイン案内を DM
```

モーダルの「ドライラン」にチェックを入れると、アカウントを作らずに承認から通知までの流れを確認できます。

## 構成

| 場所 | 中身 |
| --- | --- |
| `cdk/` | CDK (TypeScript)。DynamoDB・Lambda・Step Functions・IAM ロール |
| `cdk/lib/state-machine-definition.ts` | Step Functions の定義 (JSONata) |
| `lambda/receiver.py` | Slack の受付 (Lambda 関数 URL、Bolt for Python)。3秒以内の応答と後続処理を lazy listener で分ける |
| `lambda/notify.py` | Step Functions の最後に結果を Slack に通知する |
| `slack-app-manifest.yaml` | Slack アプリのマニフェスト |

## 前提

- AWS Organizations の**管理アカウント**にデプロイします。CreateAccount は管理アカウントの権限でしか呼べないためです
  - 本番で使うなら、別アカウントに置いた仕組みから最小権限のロールを AssumeRole する形を検討してください
- IAM Identity Center が有効になっていて、割り当てに使う権限セットがあること
  - Identity Center の API はデプロイ先のリージョン (`cdk/bin/app.ts` の `ap-northeast-1`) で呼びます。Identity Center を別のリージョンで有効にしている場合は、デプロイ先をそちらに合わせてください
- デプロイ先のアカウント・リージョンで `cdk bootstrap` が済んでいること
- Node.js、Python 3 (`pip3`)、AWS CLI。Lambda の実行環境は Python 3.14 です。Lambda の依存はローカルの `pip3` で入れるので、Docker は不要です

## セットアップ

### 1. デプロイする

```sh
cd cdk
npm install
npx cdk deploy
```

出力の `SlackRequestUrl` を控えておきます。

### 2. Slack アプリを作る

`slack-app-manifest.yaml` の `REQUEST_URL` を `SlackRequestUrl` に置き換えます。ファイル冒頭のコメントにあるコマンドで、置き換え済みの `slack-app-manifest.generated.yaml` を作れます。

https://api.slack.com/apps の「Create New App」→「From a manifest」に貼り付け、「Create and Install」でワークスペースにインストールします。

### 3. Slack の設定を SSM パラメータストアに入れる

トークンなどはファイルに書かず、SSM パラメータストアの SecureString `/account-vending/slack` に JSON で入れます。CloudFormation は SecureString を作れないので、CLI で作ります。

| キー | 値 |
| --- | --- |
| `botToken` | Bot User OAuth Token (`xoxb-...`) |
| `signingSecret` | アプリの Signing Secret |
| `adminChannelId` | 承認依頼を投稿するチャンネルの ID。ボットをチャンネルに招待しておく |
| `approverUserIds` | 承認ボタンを押せる人の Slack ユーザー ID の配列 |

```sh
aws ssm put-parameter --region ap-northeast-1 --name /account-vending/slack --type SecureString \
  --value '{"botToken":"xoxb-...","signingSecret":"...","adminChannelId":"C...","approverUserIds":["U..."]}'
```

値を入れ直したら、Lambda の実行環境が入れ替わったあとに読み直されます。

### 4. Identity Center で「Eメール送信 OTP」を有効にする

この仕組みは、申請者が Identity Center にまだいなければ API (CreateUser) でユーザーを作ります。API で作ったユーザーにはパスワードがありません。

管理アカウントのコンソールで「IAM Identity Center → 設定 → 認証 → 標準認証」の設定を開き、「Eメール送信 OTP」(Send email OTP) を有効にしておくと、初回サインインのときに確認メールが届き、本人がパスワードを設定できます。

## 使い方

1. Slack で `/aws-account` を実行する (ボットとの DM でも使えます)。グローバルショートカット「AWSアカウントを申請」からも開けます
2. アカウント名・ルートユーザーのメール・OU・権限セットを入力して送信する
3. 情シスch に承認依頼が届くので、承認者が「承認」か「却下」を押す
4. 作成が終わると、申請者にアクセスポータルの URL とログインするユーザー名が DM で届く

## 注意

- 組織のアカウント数の上限は初期値で10です (新しい組織ではもっと少ないこともあります)。閉鎖したアカウントも、完全に閉鎖されるまで (最大90日) は上限に数えられます。試すときはドライランを使ってください
- 作成したアカウントは、作成から4日経つまで組織から外せません
- アカウント名は ASCII のみ (50文字まで)、ルートユーザーのメールは全 AWS アカウントで一意である必要があります
- DynamoDB のテーブルと、Lambda・Step Functions のロググループ (3つ) は `cdk destroy` しても残ります。不要なら手で消してください
- SSM パラメータ `/account-vending/slack` は CDK の管理外なので、`aws ssm delete-parameter` で消してください

## ライセンス

[MIT](LICENSE)
