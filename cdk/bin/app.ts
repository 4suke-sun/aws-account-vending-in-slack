#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { AccountVendingStack } from '../lib/account-vending-stack';

const app = new cdk.App();

// CreateAccount は管理アカウントの権限でしか呼べないので、管理アカウントにデプロイする
new AccountVendingStack(app, 'AwsAccountVendingSlack', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'ap-northeast-1' },
  description: 'Slack から AWS アカウントを払い出す仕組み (1人アドミン検証シリーズ)',
});
