import { execSync } from 'node:child_process';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import { Construct } from 'constructs';
import { definition } from './state-machine-definition';

export type AccountVendingStackProps = cdk.StackProps;

const LAMBDA_DIR = path.join(__dirname, '..', '..', 'lambda');
const RECEIVER_FUNCTION_NAME = 'aws-account-vending-receiver';
const SLACK_PARAM_NAME = '/account-vending/slack';

export class AccountVendingStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: AccountVendingStackProps) {
    super(scope, id, props);

    // Slack のボットトークンなど (botToken / signingSecret / adminChannelId / approverUserIds の JSON)。
    // ローテーションしないので Secrets Manager ではなく SSM の SecureString (標準パラメータ・aws/ssm キーなら無料) に置く。
    // CloudFormation は SecureString を作れないので、パラメータは CLI で作り、ここでは名前で参照するだけ
    const slackParam = ssm.StringParameter.fromSecureStringParameterAttributes(this, 'SlackParam', {
      parameterName: SLACK_PARAM_NAME,
    });

    // 申請〜作成までの監査ログ。Slack 無料プランの履歴は消えるので、記録の正はこちら
    const table = new dynamodb.TableV2(this, 'RequestTable', {
      tableName: 'aws-account-vending-requests',
      partitionKey: { name: 'requestId', type: dynamodb.AttributeType.STRING },
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // slack_bolt は純 Python なので、Docker を使わずローカルの pip で依存を入れる
    const code = lambda.Code.fromAsset(LAMBDA_DIR, {
      exclude: ['__pycache__', '*.pyc', '.venv'],
      bundling: {
        image: lambda.Runtime.PYTHON_3_14.bundlingImage,
        command: [
          'bash', '-c',
          'pip install -r requirements.txt -t /asset-output && cp *.py /asset-output/',
        ],
        local: {
          tryBundle(outputDir: string) {
            execSync(`pip3 install -q -r requirements.txt -t "${outputDir}" && cp *.py "${outputDir}/"`, {
              cwd: LAMBDA_DIR,
              stdio: 'inherit',
            });
            return true;
          },
        },
      },
    });

    const commonEnv = {
      SLACK_PARAM_NAME: SLACK_PARAM_NAME,
    };

    const notifyFn = new lambda.Function(this, 'NotifyFunction', {
      functionName: 'aws-account-vending-notify',
      runtime: lambda.Runtime.PYTHON_3_14,
      architecture: lambda.Architecture.ARM_64,
      handler: 'notify.handler',
      code,
      timeout: cdk.Duration.seconds(30),
      environment: commonEnv,
      logGroup: new logs.LogGroup(this, 'NotifyLogs', { retention: logs.RetentionDays.ONE_MONTH }),
    });
    slackParam.grantRead(notifyFn);

    // ---------- Step Functions ----------
    const smRole = new iam.Role(this, 'StateMachineRole', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
    });
    smRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'organizations:ListRoots',
          'organizations:CreateAccount',
          'organizations:DescribeCreateAccountStatus',
          'organizations:MoveAccount',
          'organizations:TagResource',
        ],
        resources: ['*'],
      }),
    );
    // IAM Identity Center: 申請者のユーザーを探す/作る、新しいアカウントに権限セットを割り当てる
    smRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'identitystore:GetUserId',
          'identitystore:DescribeUser',
          'identitystore:CreateUser',
          'sso:CreateAccountAssignment',
          'sso:DescribeAccountAssignmentCreationStatus',
          'organizations:DescribeAccount',
        ],
        resources: ['*'],
      }),
    );
    table.grant(smRole, 'dynamodb:UpdateItem');
    notifyFn.grantInvoke(smRole);

    const stateMachine = new sfn.StateMachine(this, 'AccountVendingStateMachine', {
      stateMachineName: 'aws-account-vending',
      definitionBody: sfn.DefinitionBody.fromString(JSON.stringify(definition, null, 2)),
      definitionSubstitutions: {
        TableName: table.tableName,
        NotifyFunctionArn: notifyFn.functionArn,
      },
      role: smRole,
      timeout: cdk.Duration.hours(1),
      logs: {
        destination: new logs.LogGroup(this, 'StateMachineLogs', { retention: logs.RetentionDays.ONE_YEAR }),
        level: sfn.LogLevel.ALL,
        includeExecutionData: true,
      },
    });

    // ---------- Slack の受付 ----------
    const receiverFn = new lambda.Function(this, 'ReceiverFunction', {
      functionName: RECEIVER_FUNCTION_NAME,
      runtime: lambda.Runtime.PYTHON_3_14,
      architecture: lambda.Architecture.ARM_64,
      handler: 'receiver.handler',
      code,
      memorySize: 512,
      timeout: cdk.Duration.seconds(30),
      environment: {
        ...commonEnv,
        TABLE_NAME: table.tableName,
        STATE_MACHINE_ARN: stateMachine.stateMachineArn,
      },
      logGroup: new logs.LogGroup(this, 'ReceiverLogs', { retention: logs.RetentionDays.ONE_MONTH }),
    });
    slackParam.grantRead(receiverFn);
    table.grantReadWriteData(receiverFn);
    stateMachine.grantStartExecution(receiverFn);
    // モーダルを開くたびに OU の一覧を取る (固定の設定だと OU の追加・削除が反映されない)
    receiverFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'organizations:ListRoots',
          'organizations:ListOrganizationalUnitsForParent',
          // フォームの権限セットの選択肢
          'sso:ListInstances',
          'sso:ListPermissionSets',
          'sso:DescribePermissionSet',
        ],
        resources: ['*'],
      }),
    );
    // lazy listener は自分自身を非同期で呼び直す。functionArn を使うと循環参照になるので名前から組み立てる
    receiverFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['lambda:InvokeFunction'],
        resources: [
          cdk.Stack.of(this).formatArn({
            service: 'lambda',
            resource: 'function',
            resourceName: RECEIVER_FUNCTION_NAME,
            arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
          }),
        ],
      }),
    );

    // 認証は Slack の署名検証 (Bolt) で行うので、関数 URL 自体は NONE
    const receiverUrl = receiverFn.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });

    new cdk.CfnOutput(this, 'SlackRequestUrl', { value: receiverUrl.url });
    new cdk.CfnOutput(this, 'SlackParameterName', { value: SLACK_PARAM_NAME });
    new cdk.CfnOutput(this, 'StateMachineArn', { value: stateMachine.stateMachineArn });
    new cdk.CfnOutput(this, 'RequestTableName', { value: table.tableName });
  }
}
