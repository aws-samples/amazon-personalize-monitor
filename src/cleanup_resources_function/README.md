# Amazon Personalize Monitor - Cleanup Function

This Lambda function is called as a CloudFormation custom resource when the application is deleted/uninstalled so that resources created dynamically by the application, such as CloudWatch alarms, are also deleted.