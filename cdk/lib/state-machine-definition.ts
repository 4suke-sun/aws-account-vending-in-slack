/**
 * アカウント作成ワークフローの ASL (JSONata)
 *
 * 入力は受付 Lambda が DynamoDB から読んだ申請レコード。
 * ${TableName} / ${NotifyFunctionArn} は definitionSubstitutions で置き換える。
 */

const updateStatus = (status: string, next: string, extra: Record<string, string> = {}) => {
  const sets = ['#s = :s', 'updatedAt = :t', ...Object.keys(extra).map((k) => `${k} = :${k}`)];
  const values: Record<string, unknown> = {
    ':s': { S: status },
    // 受付 Lambda が書く requestedAt / decidedAt と同じ日本時間の ISO 形式にそろえる ($now() だけだと UTC になる)
    ':t': { S: "{% $now('[Y0001]-[M01]-[D01]T[H01]:[m01]:[s01][Z01:01]', '+0900') %}" },
  };
  for (const [k, v] of Object.entries(extra)) values[`:${k}`] = { S: v };
  return {
    Type: 'Task',
    Resource: 'arn:aws:states:::dynamodb:updateItem',
    Arguments: {
      TableName: '${TableName}',
      Key: { requestId: { S: '{% $request.requestId %}' } },
      UpdateExpression: `SET ${sets.join(', ')}`,
      ExpressionAttributeNames: { '#s': 'status' },
      ExpressionAttributeValues: values,
    },
    Next: next,
  };
};

const notify = (payload: Record<string, string>, next?: string) => ({
  Type: 'Task',
  Resource: 'arn:aws:states:::lambda:invoke',
  Arguments: {
    FunctionName: '${NotifyFunctionArn}',
    Payload: { request: '{% $request %}', ...payload },
  },
  Retry: [{ ErrorEquals: ['Lambda.TooManyRequestsException', 'Lambda.ServiceException'], MaxAttempts: 3 }],
  ...(next ? { Next: next } : { End: true }),
});

