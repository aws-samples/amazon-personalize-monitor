# Amazon Personalize Monitor

<!-- vscode-markdown-toc -->
* [Why is this important?](#Whyisthisimportant)
* [Features](#Features)
	* [CloudWatch dashboard](#CloudWatchdashboard)
	* [CloudWatch alarms](#CloudWatchalarms)
	* [CloudWatch metrics](#CloudWatchmetrics)
	* [Cost optimization (optional)](#Costoptimizationoptional)
		* [Idle campaigns](#Idlecampaigns)
		* [Over-provisioned campaigns](#Over-provisionedcampaigns)
* [Architecture](#Architecture)
* [Installing the application](#Installingtheapplication)
	* [Option 1 - Install from Serverless Application Repository](#Option1-InstallfromServerlessApplicationRepository)
	* [Option 2 - Install using Serverless Application Model](#Option2-InstallusingServerlessApplicationModel)
	* [Application settings/parameters](#Applicationsettingsparameters)
* [Uninstalling the application](#Uninstallingtheapplication)
* [FAQs](#FAQs)
* [Reporting issues](#Reportingissues)
* [License summary](#Licensesummary)

<!-- vscode-markdown-toc-config
	numbering=false
	autoSave=true
	/vscode-markdown-toc-config -->
<!-- /vscode-markdown-toc -->

This project contains the source code and supporting files for deploying a serverless application that adds monitoring, alerting, and optimzation capabilities for [Amazon Personalize](https://aws.amazon.com/personalize/), an AI service from AWS that allows you to create custom ML recommenders based on your data. Highlights include:

- Generation of additional [CloudWatch metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/working_with_metrics.html) to track the average RPS and `minRecommendationRequestsPerSecond` for [recommenders](https://docs.aws.amazon.com/personalize/latest/dg/creating-recommenders.html), average TPS and `minProvisionedTPS` for [campaigns](https://docs.aws.amazon.com/personalize/latest/dg/campaigns.html), and utilization of recommenders and campaigns over time.
- [CloudWatch alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html) to alert you via SNS/email when recommender or campaign utilization drops below a configurable threshold or has been idle for a configurable length of time (optional).
- [CloudWatch dashboard](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Dashboards.html) populated with graph widgets for average (actual) vs provisioned TPS/RPS, recommender and campaign utilization, recommender and campaign latency, and the number of recommenders and campaigns being monitored.
- Capable of monitoring campaigns and recommenders across multiple regions in the same AWS account.
- Automatically [stop recommenders](https://docs.aws.amazon.com/personalize/latest/dg/stopping-starting-recommender.html) and delete campaigns that have been idle more than a configurable number of hours (optional).
- Automatically reduce the `minRecommendationRequestsPerSecond` for over-provisioned recommenders and `minProvisionedTPS` for over-provisioned campaigns to optimize cost (optional).

## <a name='Whyisthisimportant'></a>Why is this important?

Before you can retrieve real-time recommendations from Amazon Personalize, you must create a [recommender](https://docs.aws.amazon.com/personalize/latest/dg/creating-recommenders.html) or [campaign](https://docs.aws.amazon.com/personalize/latest/dg/campaigns.html). Often times multiple recommenders and/or campaigns are needed to provide recommendations targeting different use cases for an pplication such as user-personalization, related items, and personalized ranking. Recommenders and campaigns represent resources that are auto-scaled by Personalize to meet the demand from requests from your application. This is typically how Personalize is integrated into your applications. When an application needs to display personalized recommendations to a user, a [GetRecommendations](https://docs.aws.amazon.com/personalize/latest/dg/getting-real-time-recommendations.html#recommendations) or [GetPersonalizedRanking](https://docs.aws.amazon.com/personalize/latest/dg/getting-real-time-recommendations.html#rankings) API call is made to a recommender or campaign to retrieve recommendations. Just like monitoring your own application components is important, monitoring your Personalize recommenders and campaigns is also important and considered a best practice. This application is designed to help you do just that.

When you provision a recommender using the [CreateRecommender](https://docs.aws.amazon.com/personalize/latest/dg/API_CreateRecommender.html) API or a campaign using the [CreateCampaign](https://docs.aws.amazon.com/personalize/latest/dg/API_CreateCampaign.html) API, you can optionally specify a value for `minRecommendationRequestsPerSecond` and `minProvisionedTPS`, respectively. This value specifies the requested _minimum_ requests/transactions (calls) per second that Amazon Personalize will support for that recommender or campaign. As your actual request volume to a recommender or campaign approaches its `minRecommendationRequestsPerSecond` or `minProvisionedTPS`, Personalize will automatically provision additional resources to support your request volume. Then when request volume drops, Personalize will automatically scale back down **no lower** than `minRecommendationRequestsPerSecond` or `minProvisionedTPS`. **Since you are billed based on the higher of actual TPS and `minRecommendationRequestsPerSecond`/`minProvisionedTPS`, it is therefore important to not over-provision your recommenders or campaigns to optimize cost.** This also means that leaving a recommender or campaign idle (active but no longer in-use) will result in unnecessary charges. This application gives you the tools to visualize your recommender and campaign utilization, to be notified when there is an opportunity to tune your recommender or campaign provisioning, and even take action to reduce and eliminate over-provisioning.

> General best practice is to set `minRecommendationRequestsPerSecond` and `minProvisionedTPS` to `1`, or your low watermark for recommendations requests, and let Personalize auto-scale recommender or campaign resources to meet actual demand.

See the Amazon Personalize [pricing page](https://aws.amazon.com/personalize/pricing/) for full details on costs.

## <a name='Features'></a>Features

### <a name='CloudWatchdashboard'></a>CloudWatch dashboard

When you deploy this project, a [CloudWatch dashboard](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Dashboards.html) is built with widgets for Actual vs Provisioned TPS/RPS, recommender/campaign utilization, and recommender/campaign latency for the recommenders and campaigns you wish to monitor. The dashboard gives you critical visual information to assess how your recommenders and campaigns are performing and being utilized. The data in these graphs can help you properly tune your recommender's `minRecommendationRequestsPerSecond` and campaign's `minProvisionedTPS`.

![Personalize Monitor CloudWatch Dashboard](./images/personalize-monitor-cloudwatch-dashboard.png)

For more details on the CloudWatch dashboard created and maintained by this application, see the [dashboard_mgmt](./src/dashboard_mgmt_function/) function page.

### <a name='CloudWatchalarms'></a>CloudWatch alarms

At deployment time, you can optionally have this application automatically create [CloudWatch alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html) that will alert you when a monitored recommender's or campaign's utilization drops below a threshold you define for nine out of twelve evaluation periods. Since the intervals are 5 minutes, that means that nine of the 5 minute evaluations over a 1 hour span must be below the threshold to enter an alarm status. The same rule applies to transition from alarm to OK status. Similarly, the idle recommender/campaign alarm will alert you when there has been no request activity for a recommender/campaign for a configurable amount of time. The alarms will be setup to alert you via email through an SNS topic in each region where resources are monitored. Once the alarms are setup, you can alternatively link them to any operations and messaging tools you already use (i.e. Slack, PagerDuty, etc).

![Personalize Monitor CloudWatch Alarms](./images/personalize-monitor-cloudwatch-alarms.png)

For more details on the CloudWatch alarms created by this application, see the [personalize_monitor](./src/personalize_monitor_function/) function page.

### <a name='CloudWatchmetrics'></a>CloudWatch metrics

To support the CloudWatch dashboard and alarms described above, a few new custom [CloudWatch metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/working_with_metrics.html) are added for the monitored recommenders and campaigns. These metrics are populated by the [personalize_monitor](./src/personalize_monitor_function/) Lambda function that is setup to run every 5 minutes in your account. You can find these metrics in CloudWatch under Metrics in the "PersonalizeMonitor" namespace.

![Personalize Monitor CloudWatch Metrics](./images/personalize-monitor-cloudwatch-metrics.png)

For more details on the custom metrics created by this application, see the [personalize_monitor](./src/personalize_monitor_function/) function page.

### <a name='Costoptimizationoptional'></a>Cost optimization (optional)

This application can be optionally configured to automatically perform cost optimization actions for your Amazon Personalize recommenders and campaigns.

#### <a name='Idlecampaigns'></a>Idle recommenders/campaigns
Idle recommenders/campaigns are those that have been provisioned but are not receiving any `GetRecommendations`/`GetPersonalizedRanking` calls. Since costs are incurred while a recommender/campaign is active regardless of whether it receives any requests, detecting and eliminating these idle recommenders/campaigns can be an important cost optimization activity. This can be particularly useful in non-production AWS accounts such as development and testing where you are more likely to have abandoned recommenders/campaigns.

Note that this is where an important difference between recommenders and campaigns comes into play. Recommenders can be started and stopped to provision and de-provision the resources needed for real-time inference. When a recommender is stopped, the real-time inference resources are deleted (which pauses ongoing recommender charges) but the underlying model artifacts are preserved. This allows you to later start the recommender without having to train the model again. Campaigns, on the other hand, represent only the resources needed for real-time inference for a solution version. Therefore, you must delete a campaign to release the real-time resources and pause campaign charges. Since the solution version is not being deleted, model artifacts are preserved similar to when a recommender is stopped.

See the `AutoDeleteOrStopIdleResources` and `IdleThresholdHours` deployment parameters in the installation instructions below and the [personalize_monitor](./src/personalize_monitor_function#automatically-deleting-idle-campaigns-optional) function for details.

#### <a name='Over-provisionedcampaigns'></a>Over-provisioned recommenders/campaigns

Properly provisioning recommenders and campaigns, as described earlier, is also an important cost optimization activity. This application can be configured to automatically reduce a recommender's `minRecommendationRequestsPerSecond` or a campaign's `minProvisionedTPS` based on actual request volume. This will optimize recommender/campaign utilization when request volume is lower while relying on Personalize to auto-scale based on actual activity. See the `AutoAdjustMinTPS` deployment parameter below and the [personalize_monitor](./src/personalize_monitor_function#automatically-adjusting-campaign-minprovisionedtps-optional) function for details.

## <a name='Architecture'></a>Architecture

The following diagram depicts how the Lambda functions in this application work together using an event-driven approach built on [Amazon EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/what-is-amazon-eventbridge.html). The [personalize_monitor](./src/personalize_monitor_function/) function is invoked every five minutes to generate CloudWatch metric data based on the monitored recommenders/campaigns and create alarms (if configured). It also generates events which are published to EventBridge that trigger activities such as optimizing `minRecommendationRequestsPerSecond`/`minProvisionedTPS`, stopping idle recommenders, deleting idle campaigns, updating the Personalize Monitor CloudWatch dashboard, and sending notifications. This approach allows you to more easily integrate these functions into your own operations by sending your own events, say, to trigger the dashboard to be rebuilt after you create a campaign or register your own targets to events generated by this application.

![Personalize Monitor Architecture](./images/personalize-monitor-architecture.png)

See the readme pages for each function for details on the events that they produce and consume.

## <a name='Installingtheapplication'></a>Installing the application

***IMPORTANT NOTE:** Deploying this application in your AWS account will create and consume AWS resources, which will cost money. For example, the CloudWatch dashboard, the Lambda function that collects additional monitoring metrics is run every 5 minutes, CloudWatch alarms, logging, and so on. Therefore, if after installing this application you choose not to use it as part of your monitoring strategy, be sure to follow the Uninstall instructions below to clean up all resources and avoid ongoing charges.*

### <a name='Option1-InstallfromServerlessApplicationRepository'></a>Option 1 - Install from Serverless Application Repository

The easiest way to deploy this application is from the [Serverless Application Repository](https://aws.amazon.com/serverless/serverlessrepo/) (SAR).

1. Within the AWS account where you wish to deploy the application, browse to the [application's page](https://serverlessrepo.aws.amazon.com/applications/arn:aws:serverlessrepo:us-east-1:316031960777:applications~Amazon-Personalize-Monitor) in the Serverless Application Repository and click **"Deploy"**.
2. Enter/update values in the **"Application settings"** panel (described below) and click **"Deploy"** again.

### <a name='Option2-InstallusingServerlessApplicationModel'></a>Option 2 - Install using Serverless Application Model

If you'd rather install the application manually, you can use the AWS [Serverless Application Model](https://aws.amazon.com/serverless/sam/) (SAM) CLI to build and  deploy the application into your AWS account.

To use the SAM CLI, you need the following tools.

* SAM CLI - [Install the SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
* [Python 3 installed](https://www.python.org/downloads/)
* Docker - [Install Docker community edition](https://hub.docker.com/search/?type=edition&offering=community)

Then ensure you are logged in to `public.ecr.aws` in Docker so SAM can download the Docker build images by running the following command in your shell.

```bash
aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws
```

To build and deploy the application for the first time, run the following in your shell:

```bash
sam build --use-container --cached
sam deploy --guided
```

The first command will build the source of the application. The second command will package and deploy the application to your AWS account with a series of prompts. The following section describes the supported application parameters.

### <a name='Applicationsettingsparameters'></a>Application settings/parameters

Whether you install this application from SAR or SAM, the following parameters can be used to control how the application monitors your Personalize deployments.

| Prompt/Parameter | Description | Default |
| --- | --- | --- |
| Stack Name | The name of the stack to deploy to CloudFormation. This should be unique to your account and region. | `personalize-monitor` |
| AWS Region | The AWS region you want to deploy this application to. Note that the CloudWatch metrics Lambda function in this application will still be able to monitor campaigns across multiple regions; you will be prompted for the region(s) to monitor below. | Your current region |
| Parameter CampaignARNs | Comma separated list of Personalize campaign ARNs to monitor or `all` to monitor all active campaigns. It is recommended to use `all` so that any new campaigns that are added after deployment will be automatically detected, monitored, and have alarms created (optional) | `all` |
| Parameter RecommenderARNs | Comma separated list of Personalize recommender ARNs to monitor or `all` to monitor all active recommenders. It is recommended to use `all` so that any new recommenders that are added after deployment will be automatically detected, monitored, and have alarms created (optional) | `all` |
| Parameter Regions | Comma separated list of AWS regions to monitor recommenders/campaigns. Only applicable when `all` is used for `CampaignARNs` or `RecommenderARNs`. Leaving this value blank will default to the region where this application is deployed (i.e. `AWS Region` parameter above). | |
| Parameter AutoCreateUtilizationAlarms | Whether to automatically create a utilization CloudWatch alarm for each monitored recommender or campaign. | `Yes` |
| Parameter UtilizationThresholdAlarmLowerBound | Minimum threshold value (in percent) to enter alarm state for recommender/campaign utilization. This value is only relevant if `AutoCreateAlarms` is `Yes`. | `100` |
| Parameter AutoAdjustMinTPS | Whether to automatically compare recommender/campaign request activity against the configured `minRecommendationRequestsPerSecond`/`minProvisionedTPS` to determine if `minRecommendationRequestsPerSecond`/`minProvisionedTPS` can be reduced to optimize utilization. | `Yes` |
| Parameter AutoCreateIdleAlarms | Whether to automatically create a idle detection CloudWatch alarm for each monitored recommender/campaign. | `Yes` |
| Parameter IdleThresholdHours | Number of hours that a recommender/campaign must be idle (i.e. no requests) before it is automatically stopped (recommender) or deleted (campaign). `AutoDeleteOrStopIdleResources` must be `Yes` for idle recommender stop or campaign deletion to occur. | `24` |
| Parameter AutoDeleteOrStopIdleResources | Whether to automatically stop idle recommenders or delete idle campaigns. An idle recommender/campaign is one that has not had any requests in `IdleThresholdHours` hours. | `No` |
| Parameter NotificationEndpoint | Email address to receive alarm and ok notifications, recommender stop/update, campaign delete/update events (optional). An [SNS](https://aws.amazon.com/sns/) topic is created in each region where resources are monitored and this email address will be added as a subscriber to the topic(s). You will receive a confirmation email for the SNS topic subscription in each region so be sure to click the confirmation link in that email to ensure you receive notifications. | |
| Confirm changes before deploy | If set to yes, any CloudFormation change sets will be shown to you before execution for manual review. If set to no, the AWS SAM CLI will automatically deploy application changes. | |
| Allow SAM CLI IAM role creation | Since this application creates IAM roles to allow the Lambda functions to access AWS services, this setting must be `Yes`. | |
| Save arguments to samconfig.toml | If set to yes, your choices will be saved to a configuration file inside the application, so that in the future you can just re-run `sam deploy` without parameters to deploy changes to your application. | |

## <a name='Uninstallingtheapplication'></a>Uninstalling the application

If you installed the application from the Serverless Application Repository, you can delete the application from the Lambda console in your AWS account (under Applications).

Alternatively, if you installed the application using SAM, you can delete the application using the AWS CLI. Assuming you used the default application name for the stack name (`personalize-monitor`), you can run the following:

```bash
aws cloudformation delete-stack --stack-name personalize-monitor
```

You can also delete the application stack in CloudFormation in the AWS console.

## <a name='FAQs'></a>FAQs

***Q: Can I use this application to determine my accumulated inference charges during the month?***

***A:*** No! Although the `averageRPS`/`averageTPS` and `minRecommendationRequestsPerSecond`/`minProvisionedTPS` custom metrics generated by this application may be used to calculate an approximation of your accumulated inference charges, they should not be used as a substitute or proxy for actual Personalize inference costs. Always consult your AWS Billing Dashboard for actual service charges.

***Q: What is an ideal recommender/campaign utilization percentage? Is it okay if my recommender/campaign utilization is over 100%?***

***A:*** The recommender/campaign utilization metric is a measure of your actual usage compared against the `minRecommendationRequestsPerSecond`/`minProvisionedTPS` for the recommender/campaign. Any utilization value >= 100% is ideal since that means you are not over-provisioning, and therefore not over-paying, for resources. You're letting Personalize handle the scaling in/out of the recommender/campaign. Anytime your utilization is below 100%, more resources are provisioned than are needed to satisfy the volume of requests at that time.

***Q: How can I tell if Personalize is scaling out fast enough?***

***A:*** Compare the "Actual vs Provisioned RPS/TPS" graph to the "Recommender/Campaign Latency" graph on the Personalize Monitor CloudWatch dashboard. When your Actual RPS/TPS increases/spikes, does the latency for the same recommender/campaign at the same time stay consistent? If so, this tells you that Personalize is maintaining response time as request volume increases and therefore scaling fast enough to meet demand. However, if latency increases significantly and to an unacceptable level for your application, this is an indication that Personalize may not be scaling fast enough to meet your traffic patterns. See the answer to the following question for some options.

***Q: My workload is very spiky and Personalize is not scaling fast enough. What can I do?***

***A:*** First, be sure to confirm that it is Personalize that is not scaling fast enough by reviewing the answer above. If the spikes are predictable or cyclical, you can pre-warm capacity in your recommender/campaign ahead of time by adjusting the `minRecommendationRequestsPerSecond`/`minProvisionedTPS` using the [UpdateRecommender](https://docs.aws.amazon.com/personalize/latest/dg/API_UpdateRecommender.html) or [UpdateCampaign](https://docs.aws.amazon.com/personalize/latest/dg/API_UpdateCampaign.html) API and then dropping it back down after the traffic subsides. For example, increase capacity 30 minutes before a flash sale or marketing campaign is launched that brings a temporary surge in traffic. This can be done manually using the AWS console or automated by using [CloudWatch events](https://docs.aws.amazon.com/AmazonCloudWatch/latest/events/WhatIsCloudWatchEvents.html) based on a schedule or triggered based on an event in your application. The [personalize_update_tps](./src/personalize_update_tps_function/) function that is deployed with this application can be used as the target for CloudWatch events or you can publish an `UpdatePersonalizeRecommenderMinRecommendationRPS` or `UpdatePersonalizeCampaignMinProvisionedTPS` event to EventBridge. If spikes in your workload are not predictable or known ahead of time, determining the optimal `minRecommendationRequestsPerSecond`/`minProvisionedTPS` to balance consistent latency vs cost is the best option. The metrics and dashboard graphs in this application can help you determine this value.

***Q: After deploying this application in my AWS account, I created some new Personalize recommenders or campaigns that I also want to monitor. How can I add them to be monitored and have them appear on my dashboard? Also, what about monitoried recommenders or campaigns that I delete?***

***A:*** If you specified `all` for the `RecommenderARNs` or `CampaignARNs` deployment parameter (see installation instructions above), any new recommenders/campaigns you create will be automatically monitored and alarms created (if `AutoCreateAlarms` was set to `Yes`) when the recommenders/campaigns become active. Likewise, any recommenders/campaigns that are deleted will no longer be monitored. If you want this application to monitor recommenders/campaigns across multiple regions, be sure to specify the region names in the `Regions` deployment parameter. Note that this only applies when `RecommenderARNs` or `CampaignARNs` is set to `all`. The CloudWatch dashboard will be automatically rebuilt ever hour to add new recommenders and campaigns and drop deleted recommenders and campaigns. You can also trigger the dashboard to be rebuilt by publishing a `BuildPersonalizeMonitorDashboard` event to the default EventBridge event bus (see [dashboard_mgmt_function](./src/dashboard_mgmt_function/)).

If you want to change your deployment parameters that control what recommenders/campaigns are monitored, redeploy the application using the `--guided` parameter and follow the prompts.

**IMPORTANT: Redeploying this application will fully rebuild and replace your Personalize Monitor dashboard so any changes you made manually to the dashboard will be lost.**

## <a name='Reportingissues'></a>Reporting issues

If you encounter a bug, please create a new issue with as much detail as possible and steps for reproducing the bug. Similarly, if you have an idea for an improvement, please add an issue as well. Pull requests are also welcome! See the [Contributing Guidelines](./CONTRIBUTING.md) for more details.

## <a name='Licensesummary'></a>License summary

This sample code is made available under a modified MIT license. See the LICENSE file.