export const definition = {
  Comment: 'Slack で承認された申請から AWS アカウントを作成する',
  QueryLanguage: 'JSONata',
  StartAt: 'ListRoots',
  States: {
    // 最初に Organizations を呼べるか確かめる。MoveAccount の移動元 (ルート) の ID もここで取る
    ListRoots: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:organizations:listRoots',
      Arguments: {},
      Assign: {
        request: '{% $states.input %}',
        rootId: '{% $states.result.Roots[0].Id %}',
        userCreated: false,
        dryRunUser: 'SKIPPED',
        loginUserName: '',
        userId: '',
        createStatus: {},
      },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'IsDryRun',
    },
    IsDryRun: {
      Type: 'Choice',
      Choices: [{ Condition: '{% $request.dryRun = true %}', Next: 'DryRunNeedsUser' }],
      Default: 'MarkCreating',
    },
    // ドライランでも Identity Center のユーザー検索 (読み取りのみ) までは確かめる
    DryRunNeedsUser: {
      Type: 'Choice',
      Choices: [{ Condition: "{% $not($exists($request.permissionSetArn)) or $request.permissionSetArn = '' %}", Next: 'MarkDryRun' }],
      Default: 'DryRunFindUser',
    },
    DryRunFindUser: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:identitystore:getUserId',
      Arguments: {
        IdentityStoreId: '{% $request.identityStoreId %}',
        AlternateIdentifier: { UniqueAttribute: { AttributePath: 'emails.value', AttributeValue: '{% $request.loginEmail %}' } },
      },
      Assign: { dryRunUser: 'EXISTS', userId: '{% $states.result.UserId %}' },
      Catch: [
        {
          ErrorEquals: ['Identitystore.ResourceNotFoundException'],
          Assign: { dryRunUser: 'WILL_CREATE', loginUserName: '{% $request.loginEmail %}' },
          Next: 'MarkDryRun',
        },
        { ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' },
      ],
      Next: 'DryRunDescribeUser',
    },
    DryRunDescribeUser: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:identitystore:describeUser',
      Arguments: { IdentityStoreId: '{% $request.identityStoreId %}', UserId: '{% $userId %}' },
      Assign: { loginUserName: '{% $states.result.UserName %}' },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'MarkDryRun',
    },
    MarkDryRun: updateStatus('DRY_RUN_SUCCEEDED', 'NotifyDryRun'),
    NotifyDryRun: notify({ result: 'DRY_RUN', rootId: '{% $rootId %}', dryRunUser: '{% $dryRunUser %}', loginUserName: '{% $loginUserName %}' }),

    MarkCreating: updateStatus('CREATING', 'CreateAccount'),
    CreateAccount: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:organizations:createAccount',
      Arguments: {
        AccountName: '{% $request.accountName %}',
        Email: '{% $request.email %}',
        Tags: [
          { Key: 'RequestId', Value: '{% $request.requestId %}' },
          { Key: 'Requester', Value: '{% $request.requester %}' },
          { Key: 'Approver', Value: '{% $request.approver %}' },
        ],
      },
      Assign: { createRequestId: '{% $states.result.CreateAccountStatus.Id %}' },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'WaitForCreation',
    },
    WaitForCreation: { Type: 'Wait', Seconds: 20, Next: 'DescribeCreateAccountStatus' },
    DescribeCreateAccountStatus: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:organizations:describeCreateAccountStatus',
      Arguments: { CreateAccountRequestId: '{% $createRequestId %}' },
      Assign: { createStatus: '{% $states.result.CreateAccountStatus %}' },
      Retry: [{ ErrorEquals: ['Organizations.TooManyRequestsException'], IntervalSeconds: 5, MaxAttempts: 5, BackoffRate: 2 }],
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'CheckCreationState',
    },
    CheckCreationState: {
      Type: 'Choice',
      Choices: [
        { Condition: "{% $createStatus.State = 'SUCCEEDED' %}", Next: 'NeedsMove' },
        {
          Condition: "{% $createStatus.State = 'FAILED' %}",
          Assign: { error: { Error: 'CreateAccountFailed', Cause: '{% $createStatus.FailureReason %}' } },
          Next: 'MarkFailed',
        },
      ],
      Default: 'WaitForCreation',
    },
    // 作成直後のアカウントはルート直下にいる。「ルート直下 (OU なし)」を選んだ申請は移動しない
    NeedsMove: {
      Type: 'Choice',
      Choices: [{ Condition: '{% $request.ouId = $rootId %}', Next: 'NeedsAssignment' }],
      Default: 'MoveAccount',
    },
    MoveAccount: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:organizations:moveAccount',
      Arguments: {
        AccountId: '{% $createStatus.AccountId %}',
        SourceParentId: '{% $rootId %}',
        DestinationParentId: '{% $request.ouId %}',
      },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'NeedsAssignment',
    },

    // ---------- IAM Identity Center: 申請者がログインできるようにする ----------
    // 割り当ては管理アカウント (または委任管理者) からしかできない。この仕組みは管理アカウントで動くのでそのまま呼べる
    NeedsAssignment: {
      Type: 'Choice',
      Choices: [{ Condition: "{% $not($exists($request.permissionSetArn)) or $request.permissionSetArn = '' %}", Next: 'MarkSucceeded' }],
      Default: 'FindUser',
    },
    FindUser: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:identitystore:getUserId',
      Arguments: {
        IdentityStoreId: '{% $request.identityStoreId %}',
        AlternateIdentifier: { UniqueAttribute: { AttributePath: 'emails.value', AttributeValue: '{% $request.loginEmail %}' } },
      },
      Assign: { userId: '{% $states.result.UserId %}' },
      Catch: [
        // 例外名の接頭辞は Identitystore (s は小文字)。TestState で確認した
        { ErrorEquals: ['Identitystore.ResourceNotFoundException'], Next: 'CreateUser' },
        { ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' },
      ],
      Next: 'DescribeUser',
    },
    // サインイン画面で入力するのはメールではなくユーザー名。既存ユーザーはメールと違うことがあるので取っておく
    DescribeUser: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:identitystore:describeUser',
      Arguments: { IdentityStoreId: '{% $request.identityStoreId %}', UserId: '{% $userId %}' },
      Assign: { loginUserName: '{% $states.result.UserName %}' },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'CreateAccountAssignment',
    },
    // API で作ったユーザーはパスワードがない。Identity Center の設定で「Send email OTP」を有効にしておくと、
    // 初回サインイン時に確認メールが届き、本人がパスワードを設定できる
    CreateUser: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:identitystore:createUser',
      Arguments: {
        IdentityStoreId: '{% $request.identityStoreId %}',
        UserName: '{% $request.loginEmail %}',
        DisplayName: '{% $request.displayName %}',
        Name: { GivenName: '{% $request.givenName %}', FamilyName: '{% $request.familyName %}' },
        Emails: [{ Value: '{% $request.loginEmail %}', Type: 'work', Primary: true }],
      },
      Assign: { userId: '{% $states.result.UserId %}', userCreated: true, loginUserName: '{% $request.loginEmail %}' },
      Catch: [
        // メールでは見つからないのに、ユーザー名 (= メール) が別のユーザーに使われている。TestState で例外名を確認した
        {
          ErrorEquals: ['Identitystore.ConflictException'],
          Assign: {
            error: {
              Error: 'UserNameConflict',
              Cause: "{% 'Identity Center にユーザー名 ' & $request.loginEmail & ' のユーザーが既にいますが、メールアドレスが一致しません' %}",
            },
          },
          Next: 'MarkFailed',
        },
        { ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' },
      ],
      Next: 'CreateAccountAssignment',
    },
    CreateAccountAssignment: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:ssoadmin:createAccountAssignment',
      Arguments: {
        InstanceArn: '{% $request.ssoInstanceArn %}',
        TargetId: '{% $createStatus.AccountId %}',
        TargetType: 'AWS_ACCOUNT',
        PermissionSetArn: '{% $request.permissionSetArn %}',
        PrincipalType: 'USER',
        PrincipalId: '{% $userId %}',
      },
      Assign: { assignmentRequestId: '{% $states.result.AccountAssignmentCreationStatus.RequestId %}' },
      // 作成直後のアカウントはまだ準備中のことがあるので、競合・スロットリングは待って再試行する
      Retry: [
        {
          ErrorEquals: ['SsoAdmin.ConflictException', 'SsoAdmin.ThrottlingException'],
          IntervalSeconds: 10,
          MaxAttempts: 6,
          BackoffRate: 2,
        },
      ],
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'WaitForAssignment',
    },
    WaitForAssignment: { Type: 'Wait', Seconds: 5, Next: 'DescribeAccountAssignmentCreationStatus' },
    DescribeAccountAssignmentCreationStatus: {
      Type: 'Task',
      Resource: 'arn:aws:states:::aws-sdk:ssoadmin:describeAccountAssignmentCreationStatus',
      Arguments: {
        InstanceArn: '{% $request.ssoInstanceArn %}',
        AccountAssignmentCreationRequestId: '{% $assignmentRequestId %}',
      },
      Assign: { assignmentStatus: '{% $states.result.AccountAssignmentCreationStatus %}' },
      Catch: [{ ErrorEquals: ['States.ALL'], Assign: { error: '{% $states.errorOutput %}' }, Next: 'MarkFailed' }],
      Next: 'CheckAssignmentStatus',
    },
    CheckAssignmentStatus: {
      Type: 'Choice',
      Choices: [
        { Condition: "{% $assignmentStatus.Status = 'SUCCEEDED' %}", Next: 'MarkSucceeded' },
        {
          Condition: "{% $assignmentStatus.Status = 'FAILED' %}",
          Assign: { error: { Error: 'AccountAssignmentFailed', Cause: '{% $assignmentStatus.FailureReason %}' } },
          Next: 'MarkFailed',
        },
      ],
      Default: 'WaitForAssignment',
    },
    MarkSucceeded: updateStatus('SUCCEEDED', 'NotifySucceeded', { accountId: '{% $createStatus.AccountId %}' }),
    NotifySucceeded: notify({
      result: 'SUCCEEDED',
      accountId: '{% $createStatus.AccountId %}',
      userCreated: '{% $userCreated %}',
      loginUserName: '{% $loginUserName %}',
    }),

    MarkFailed: updateStatus('FAILED', 'NotifyFailed', { failureReason: '{% $string($error) %}' }),
    NotifyFailed: notify(
      { result: 'FAILED', error: '{% $error %}', accountId: "{% $exists($createStatus.AccountId) ? $createStatus.AccountId : '' %}" },
      'Failed',
    ),
    Failed: { Type: 'Fail', Error: 'AccountVendingFailed' },
  },
};
